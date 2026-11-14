from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from app.core.config import settings


class UnsafeUrlError(ValueError):
    pass


_BLOCKED_PORTS = {22, 23, 25, 135, 139, 445, 3389}
_MAX_REDIRECTS = 5

# 云厂商的元数据服务：登记的端点开了内网访问也不放行（拿到它等于拿到这台机器的云凭据）。链路本地整段都算
# （169.254.169.254、ECS 任务元数据 169.254.170.2、IPv6 的 fe80::/10），另加不在链路本地段里的几个
_METADATA_HOSTS = frozenset({"100.100.100.200", "fd00:ec2::254", "metadata.google.internal"})

# NAT64 标准前缀（RFC 6052 / RFC 8215）：末 32 位嵌的是目标 IPv4 地址
_NAT64_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)

_ORIG_GETADDRINFO = socket.getaddrinfo
_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _strip_zone(ip: str) -> str:
    return ip.split("%", 1)[0].strip()


def _unwrapped_ips(ip: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    clean = _strip_zone(ip)
    if not clean:
        return None
    try:
        addr = ipaddress.ip_address(clean)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if addr.ipv4_mapped is not None:
            embedded.append(addr.ipv4_mapped)
        if addr.sixtofour is not None:
            embedded.append(addr.sixtofour)
        if addr.teredo is not None:
            server_v4, client_v4 = addr.teredo
            embedded.append(server_v4)
            embedded.append(client_v4)
        if any(addr in net for net in _NAT64_PREFIXES):
            embedded.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        if int(addr) < (1 << 32) and int(addr) not in (0, 1):
            embedded.append(ipaddress.IPv4Address(int(addr)))
        if embedded:
            return list(embedded)
    return [addr]


def _addr_is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or str(addr).lower() in _METADATA_HOSTS
    )


def _unwrap_ip(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    addrs = _unwrapped_ips(ip)
    if not addrs:
        return None
    if len(addrs) > 1:
        for candidate in addrs:
            if _addr_is_private(candidate):
                return candidate
        return addrs[-1]
    return addrs[0]


def _is_metadata(ip: str) -> bool:
    clean = _strip_zone(ip)
    if not clean:
        return True
    try:
        raw_addr = ipaddress.ip_address(clean)
    except ValueError:
        return True
    addrs = _unwrapped_ips(clean) or [raw_addr]
    if raw_addr.is_link_local or str(raw_addr).lower() in _METADATA_HOSTS:
        return True
    return any(a.is_link_local or str(a).lower() in _METADATA_HOSTS for a in addrs)


def _is_private(ip: str) -> bool:
    addrs = _unwrapped_ips(ip)
    if not addrs:
        return True
    if _is_metadata(ip):
        return True
    return any(_addr_is_private(a) for a in addrs)


# 本机代理 fake-ip 模式最常见的假地址段。没配 fake-ip 网段时，拦截报错里据此提示一句
_COMMON_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
# 核实过的真实地址：host -> (过期时刻, 地址)。TTL 取 DNS 给的，夹在 30s–300s 之间
_real_ip_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_DOH_TYPES = {"A": 1, "AAAA": 28}


def _fake_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(c, strict=False) for c in settings.http_tool_fake_ip_ranges]


def _in(ip: str, networks) -> bool:
    addrs = _unwrapped_ips(ip)
    if not addrs:
        return False
    return any(addr.version == n.version and addr in n for addr in addrs for n in networks)


def _doh_client() -> httpx.AsyncClient:
    # 不走环境变量里的代理：核实要的就是公共 DNS 的原话
    return httpx.AsyncClient(timeout=5, trust_env=False)


async def _real_ips(host: str) -> tuple[str, ...]:
    """向公共 DNS（DoH）问 host 的真实地址。问不到就拦——核实不了不能当作安全。"""
    cached = _real_ip_cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    nxdomain = False
    async with _doh_client() as client:
        for base in settings.http_tool_doh_urls:
            try:
                replies = await asyncio.gather(*(
                    client.get(base, params={"name": host, "type": t},
                               headers={"accept": "application/dns-json"})
                    for t in _DOH_TYPES
                ))
                bodies = [r.json() for r in replies if r.status_code == 200]
            except (httpx.HTTPError, ValueError):
                continue
            if len(bodies) != len(_DOH_TYPES):
                continue
            ips: list[str] = []
            ttls: list[int] = []
            for body in bodies:
                for answer in body.get("Answer") or []:
                    if answer.get("type") in _DOH_TYPES.values() and answer.get("data"):
                        ips.append(str(answer["data"]))
                        ttls.append(int(answer.get("TTL") or 0))
            if ips:
                ttl = min(max(min(ttls), 30), 300)
                _real_ip_cache[host] = (time.monotonic() + ttl, tuple(ips))
                return tuple(ips)
            nxdomain = nxdomain or all(b.get("Status") == 3 for b in bodies)
            if nxdomain:
                break
    if nxdomain:
        raise UnsafeUrlError(f"{host} 在公共 DNS 中无法解析（本机只解析到代理分配的虚拟地址），已拦截")
    raise UnsafeUrlError(f"{host} 解析到代理分配的虚拟地址，且未能向公共 DNS 核实真实地址，已拦截")


async def _resolve_host(host: str, port: int) -> list[Any]:
    if socket.getaddrinfo is not _ORIG_GETADDRINFO:
        return list(await asyncio.to_thread(socket.getaddrinfo, host, port))
    return list(await asyncio.get_running_loop().getaddrinfo(host, port))


async def resolve_safe_url(url: str, *, allow_intranet: bool = False) -> tuple[str, str]:
    """校验 URL 安全性并返回 (url, 首个已核验的解析 IP)。"""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as e:
        raise UnsafeUrlError("URL 格式或端口不合法") from e

    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"只支持 http/https，当前为「{parsed.scheme}」")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL 中没有主机名")
    if port in _BLOCKED_PORTS:
        raise UnsafeUrlError(f"端口 {port} 不允许访问")
    clean_host = _strip_zone(host)
    if host.lower() in _METADATA_HOSTS or clean_host.lower() in _METADATA_HOSTS:
        raise UnsafeUrlError(f"{host} 是云服务器的元数据地址，已拦截")

    if not allow_intranet:
        allowlist = settings.http_tool_allowlist
        if allowlist:
            if not any(clean_host == d or clean_host.endswith("." + d) for d in allowlist):
                raise UnsafeUrlError(f"{host} 不在允许访问的域名列表中（AGENTLAB_HTTP_TOOL_ALLOWLIST）")

    # 逐个解析结果都要查：一个域名可能同时解析到公网和内网地址
    target_port = port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await _resolve_host(clean_host, target_port)
    except OSError as e:
        raise UnsafeUrlError(f"域名解析失败：{host}") from e
    if not infos:
        raise UnsafeUrlError(f"域名解析失败：{host}")

    # 无论是否允许内网，云元数据地址一律先拦
    for info in infos:
        resolved_ip = str(info[4][0])
        if _is_metadata(resolved_ip):
            raise UnsafeUrlError(f"{host} 解析到元数据地址 {resolved_ip}，已拦截")

    pinned_ip = _strip_zone(str(infos[0][4][0]))
    if allow_intranet:
        return url, pinned_ip

    fake = _fake_networks()
    try:
        ipaddress.ip_address(clean_host)
        is_literal = True  # 直接写的 IP 没有域名可以去核实
    except ValueError:
        is_literal = False

    needs_check = False
    for info in infos:
        ip = str(info[4][0])
        if fake and _in(ip, fake) and not is_literal:
            needs_check = True  # 假地址本身说明不了什么，下面问公共 DNS
            continue
        if _is_private(ip):
            hint = ""
            if not fake and _in(ip, [_COMMON_FAKE_IP]):
                hint = ("。198.18.x.x 通常是本机代理 fake-ip 模式分配的虚拟地址：可设置 "
                        "AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES=198.18.0.0/15，改为向公共 DNS 核实真实地址；"
                        "或将代理的 DNS 改为 redir-host，并把该域名加入 fake-ip-filter")
            raise UnsafeUrlError(f"{host} 解析到内网地址 {ip}，已拦截{hint}")

    if needs_check:
        real_list = await _real_ips(clean_host)
        for ip in real_list:
            if _is_metadata(ip):
                raise UnsafeUrlError(f"{host} 的真实地址 {ip} 是元数据地址，已拦截")
            if _is_private(ip):
                raise UnsafeUrlError(f"{host} 的真实地址 {ip} 是内网地址，已拦截")
        # 注意：fake-ip 模式下本机路由走虚拟网卡，保留 infos[0] 供本机栈连通；非 fake-ip 则直连已核验公网 IP

    return url, pinned_ip


async def assert_safe_url(url: str, *, allow_intranet: bool = False) -> str:
    """挡住 SSRF。

    agent 手里的 HTTP 工具就是一把面向内网的扫描枪，所以这里做三件事：
    只放行 http/https、解析域名后逐个 IP 检查是不是内网/回环/元数据地址、
    再按配置里的域名白名单过一遍。

    本机代理开了 fake-ip 时，查什么都先拿到 198.18 段的假地址。配了
    http_tool_fake_ip_ranges 的话，落在段里的解析结果改向公共 DNS 核实真实地址
    再判断；直接放行不行——内网域名在 fake-ip 下也是这个段。

    allow_intranet 只给管理员登记、开了内网访问的端点用（app/capabilities/endpoints.py）：内网、回环地址放行，
    元数据地址和高危端口照样拦；域名白名单管的是模型能去哪儿，登记的端点不受它限制。模型给出的地址永远不带这个参数。
    """
    safe_url, _ = await resolve_safe_url(url, allow_intranet=allow_intranet)
    return safe_url


_ORIG_ASSERT_SAFE_URL = assert_safe_url


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """把 connect_tcp 固定到已通过 SSRF 校验的解析 IP，阻断 DNS Rebinding（TOCTOU），
    同时保留原始 URL、Host 头与 TLS SNI 主机名。"""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, pins: dict[str, str]) -> None:
        self._inner = inner
        self._pins = pins

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        key = _strip_zone(host).lower()
        target = self._pins.get(key, host)
        return await self._inner.connect_tcp(
            target,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport(pins: dict[str, str]) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(verify=True, trust_env=False)
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if pool is not None and backend is not None:
        pool._network_backend = _PinnedNetworkBackend(backend, pins)
    return transport


async def _validate_hop(url: str, pins: dict[str, str]) -> str:
    if assert_safe_url is _ORIG_ASSERT_SAFE_URL:
        safe_url, resolved_ip = await resolve_safe_url(url)
        host = urlparse(safe_url).hostname
        if host and resolved_ip:
            pins[_strip_zone(host).lower()] = resolved_ip
        return safe_url
    return await assert_safe_url(url)


async def safe_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: object | None = None,
    data: str | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    """发一个受控的 HTTP 请求：禁跟随跳转（跳转是绕过 SSRF 检查的常见手法），
    逐跳重新校验并绑定已校验 IP，并对响应体流式截断。"""
    timeout = timeout or settings.http_tool_timeout
    max_bytes = max_bytes or settings.http_tool_max_bytes
    pins: dict[str, str] = {}
    current = await _validate_hop(url, pins)

    _SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})
    cur_method = method.upper()
    cur_headers = dict(headers or {})
    cur_params: dict[str, str] | None = params
    cur_json: object | None = json_body
    cur_data: str | None = data

    async def _run(active_client: httpx.AsyncClient) -> dict[str, object]:
        nonlocal current, cur_method, cur_headers, cur_params, cur_json, cur_data
        for hop in range(_MAX_REDIRECTS + 1):
            async with active_client.stream(
                cur_method,
                current,
                headers=cur_headers,
                params=cur_params,
                json=cur_json,
                content=cur_data,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    if hop >= _MAX_REDIRECTS:
                        await resp.aclose()
                        raise UnsafeUrlError("重定向次数过多")
                    prev_url = httpx.URL(current)
                    next_url = prev_url.join(resp.headers["location"])
                    current = await _validate_hop(str(next_url), pins)
                    cur_params = None
                    if (next_url.scheme, next_url.host, next_url.port) != (prev_url.scheme, prev_url.host, prev_url.port):
                        cur_headers = {k: v for k, v in cur_headers.items() if k.lower() not in _SENSITIVE_HEADERS}
                    if resp.status_code == 303 or (resp.status_code in (301, 302) and cur_method == "POST"):
                        cur_method = "GET"
                        cur_json = None
                        cur_data = None
                    continue

                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    remaining = max_bytes - total
                    if len(chunk) > remaining:
                        if remaining > 0:
                            chunks.append(chunk[:remaining])
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)

                body = b"".join(chunks)
                try:
                    text = body.decode(resp.encoding or "utf-8", errors="replace")
                except (LookupError, UnicodeDecodeError):
                    text = body.decode("utf-8", errors="replace")
                return {
                    "status": resp.status_code,
                    "url": str(resp.url),
                    "headers": dict(resp.headers),
                    "body": text,
                    "truncated": truncated,
                }
        raise UnsafeUrlError("重定向次数过多")

    if client is not None:
        return await _run(client)

    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": False,
        "trust_env": False,
    }
    if httpx.AsyncClient is _ORIG_ASYNC_CLIENT:
        client_kwargs["transport"] = _pinned_transport(pins)

    async with httpx.AsyncClient(**client_kwargs) as owned:
        return await _run(owned)

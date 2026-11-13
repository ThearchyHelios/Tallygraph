import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const ROOT = dirname(dirname( dirname(fileURLToPath(import.meta.url)) ))
const FRONTEND = join(ROOT, 'frontend')

/**
 * 跨平台解析可用的 Python 解释器：
 * 优先尊重 CHECK_PYTHON、当前激活的虚拟环境 / Conda 环境、仓库内 .venv，
 * 再回退到常见开发路径与系统 PATH 中的 python3 / python，并实际试跑确认可执行。
 */
export function resolvePython({ requireBackend = false } = {}) {
  const isWin = process.platform === 'win32'
  const venvBin = (dir) => (isWin ? join(dir, 'Scripts', 'python.exe') : join(dir, 'bin', 'python'))
  const candidates = [
    process.env.CHECK_PYTHON,
    process.env.VIRTUAL_ENV && venvBin(process.env.VIRTUAL_ENV),
    process.env.CONDA_PREFIX && (isWin ? join(process.env.CONDA_PREFIX, 'python.exe') : venvBin(process.env.CONDA_PREFIX)),
    venvBin(join(ROOT, '.venv')),
    venvBin(join(ROOT, 'backend', '.venv')),
    join(homedir(), 'miniforge3', 'envs', 'agentlab', 'bin', 'python'),
    join(homedir(), 'miniconda3', 'envs', 'agentlab', 'bin', 'python'),
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    'python3',
    'python',
  ].filter(Boolean)

  const probe = requireBackend
    ? 'import sys, app; assert sys.version_info >= (3, 12)'
    : 'import sys; assert sys.version_info >= (3, 12)'
  const cwd = requireBackend ? join(ROOT, 'backend') : ROOT

  const seen = new Set()
  for (const bin of candidates) {
    if (seen.has(bin)) continue
    seen.add(bin)
    if ((bin.includes('/') || bin.includes('\\')) && !existsSync(bin)) continue
    const res = spawnSync(bin, ['-c', probe], { cwd, stdio: 'ignore' })
    if (res.status === 0) return bin
  }
  return process.env.CHECK_PYTHON ?? 'python3'
}

/** 跨平台解析 Chrome / Chromium 可执行文件路径 */
export function resolveChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH
  const candidates = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/snap/bin/chromium',
  ]
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return candidates[0]
}

function resolveTsFile(fromDir, spec) {
  const base = spec.startsWith('/src/')
    ? join(FRONTEND, spec.slice(1))
    : resolve(fromDir, spec)
  const tries = extname(base)
    ? [base]
    : [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]
  for (const candidate of tries) {
    if (existsSync(candidate)) return candidate
  }
  throw new Error(`找不到模块 ${spec}（自 ${fromDir}）`)
}

/**
 * 为 check-trace / check-decode 提供前端 TS 模块加载器：
 * - 若本地或指定的 AGENTLAB_WEB 开发服务器可达，直接拉取其转译结果；
 * - 若开发服务器未启动（如 CI、离线环境或单跑脚本），自动回退到 Vite 内置的 Oxc 内存转译，无需启服务或绑端口。
 */
export async function createTsLoader(webUrl = process.env.AGENTLAB_WEB ?? 'http://localhost:5273') {
  let serverOnline = false
  if (process.env.CHECK_OFFLINE !== '1') {
    try {
      const r = await fetch(`${webUrl}/`, { signal: AbortSignal.timeout(400) })
      serverOnline = r.ok
    } catch {
      serverOnline = false
    }
  }

  if (serverOnline) {
    const loaded = new Map()
    const loadFromWeb = async (modPath) => {
      if (loaded.has(modPath)) return loaded.get(modPath)
      const res = await fetch(`${webUrl}${modPath}?t=${Date.now()}`)
      if (!res.ok) throw new Error(`${modPath} → HTTP ${res.status}`)
      let code = await res.text()
      const deps = new Set([...code.matchAll(/from\s+["'](\/src\/[^"'?]+)(?:\?[^"']*)?["']/g)].map((m) => m[1]))
      for (const dep of deps) {
        const url = await loadFromWeb(dep)
        const pattern = new RegExp(`(["'])${dep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\?[^"']*)?\\1`, 'g')
        code = code.replace(pattern, JSON.stringify(url))
      }
      const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
      loaded.set(modPath, url)
      return url
    }
    return loadFromWeb
  }

  const viteEntry = pathToFileURL(join(FRONTEND, 'node_modules', 'vite', 'dist', 'node', 'index.js')).href
  const { transformWithOxc } = await import(viteEntry)
  const loaded = new Map()

  const loadLocalFile = async (absPath) => {
    if (loaded.has(absPath)) return loaded.get(absPath)
    const raw = readFileSync(absPath, 'utf8')
    const lang = absPath.endsWith('.tsx') ? 'tsx' : 'ts'
    const transformed = await transformWithOxc(raw, absPath, { lang, sourcemap: false })
    let code = transformed.code
    const dir = dirname(absPath)
    const specs = new Set(
      [...code.matchAll(/(?:import|export)\s+(?:[^"']*?\s+from\s+)?["']((?:\.\.?\/|\/src\/)[^"'?]+)(?:\?[^"']*)?["']/g)]
        .map((m) => m[1]),
    )
    for (const spec of specs) {
      const depAbs = resolveTsFile(dir, spec)
      const depUrl = await loadLocalFile(depAbs)
      const escaped = spec.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const pattern = new RegExp(`(["'])${escaped}(\\?[^"']*)?\\1`, 'g')
      code = code.replace(pattern, JSON.stringify(depUrl))
    }
    const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
    loaded.set(absPath, url)
    return url
  }

  return async (modPath) => loadLocalFile(resolveTsFile(FRONTEND, modPath))
}

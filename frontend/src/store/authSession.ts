import type { Principal } from '../typesApp'

export type AuthModule = typeof import('./auth')

let authModulePromise: Promise<AuthModule> | null = null

/**
 * 按需加载认证状态模块，避免 /s 等公开路由在首屏入口静态拉取整棵认证与目录依赖树，
 * 同时与登录页及主外壳复用同一个 useAuth 单例，确保 /auth/me 全局只请求一次。
 */
export function loadAuthModule(): Promise<AuthModule> {
  if (!authModulePromise) {
    authModulePromise = import('./auth')
  }
  return authModulePromise
}

export async function resolveBootSession(timeoutMs: number): Promise<{
  authMod: AuthModule
  me: Principal | null | undefined
  loading: Promise<Principal | null>
}> {
  const authMod = await loadAuthModule()
  const loading = authMod.useAuth.getState().load()
  const me = await Promise.race([
    loading,
    new Promise<undefined>((resolve) => setTimeout(() => resolve(undefined), timeoutMs)),
  ])
  return { authMod, me, loading }
}

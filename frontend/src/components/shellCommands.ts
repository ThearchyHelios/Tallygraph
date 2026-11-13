import { useMemo, useSyncExternalStore } from 'react'
import type { ComponentType } from 'react'
import type { NavigateFunction } from 'react-router-dom'
import { create } from 'zustand'
import {
  BookOpen, CalendarClock, Database, FlaskConical, Gauge, History, Layers,
  LayoutGrid, MessageSquare, Settings, Wrench,
} from 'lucide-react'
import { ApiError, api } from '../api/client'
import { atLeast, loginOn, prefsLocalOnly, useAuth } from '../store/auth'
import { useConversations } from '../store/conversations'
import { toast } from './ui'
import { applyTheme, readThemePref, resolvedTheme } from '../lib/theme'
import type { ThemePref } from '../lib/theme'
import { parseServerTime } from '../lib/format'
import { APP_TEXT, SCHEDULE_TEXT } from '../lib/terms'
import { EVAL_TEXT } from '../lib/termsEval'
import { errorMessage } from '../lib/errors'
import { disableNotify, enableNotify, useSignals } from '../lib/notify'
import { canLeave, leavePass } from '../lib/leave'
import type { Principal } from '../typesApp'

export interface PageDef {
  to: string
  label: string
  /** 一句话说这一页是干什么的：导航悬停提示和命令面板里都用 */
  hint: string
  icon: ComponentType<{ size?: number; className?: string; strokeWidth?: number }>
  shortcut: string
  /** 命令面板的搜索词：英文路由名、旧叫法、同义词 */
  keywords: string
}

// ⌥ + 数字切页：不和浏览器的 ⌘1–9（切标签页，页面拦不住）撞，也不和画布上的
// Backspace / Delete 以及各处输入框打架——「g c」这类两键序列就是栽在这上面
export const PAGES: PageDef[] = [
  { to: '/chat', label: '问数据', hint: '描述需求，自动接入数据源、构建工作流并给出结论', icon: MessageSquare, shortcut: 'Alt+1', keywords: 'chat ask 对话 提问 首页' },
  { to: '/studio', label: '编排', hint: '在画布上构建、调试和发布工作流', icon: FlaskConical, shortcut: 'Alt+2', keywords: 'studio canvas 画布 工作流 workflow' },
  // 应用是后加的，紧跟在「编排」后面（用的是编排发布出来的东西）；快捷键取下一个空着的 ⌥9，别的页一个都不动
  { to: '/apps', label: APP_TEXT.nav, hint: APP_TEXT.hint, icon: LayoutGrid, shortcut: 'Alt+9', keywords: 'apps app 应用 待办 inbox 流程 看板 发起' },
  { to: '/runs', label: '记录', hint: '每次运行的过程、成果和待审批', icon: History, shortcut: 'Alt+3', keywords: 'runs history 运行 运行记录 历史 审批' },
  { to: '/data', label: '数据', hint: '接入数据库和表格，供问数据和工作流查询', icon: Database, shortcut: 'Alt+4', keywords: 'data datasource 数据源 数据库 表格 excel csv' },
  { to: '/tools', label: '工具', hint: '工具库、沙箱试验台、自定义工具、MCP', icon: Wrench, shortcut: 'Alt+5', keywords: 'tools mcp sandbox 沙箱 函数' },
  { to: '/knowledge', label: '知识', hint: '知识库、长期记忆、方法论 Skill', icon: BookOpen, shortcut: 'Alt+6', keywords: 'knowledge kb memory skill 知识库 记忆 文档' },
  // 后加的页排在设置前面，设置留在最底下、快捷键也不动：⌥7 按了好几个版本，改了等于让人重新记
  { to: '/schedules', label: SCHEDULE_TEXT.title, hint: SCHEDULE_TEXT.hint, icon: CalendarClock, shortcut: 'Alt+8', keywords: 'schedules schedule cron 定时 计划 周期 自动运行' },
  // 评测（评测-D8）：搭建者以上可见（使用者的页面表在 visiblePages 里收窄，用不着另判），同样排在设置前面；快捷键用最后一个空着的 ⌥0
  { to: '/evals', label: EVAL_TEXT.nav, hint: EVAL_TEXT.hint, icon: Gauge, shortcut: 'Alt+0', keywords: 'evals eval evaluation 评测 回归 题集 准确率 A/B 口径命中 重复度' },
  // 口径目录：已发布口径的入口、依赖和试跑。后加的页，快捷键用没占用的 Alt+L
  { to: '/ledger/calibers', label: '口径', hint: '查看已发布口径的入口、依赖，并按参数试运行', icon: Layers, shortcut: 'Alt+L', keywords: 'caliber ledger 口径 指标 试运行 入口' },
  { to: '/settings', label: '设置', hint: '模型接入、偏好、运行环境', icon: Settings, shortcut: 'Alt+7', keywords: 'settings provider 模型 署名 主题 偏好' },
]

/**
 * 这个人看得到哪几页。开了登录的使用者（user 角色）只有「应用」和「设置」（个人那几项），管理员开了「使用者可以问数据」
 * 时加上问数据（Q2）；搭建者以上和关登录时全部都在
 */
export function visiblePages(me: Principal | null | undefined): PageDef[] {
  if (!loginOn(me) || atLeast(me, 'builder')) return PAGES
  const keep = new Set(['/apps', '/settings', ...(me?.user_can_chat ? ['/chat'] : [])])
  return PAGES.filter((p) => keep.has(p.to))
}

export function usePages(): PageDef[] {
  const me = useAuth((s) => s.me)
  return useMemo(() => visiblePages(me), [me])
}

// -------------------------------------------------------------------------
// 外壳浮层的开关
// -------------------------------------------------------------------------

export const useShell = create<{ palette: boolean; help: boolean }>(() => ({ palette: false, help: false }))

export const openCommandPalette = () => useShell.setState({ palette: true, help: false })
export const closeCommandPalette = () => useShell.setState({ palette: false })
export const toggleCommandPalette = () => useShell.setState((s) => ({ palette: !s.palette, help: false }))
export const openShortcutHelp = () => useShell.setState({ help: true, palette: false })
export const closeShortcutHelp = () => useShell.setState({ help: false })
export const useShellOverlay = () => useShell((s) => s.palette || s.help)

// -------------------------------------------------------------------------
// 命令：导航和面板共用
// -------------------------------------------------------------------------

function subscribeTheme(cb: () => void): () => void {
  const mo = new MutationObserver(cb)
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  const mq = window.matchMedia('(prefers-color-scheme: light)')
  mq.addEventListener('change', cb)
  return () => {
    mo.disconnect()
    mq.removeEventListener('change', cb)
  }
}
const themeSnapshot = () => `${readThemePref()}:${resolvedTheme()}`

/** 当前的主题偏好和实际画的那一套，谁改了都跟得上 */
export function useTheme(): { pref: ThemePref; resolved: 'light' | 'dark' } {
  const snap = useSyncExternalStore(subscribeTheme, themeSnapshot, () => 'system:dark')
  const [pref, resolved] = snap.split(':') as [ThemePref, 'light' | 'dark']
  return { pref, resolved }
}

/** 最后一次选的主题，和它还没存进设置时的那个值（断线时换的） */
let latestTheme: ThemePref | null = null
let unsavedTheme: ThemePref | null = null

export async function setThemePref(pref: ThemePref): Promise<void> {
  applyTheme(pref)
  latestTheme = pref
  window.dispatchEvent(new CustomEvent('agentlab:theme', { detail: pref }))
  if (prefsLocalOnly(useAuth.getState().me)) {
    unsavedTheme = null
    return
  }
  try {
    await saveTheme(pref)
  } catch (e) {
    const offline = e instanceof ApiError && e.kind === 'network'
    toast.warn(
      offline ? '主题已在本设备生效，连接服务端后将自动保存到设置' : '主题仅在本设备生效，未能保存到设置',
      { detail: errorMessage(e), key: 'theme-save' },
    )
  }
}

async function saveTheme(pref: ThemePref): Promise<void> {
  try {
    const current = await api.settings.get()
    await api.settings.put({ ui: { ...(current?.ui ?? {}), theme: pref } })
    if (latestTheme === pref) unsavedTheme = null
  } catch (e) {
    if (latestTheme === pref) unsavedTheme = pref
    throw e
  }
}

export function flushThemePref(): boolean {
  if (!unsavedTheme) return false
  if (prefsLocalOnly(useAuth.getState().me)) {
    unsavedTheme = null
    return true
  }
  void saveTheme(unsavedTheme).catch(() => {})
  return true
}

export const toggleTheme = () => void setThemePref(resolvedTheme() === 'dark' ? 'light' : 'dark')

export async function toggleNotify(): Promise<void> {
  if (useSignals.getState().notify) {
    disableNotify()
    toast.info('后台提醒已关闭')
    return
  }
  const permission = await enableNotify()
  if (permission === 'granted') toast.ok('后台提醒已开启：标签页在后台时，运行结束、失败或有新的待审批、待办时发送系统通知')
  else if (permission === 'denied') {
    toast.warn('浏览器已拒绝通知权限', {
      detail: '请在地址栏左侧的站点设置中将「通知」改为允许，然后重新打开此开关。',
      key: 'notify-denied',
    })
  } else if (permission === 'unsupported') toast.warn('当前浏览器不支持系统通知')
}

export async function newConversation(navigate: NavigateFunction): Promise<void> {
  if (!(await canLeave())) return
  try {
    const id = await useConversations.getState().create()
    navigate(`/chat/${id}`, leavePass())
  } catch (e) {
    toast.error(e)
  }
}

export function waitedFor(createdAt: string | null | undefined, now = Date.now()): string | null {
  const d = parseServerTime(createdAt)
  if (!d) return null
  const mins = Math.max(0, Math.floor((now - d.getTime()) / 60_000))
  if (mins < 1) return '不到 1 分钟'
  if (mins < 60) return `${mins} 分钟`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时`
  return `${Math.floor(hours / 24)} 天`
}

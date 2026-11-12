import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Check, Network, Plus, Sparkles, Square, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { DataSourceName } from '../../types'
import type {
  CanonicalEntity,
  CanonicalEntityBinding,
  CanonicalEntityCaliberParam,
  CanonicalEntityMember,
  EntityApplyPatchOut,
  EntityDiscoveryJob,
  OntologyStatus,
} from '../../typesLedger'
import { EmptyState, ErrorState, Modal, Notice, Spinner, toast } from '../../components/ui'
import { EntityAssistant } from './EntityAssistant'
import { EntityDiscoverDialog } from './EntityDiscoverDialog'

const splitWords = (text: string) =>
  text.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean)

const parseUtcMs = (ts?: string | null): number => {
  if (!ts) return NaN
  const raw = ts.trim()
  if (!raw) return NaN
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`
  return Date.parse(normalized)
}

const formatDurationSec = (sec: number): string => {
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s > 0 ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

const formatCodes = (codes?: Record<string, string>) =>
  Object.entries(codes ?? {}).map(([k, v]) => `${k}=${v}`).join('，')

const parseCodes = (text: string): Record<string, string> => {
  const out: Record<string, string> = {}
  for (const part of splitWords(text)) {
    const eq = part.indexOf('=')
    if (eq <= 0) continue
    const k = part.slice(0, eq).trim()
    const v = part.slice(eq + 1).trim()
    if (k && v) out[k] = v
  }
  return out
}

const STATUS_LABEL: Record<OntologyStatus, string> = {
  confirmed: '已确认',
  verified: '已验证',
  proposed: '待确认',
  rejected: '已驳回',
}

function StatusPill({ status }: { status: OntologyStatus }) {
  const cls = status === 'confirmed' || status === 'verified'
    ? 'text-[var(--ok)] border-[var(--ok)]/30'
    : status === 'proposed'
      ? 'text-[var(--warn)] border-[var(--warn)]/40'
      : 'text-faint border-[var(--border)]'
  return (
    <span className={clsx('inline-flex items-center rounded border px-1.5 py-0.5 text-2xs', cls)}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

interface DraftState {
  id: string | null
  version: number
  key: string
  label: string
  description: string
  aliasesText: string
  bindings: CanonicalEntityBinding[]
  members: (CanonicalEntityMember & { codesText: string; aliasesText: string })[]
  caliberParams: CanonicalEntityCaliberParam[]
}

function toDraft(entity: CanonicalEntity): DraftState {
  return {
    id: entity.id,
    version: entity.version,
    key: entity.key,
    label: entity.label,
    description: entity.description ?? '',
    aliasesText: (entity.aliases ?? []).join('，'),
    bindings: (entity.bindings ?? []).map((b) => ({ ...b })),
    members: (entity.members ?? []).map((m) => ({
      ...m,
      aliasesText: (m.aliases ?? []).join('，'),
      codesText: formatCodes(m.source_codes),
    })),
    caliberParams: (entity.caliber_params ?? []).map((p) => ({ ...p })),
  }
}

function blankDraft(sourceName: string, sourceId: string): DraftState {
  return {
    id: null,
    version: 1,
    key: '',
    label: '',
    description: '',
    aliasesText: '',
    bindings: sourceName
      ? [{
        source_id: sourceId || null,
        source_name: sourceName,
        table_name: '',
        column_name: '',
        status: 'confirmed',
        source: 'human',
        note: '',
      }]
      : [],
    members: [],
    caliberParams: [],
  }
}

export function CanonicalEntitiesView({
  sourceId,
  sourceName,
  sources,
  tables,
  onBack,
}: {
  sourceId: string
  sourceName: string
  sources: DataSourceName[]
  tables: string[]
  onBack: () => void
}) {
  const [items, setItems] = useState<CanonicalEntity[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [scope, setScope] = useState<'all' | 'source'>('all')
  const [search, setSearch] = useState('')
  const [activeId, setActiveId] = useState<string | '__new__' | null>(null)
  const [draft, setDraft] = useState<DraftState | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<unknown>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [discoverOpen, setDiscoverOpen] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  const [activeJob, setActiveJob] = useState<EntityDiscoveryJob | null>(null)
  const [finishedBanner, setFinishedBanner] = useState<EntityDiscoveryJob | null>(null)
  const [progressDetailOpen, setProgressDetailOpen] = useState(false)
  const [nowTick, setNowTick] = useState(() => Date.now())
  const [cancellingJob, setCancellingJob] = useState(false)

  const load = useCallback(async (pickId?: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.ontology.listEntities()
      const list = res.items ?? []
      setItems(list)
      const targetId = pickId !== undefined ? pickId : activeId
      if (targetId && targetId !== '__new__') {
        const found = list.find((e) => e.id === targetId)
        if (found) {
          setDraft(toDraft(found))
          setActiveId(found.id)
          return
        }
      }
      if (targetId !== '__new__' && list.length > 0) {
        setActiveId(list[0].id)
        setDraft(toDraft(list[0]))
      } else if (targetId !== '__new__') {
        setActiveId(null)
        setDraft(null)
      }
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [activeId])

  useEffect(() => {
    void load()
    void api.ontology
      .activeDiscoveryJob()
      .then((res) => {
        if (res?.job) setActiveJob(res.job)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId])

  useEffect(() => {
    if (!activeJob || (activeJob.status !== 'queued' && activeJob.status !== 'running')) return
    const timer = window.setInterval(() => {
      void api.ontology
        .getDiscoveryJob(activeJob.id)
        .then((job) => {
          if (!job || !job.status) return
          if (job.status === 'queued' || job.status === 'running') {
            setActiveJob(job)
          } else {
            setActiveJob(null)
            setFinishedBanner(job)
            if (job.status === 'succeeded') {
              void load()
            }
          }
        })
        .catch(() => {})
    }, 1500)
    return () => window.clearInterval(timer)
  }, [activeJob, load])

  const cancelDiscovery = async () => {
    if (!activeJob || cancellingJob) return
    setCancellingJob(true)
    try {
      const updated = await api.ontology.cancelDiscoveryJob(activeJob.id)
      setActiveJob(null)
      setFinishedBanner(updated)
      toast.ok('已停止候选实体扫描')
    } catch (e) {
      setSaveError(e)
    } finally {
      setCancellingJob(false)
    }
  }

  const handleAssistantApplied = (res: EntityApplyPatchOut) => {
    const firstSaved = res.entities?.[0]
    void load(firstSaved?.id ?? undefined)
  }

  useEffect(() => {
    if (!activeJob || (activeJob.status !== 'queued' && activeJob.status !== 'running')) return
    const tick = window.setInterval(() => setNowTick(Date.now()), 1000)
    return () => window.clearInterval(tick)
  }, [activeJob])

  const sourceEntityCount = useMemo(() => {
    if (!sourceName) return items.length
    return items.filter((item) =>
      (item.bindings ?? []).some((b) => b.source_name === sourceName || b.source_id === sourceId),
    ).length
  }, [items, sourceName, sourceId])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((item) => {
      if (scope === 'source' && sourceName) {
        const linked = (item.bindings ?? []).some(
          (b) => b.source_name === sourceName || b.source_id === sourceId,
        )
        if (!linked) return false
      }
      if (!q) return true
      return (
        item.key.toLowerCase().includes(q)
        || item.label.toLowerCase().includes(q)
        || (item.aliases ?? []).some((a) => a.toLowerCase().includes(q))
      )
    })
  }, [items, scope, sourceName, sourceId, search])

  const selectEntity = (entity: CanonicalEntity) => {
    setActiveId(entity.id)
    setDraft(toDraft(entity))
    setSaveError(null)
    setConfirmDelete(false)
  }

  const startCreate = () => {
    setActiveId('__new__')
    setDraft(blankDraft(sourceName, sourceId))
    setSaveError(null)
    setConfirmDelete(false)
  }

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setSaveError(null)
    try {
      const payload = {
        key: draft.key.trim(),
        label: draft.label.trim(),
        description: draft.description.trim(),
        aliases: splitWords(draft.aliasesText),
        bindings: draft.bindings
          .filter((b) => b.source_name.trim() || b.table_name.trim() || b.column_name.trim())
          .map((b) => ({
            ...(b.id ? { id: b.id } : {}),
            source_id: sources.find((s) => s.name === b.source_name)?.id ?? b.source_id ?? null,
            source_name: b.source_name.trim(),
            table_name: b.table_name.trim(),
            column_name: b.column_name.trim(),
            status: b.status || 'confirmed',
            source: b.source || 'human',
            note: (b.note ?? '').trim(),
          })),
        members: draft.members
          .filter((m) => m.value.trim())
          .map((m) => ({
            value: m.value.trim(),
            label: (m.label || m.value).trim(),
            aliases: splitWords(m.aliasesText),
            source_codes: parseCodes(m.codesText),
          })),
        caliber_params: draft.caliberParams
          .filter((p) => p.workflow_id.trim() || p.param_name.trim())
          .map((p) => ({
            ...(p.id ? { id: p.id } : {}),
            workflow_id: p.workflow_id.trim(),
            card_id: (p.card_id ?? '').trim(),
            param_name: p.param_name.trim(),
            status: p.status || 'confirmed',
            source: p.source || 'human',
            note: (p.note ?? '').trim(),
          })),
        ...(draft.id ? { version: draft.version } : {}),
      }
      const saved = draft.id
        ? await api.ontology.updateEntity(draft.id, payload)
        : await api.ontology.createEntity(payload)
      toast.ok(draft.id ? '标准业务实体已更新' : '标准业务实体已创建')
      setActiveId(saved.id)
      setDraft(toDraft(saved))
      await load(saved.id)
    } catch (e) {
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  const reviewItem = async (
    targetKind: 'binding' | 'caliber_param',
    itemId: string | undefined,
    status: 'confirmed' | 'rejected',
  ) => {
    if (!draft?.id || !itemId) return
    setSaving(true)
    setSaveError(null)
    try {
      const updated = await api.ontology.reviewEntity(draft.id, {
        target_kind: targetKind,
        item_id: itemId,
        status,
        version: draft.version,
      })
      toast.ok(status === 'confirmed' ? '已确认该映射项，现可参与跨源对齐' : '已驳回该候选映射')
      setDraft(toDraft(updated))
      setItems((cur) => cur.map((x) => (x.id === updated.id ? updated : x)))
    } catch (e) {
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  const removeEntity = async () => {
    if (!draft?.id) return
    setSaving(true)
    setSaveError(null)
    try {
      await api.ontology.deleteEntity(draft.id)
      toast.ok('标准业务实体已删除')
      setConfirmDelete(false)
      await load(null)
    } catch (e) {
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1" data-catalog-entities-view="">
      <aside className="flex min-h-0 w-full flex-col border-r lg:w-[360px] lg:shrink-0">
        <div className="flex flex-wrap items-center justify-between gap-1.5 border-b px-3 py-2">
          <button type="button" className="btn btn-xs btn-ghost" onClick={onBack} data-entity-back="">
            <ArrowLeft size={12} aria-hidden /> 返回表目录
          </button>
          <div className="flex flex-wrap items-center gap-1">
            <button
              type="button"
              className="btn btn-xs"
              disabled={Boolean(activeJob)}
              onClick={() => setDiscoverOpen(true)}
              data-entity-discover-open=""
            >
              <Sparkles size={11} aria-hidden /> 扫描发现候选实体
            </button>
            <button
              type="button"
              className={clsx('btn btn-xs', assistantOpen && 'border-[var(--accent)] text-fg')}
              onClick={() => setAssistantOpen((v) => !v)}
              aria-expanded={assistantOpen}
              data-entity-assistant-open=""
            >
              <Sparkles size={11} aria-hidden /> 实体助手
            </button>
            <button type="button" className="btn btn-xs btn-primary" onClick={startCreate} data-entity-new="">
              <Plus size={12} aria-hidden /> 新建实体
            </button>
          </div>
        </div>

        {activeJob && (() => {
          const sum = activeJob.summary ?? {}
          const pct = Math.max(6, Math.min(98, sum.progress_pct ?? (activeJob.status === 'queued' ? 8 : 25)))
          const startedMs = parseUtcMs(activeJob.started_at || activeJob.created_at)
          const elapsedSec = Number.isFinite(startedMs) ? Math.max(0, Math.floor((nowTick - startedMs) / 1000)) : null
          const scopeBits: string[] = []
          if ((sum.sources_scanned ?? 0) > 0) {
            scopeBits.push(`数据源 ${sum.sources_scanned} 个${sum.tables_scanned ? `（${sum.tables_scanned} 张表）` : ''}`)
          }
          if ((sum.documents_scanned ?? 0) > 0) {
            scopeBits.push(`文档 ${sum.documents_scanned} 份`)
          }
          if ((sum.ops_detected ?? 0) > 0) {
            scopeBits.push(`已识别 ${sum.ops_detected} 项操作`)
          }
          const candKeys = sum.candidate_keys ?? []
          return (
            <div
              className="space-y-1.5 border-b bg-elev px-3 py-2.5 text-2xs"
              data-entity-discover-banner={activeJob.status}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-start gap-1.5 text-fg">
                  <span className="mt-0.5 shrink-0">
                    <Spinner size={12} />
                  </span>
                  <span className="break-words font-medium leading-snug">
                    {activeJob.phase || '正在扫描发现候选实体…'}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost"
                    onClick={() => setProgressDetailOpen(true)}
                    data-entity-discover-detail-open=""
                  >
                    进度详情
                  </button>
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost"
                    disabled={cancellingJob}
                    onClick={() => void cancelDiscovery()}
                    data-entity-discover-cancel=""
                  >
                    <Square size={10} aria-hidden /> 停止扫描
                  </button>
                </div>
              </div>
              <div
                className="h-1.5 w-full overflow-hidden rounded-full bg-hover"
                role="progressbar"
                aria-label="候选实体扫描进度"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                data-entity-discover-progressbar={pct}
              >
                <div
                  className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300"
                  style={{ width: `${pct}%` }}
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-1 text-faint">
                <span>{scopeBits.join(' · ') || '正在准备扫描上下文…'}</span>
                <span className="tnum">
                  {pct}%{elapsedSec != null ? ` · 已用时 ${formatDurationSec(elapsedSec)}` : ''}
                </span>
              </div>
              {candKeys.length > 0 && (
                <div className="flex flex-wrap items-center gap-1 pt-0.5 text-faint">
                  <span>实时发现：</span>
                  {candKeys.slice(0, 6).map((k) => (
                    <span key={k} className="rounded border bg-panel px-1 py-0.2 font-mono text-fg">
                      {k}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })()}

        {!activeJob && finishedBanner && (
          <div
            className="flex items-center justify-between gap-2 border-b px-3 py-2 text-2xs"
            data-entity-discover-banner={finishedBanner.status}
          >
            <div className="min-w-0 text-dim">
              {finishedBanner.status === 'succeeded' ? (
                <span>
                  扫描完成：新建实体 {finishedBanner.summary?.created_entities ?? 0} 个，更新 {finishedBanner.summary?.updated_entities ?? 0} 个，新增待确认绑定 {finishedBanner.summary?.proposed_bindings ?? 0} 处、成员 {finishedBanner.summary?.added_members ?? 0} 项
                </span>
              ) : finishedBanner.status === 'cancelled' ? (
                <span>已停止候选实体扫描</span>
              ) : (
                <span className="text-[var(--err)]">{finishedBanner.error || '候选实体扫描未成功'}</span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                onClick={() => setProgressDetailOpen(true)}
                data-entity-discover-detail-open=""
              >
                查看明细
              </button>
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                onClick={() => setFinishedBanner(null)}
                aria-label="关闭扫描结果提示"
              >
                <X size={11} aria-hidden />
              </button>
            </div>
          </div>
        )}
        <div className="space-y-2 border-b px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-1" role="group" aria-label="筛选实体范围">
            <button
              type="button"
              className={clsx('btn btn-xs', scope === 'all' ? 'btn-primary' : 'btn-ghost')}
              onClick={() => setScope('all')}
              title="显示整个工作区全部数据源登记的标准业务实体"
              data-entity-scope="all"
            >
              全部实体（{items.length}）
            </button>
            {sourceName && (
              <button
                type="button"
                className={clsx('btn btn-xs', scope === 'source' ? 'btn-primary' : 'btn-ghost')}
                onClick={() => setScope('source')}
                title="仅筛选已绑定当前数据源物理表列的标准业务实体"
                data-entity-scope="source"
              >
                仅看 {sourceName}（{sourceEntityCount}）
              </button>
            )}
          </div>
          <input
            className="field w-full text-xs"
            placeholder="搜索实体名称、标识或常用叫法…"
            aria-label="搜索标准业务实体"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {loading ? (
            <div className="flex items-center gap-2 p-4 text-xs text-faint">
              <Spinner size={13} /> 正在加载标准业务实体…
            </div>
          ) : error != null ? (
            <ErrorState error={error} onRetry={() => void load()} compact />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<Network size={20} />}
              title="暂无标准业务实体"
              body="标准业务实体用于声明不同数据源之间的统一对象（如产线、门店、渠道）与编码对应关系，只有已确认的绑定才会用于跨源排查。"
              action={(
                <button type="button" className="btn btn-sm btn-primary" onClick={startCreate}>
                  新建第一个业务实体
                </button>
              )}
            />
          ) : (
            <ul className="space-y-1">
              {filtered.map((entity) => {
                const pendingCount = (entity.bindings ?? []).filter((b) => b.status === 'proposed').length
                  + (entity.caliber_params ?? []).filter((p) => p.status === 'proposed').length
                const activeCount = (entity.bindings ?? []).filter(
                  (b) => b.status === 'confirmed' || b.status === 'verified',
                ).length
                const selected = activeId === entity.id
                return (
                  <li key={entity.id}>
                    <button
                      type="button"
                      onClick={() => selectEntity(entity)}
                      data-entity-item={entity.key}
                      className={clsx(
                        'w-full rounded-md border px-2.5 py-2 text-left text-xs transition-colors',
                        selected
                          ? 'border-[var(--accent)] bg-elev text-fg'
                          : 'border-transparent hover:bg-elev text-dim',
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium text-fg">{entity.label}</span>
                        <span className="font-mono text-2xs text-faint">{entity.key}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-2xs text-faint">
                        <span>生效绑定 {activeCount} 处</span>
                        {(entity.members ?? []).length > 0 && <span>成员 {entity.members.length} 项</span>}
                        {pendingCount > 0 && (
                          <span className="text-[var(--warn)]" data-entity-pending={pendingCount}>
                            待确认 {pendingCount} 项
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </aside>

      <section className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4" aria-label="标准业务实体详情">
        {!draft ? (
          <EmptyState
            icon={<Network size={22} />}
            title="选择或新建标准业务实体"
            body="在此维护跨系统的统一业务对象（如工厂、产线、区域、门店）、各数据源的表列绑定与编码映射。"
            action={(
              <button type="button" className="btn btn-sm btn-primary" onClick={startCreate}>
                <Plus size={12} aria-hidden /> 新建实体
              </button>
            )}
          />
        ) : (
          <div className="mx-auto max-w-4xl space-y-4 text-xs" data-entity-detail={draft.key || '__new__'}>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-3">
              <div>
                <h2 className="text-sm font-semibold text-fg">
                  {draft.id ? `${draft.label}（${draft.key}）` : '新建标准业务实体'}
                </h2>
                <p className="mt-0.5 text-2xs text-faint">
                  跨源指标穿透仅采用「已确认」或「已验证」的绑定与编码映射；「待确认」候选须经人工审阅后才生效。
                </p>
              </div>
              <div className="flex items-center gap-2">
                {draft.id && !confirmDelete && (
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost text-[var(--err)]"
                    disabled={saving}
                    onClick={() => setConfirmDelete(true)}
                    data-entity-delete=""
                  >
                    <Trash2 size={12} aria-hidden /> 删除
                  </button>
                )}
                {confirmDelete && (
                  <span className="inline-flex items-center gap-1.5 rounded border border-[var(--err)]/40 px-2 py-1 text-2xs">
                    <span>确认删除此实体？</span>
                    <button
                      type="button"
                      className="btn btn-xs text-[var(--err)]"
                      disabled={saving}
                      onClick={() => void removeEntity()}
                      data-entity-delete-confirm=""
                    >
                      确认删除
                    </button>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={() => setConfirmDelete(false)}>
                      取消
                    </button>
                  </span>
                )}
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  disabled={saving || !draft.key.trim() || !draft.label.trim()}
                  onClick={() => void save()}
                  data-entity-save=""
                >
                  {saving ? '正在保存…' : '保存实体'}
                </button>
              </div>
            </div>

            {saveError != null && <ErrorState error={saveError} compact />}

            <div className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-dim">实体标识（英文小写与下划线）</span>
                <input
                  className="field mt-1 w-full font-mono"
                  placeholder="例如：production_line / sales_region"
                  disabled={!!draft.id || saving}
                  value={draft.key}
                  onChange={(e) => setDraft({ ...draft, key: e.target.value })}
                  data-entity-key-input=""
                />
              </label>
              <label className="block">
                <span className="text-dim">实体名称</span>
                <input
                  className="field mt-1 w-full"
                  placeholder="例如：生产产线 / 销售大区"
                  disabled={saving}
                  value={draft.label}
                  onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                  data-entity-label-input=""
                />
              </label>
              <label className="block sm:col-span-2">
                <span className="text-dim">常用叫法（逗号分隔）</span>
                <input
                  className="field mt-1 w-full"
                  placeholder="例如：产线，机台线，生产线"
                  disabled={saving}
                  value={draft.aliasesText}
                  onChange={(e) => setDraft({ ...draft, aliasesText: e.target.value })}
                />
              </label>
              <label className="block sm:col-span-2">
                <span className="text-dim">业务说明与对齐粒度</span>
                <textarea
                  className="field mt-1 w-full"
                  rows={2}
                  placeholder="说明该业务实体在各系统中的粒度定义与对齐注意事项"
                  disabled={saving}
                  value={draft.description}
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                />
              </label>
            </div>

            {/* 跨源物理表列绑定 */}
            <section className="space-y-2 rounded-lg border p-3" data-entity-bindings="">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-fg">跨源物理表列绑定 · {draft.bindings.length} 处</h3>
                  <p className="text-2xs text-faint">声明各数据源中代表同一业务实体的物理表与字段。</p>
                </div>
                <button
                  type="button"
                  className="btn btn-xs"
                  disabled={saving}
                  onClick={() => setDraft({
                    ...draft,
                    bindings: [
                      ...draft.bindings,
                      {
                        source_id: sourceId || null,
                        source_name: sourceName || (sources[0]?.name ?? ''),
                        table_name: '',
                        column_name: '',
                        status: 'confirmed',
                        source: 'human',
                        note: '',
                      },
                    ],
                  })}
                  data-entity-add-binding=""
                >
                  <Plus size={11} aria-hidden /> 添加表列绑定
                </button>
              </div>
              <datalist id="catalog-entity-tables">
                {tables.map((t) => <option key={t} value={t} />)}
              </datalist>
              {draft.bindings.length === 0 ? (
                <Notice tone="info">尚未添加物理表列绑定；可点击右上方「添加表列绑定」关联各数据源的表字段。</Notice>
              ) : (
                <div className="space-y-2">
                  {draft.bindings.map((b, idx) => (
                    <div
                      key={b.id ?? idx}
                      className="grid items-center gap-2 rounded border bg-elev p-2 sm:grid-cols-12"
                      data-entity-binding-row={b.id ?? String(idx)}
                    >
                      <div className="sm:col-span-3">
                        <input
                          className="field w-full"
                          list="catalog-entity-sources"
                          placeholder="数据源名称"
                          aria-label={`绑定 ${idx + 1} 数据源`}
                          value={b.source_name}
                          onChange={(e) => setDraft({
                            ...draft,
                            bindings: draft.bindings.map((x, i) => (i === idx ? { ...x, source_name: e.target.value } : x)),
                          })}
                        />
                      </div>
                      <div className="sm:col-span-3">
                        <input
                          className="field w-full font-mono"
                          list={b.source_name === sourceName ? 'catalog-entity-tables' : undefined}
                          placeholder="表名"
                          aria-label={`绑定 ${idx + 1} 表名`}
                          value={b.table_name}
                          onChange={(e) => setDraft({
                            ...draft,
                            bindings: draft.bindings.map((x, i) => (i === idx ? { ...x, table_name: e.target.value } : x)),
                          })}
                        />
                      </div>
                      <div className="sm:col-span-2">
                        <input
                          className="field w-full font-mono"
                          placeholder="列名"
                          aria-label={`绑定 ${idx + 1} 列名`}
                          value={b.column_name}
                          onChange={(e) => setDraft({
                            ...draft,
                            bindings: draft.bindings.map((x, i) => (i === idx ? { ...x, column_name: e.target.value } : x)),
                          })}
                        />
                      </div>
                      <div className="sm:col-span-2">
                        <input
                          className="field w-full"
                          placeholder="备注（可选）"
                          aria-label={`绑定 ${idx + 1} 备注`}
                          value={b.note ?? ''}
                          onChange={(e) => setDraft({
                            ...draft,
                            bindings: draft.bindings.map((x, i) => (i === idx ? { ...x, note: e.target.value } : x)),
                          })}
                        />
                      </div>
                      <div className="flex flex-wrap items-center justify-end gap-1 sm:col-span-2">
                        <StatusPill status={b.status} />
                        {draft.id && b.id && b.status === 'proposed' && (
                          <>
                            <button
                              type="button"
                              className="btn btn-xs text-[var(--ok)]"
                              title="确认该绑定"
                              disabled={saving}
                              onClick={() => void reviewItem('binding', b.id, 'confirmed')}
                              data-entity-confirm={b.id}
                            >
                              <Check size={11} aria-hidden /> 确认
                            </button>
                            <button
                              type="button"
                              className="btn btn-xs text-faint"
                              title="驳回该绑定"
                              disabled={saving}
                              onClick={() => void reviewItem('binding', b.id, 'rejected')}
                              data-entity-reject={b.id}
                            >
                              <X size={11} aria-hidden /> 驳回
                            </button>
                          </>
                        )}
                        <button
                          type="button"
                          className="btn btn-xs btn-ghost"
                          title="移除此行"
                          disabled={saving}
                          onClick={() => setDraft({
                            ...draft,
                            bindings: draft.bindings.filter((_, i) => i !== idx),
                          })}
                        >
                          移除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <datalist id="catalog-entity-sources">
                {sources.map((s) => <option key={s.id} value={s.name} />)}
              </datalist>
            </section>

            {/* 跨源成员与底层编码对齐 */}
            <section className="space-y-2 rounded-lg border p-3" data-entity-members="">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-fg">标准成员与跨源编码映射 · {draft.members.length} 项</h3>
                  <p className="text-2xs text-faint">
                    当不同系统对同一成员采用不同编码时，按「数据源名=编码」填写（多项以逗号分隔），跨源调用口径时自动精确换算。
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-xs"
                  disabled={saving}
                  onClick={() => setDraft({
                    ...draft,
                    members: [
                      ...draft.members,
                      { value: '', label: '', aliases: [], aliasesText: '', codesText: '' },
                    ],
                  })}
                  data-entity-add-member=""
                >
                  <Plus size={11} aria-hidden /> 添加成员映射
                </button>
              </div>
              {draft.members.length > 0 && (
                <div className="space-y-2">
                  {draft.members.map((m, idx) => (
                    <div key={idx} className="grid items-center gap-2 rounded border bg-elev p-2 sm:grid-cols-12">
                      <input
                        className="field sm:col-span-2 font-mono"
                        placeholder="标准值（如 L1）"
                        aria-label={`成员 ${idx + 1} 标准值`}
                        value={m.value}
                        onChange={(e) => setDraft({
                          ...draft,
                          members: draft.members.map((x, i) => (i === idx ? { ...x, value: e.target.value } : x)),
                        })}
                      />
                      <input
                        className="field sm:col-span-2"
                        placeholder="标准名称（如 一号产线）"
                        aria-label={`成员 ${idx + 1} 标准名称`}
                        value={m.label}
                        onChange={(e) => setDraft({
                          ...draft,
                          members: draft.members.map((x, i) => (i === idx ? { ...x, label: e.target.value } : x)),
                        })}
                      />
                      <input
                        className="field sm:col-span-3"
                        placeholder="别名（逗号分隔）"
                        aria-label={`成员 ${idx + 1} 别名`}
                        value={m.aliasesText}
                        onChange={(e) => setDraft({
                          ...draft,
                          members: draft.members.map((x, i) => (i === idx ? { ...x, aliasesText: e.target.value } : x)),
                        })}
                      />
                      <input
                        className="field sm:col-span-4 font-mono"
                        placeholder="数据源名=编码（如 mes_db=LINE_01，erp_db=PL01）"
                        aria-label={`成员 ${idx + 1} 跨源编码`}
                        value={m.codesText}
                        onChange={(e) => setDraft({
                          ...draft,
                          members: draft.members.map((x, i) => (i === idx ? { ...x, codesText: e.target.value } : x)),
                        })}
                      />
                      <div className="flex justify-end sm:col-span-1">
                        <button
                          type="button"
                          className="btn btn-xs btn-ghost"
                          onClick={() => setDraft({
                            ...draft,
                            members: draft.members.filter((_, i) => i !== idx),
                          })}
                        >
                          移除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        )}
      </section>

      {assistantOpen && (
        <EntityAssistant
          currentSourceId={sourceId}
          currentSourceName={sourceName}
          sources={sources}
          focusEntityKey={draft?.id ? draft.key : null}
          focusEntityLabel={draft?.id ? draft.label : null}
          onApplied={handleAssistantApplied}
          onClose={() => setAssistantOpen(false)}
        />
      )}

      <EntityDiscoverDialog
        open={discoverOpen}
        onClose={() => setDiscoverOpen(false)}
        sources={sources}
        defaultSourceId={sourceId}
        onStarted={(job) => {
          setFinishedBanner(null)
          setActiveJob(job)
        }}
      />

      <DiscoveryProgressModal
        open={progressDetailOpen && Boolean(activeJob || finishedBanner)}
        job={activeJob ?? finishedBanner}
        nowTick={nowTick}
        cancelling={cancellingJob}
        onCancel={() => void cancelDiscovery()}
        onPickEntity={(entityId) => {
          const target = items.find((x) => x.id === entityId)
          if (target) selectEntity(target)
          setProgressDetailOpen(false)
        }}
        onClose={() => setProgressDetailOpen(false)}
      />
    </div>
  )
}

const DISCOVERY_STEPS: { index: number; title: string }[] = [
  { index: 1, title: '读取所选数据源表结构、数据目录与关联口径入参' },
  { index: 2, title: '解析挂载的业务文档、数据字典与主数据对照表分段' },
  { index: 3, title: '调用起草模型流式识别标准业务实体、跨源绑定与编码映射' },
  { index: 4, title: '核验物理表列与口径参数真实性，写入「待确认」候选列表' },
]

function DiscoveryProgressModal({
  open,
  job,
  nowTick,
  cancelling,
  onCancel,
  onPickEntity,
  onClose,
}: {
  open: boolean
  job: EntityDiscoveryJob | null
  nowTick: number
  cancelling: boolean
  onCancel: () => void
  onPickEntity: (entityId: string) => void
  onClose: () => void
}) {
  if (!job) return null
  const isRunning = job.status === 'queued' || job.status === 'running'
  const sum = job.summary ?? {}
  const stepIndex = job.status === 'succeeded' ? 5 : (sum.step_index ?? (job.status === 'queued' ? 1 : 2))
  const pct = job.status === 'succeeded' ? 100 : Math.max(6, Math.min(99, sum.progress_pct ?? 20))
  const startMs = parseUtcMs(job.started_at || job.created_at)
  const endMs = job.finished_at ? parseUtcMs(job.finished_at) : nowTick
  const durationSec = Number.isFinite(startMs) && Number.isFinite(endMs)
    ? Math.max(0, Math.floor((endMs - startMs) / 1000))
    : null
  const touched = sum.touched_entities ?? []
  const candKeys = sum.candidate_keys ?? []
  const skipped = sum.skipped_reasons ?? []

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={isRunning ? '候选业务实体扫描进度' : '候选业务实体扫描明细'}
      width={560}
      footer={(
        <div className="flex items-center justify-end gap-2">
          {isRunning && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={cancelling}
              onClick={onCancel}
            >
              <Square size={11} aria-hidden /> 停止扫描
            </button>
          )}
          <button type="button" className="btn btn-sm btn-primary" onClick={onClose}>
            关闭
          </button>
        </div>
      )}
    >
      <div className="space-y-4 text-xs" data-entity-discover-progress-modal={job.status}>
        {/* 进度条与当前阶段 */}
        <div className="space-y-1.5 rounded-lg border bg-bg p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-fg">{job.phase || '正在扫描…'}</span>
            <span className="tnum text-2xs text-faint">
              {pct}%{durationSec != null ? ` · 用时 ${formatDurationSec(durationSec)}` : ''}
            </span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-hover">
            <div
              className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>
          {job.error && (
            <p className="pt-1 text-2xs text-[var(--err)]">{job.error}</p>
          )}
        </div>

        {/* 四步执行状态 */}
        <section className="space-y-1.5">
          <div className="font-medium text-fg">执行阶段</div>
          <ol className="space-y-1.5 rounded-lg border p-2.5 text-2xs">
            {DISCOVERY_STEPS.map((st) => {
              const done = stepIndex > st.index
              const active = isRunning && stepIndex === st.index
              return (
                <li key={st.index} className="flex items-center gap-2">
                  {done ? (
                    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[var(--ok)]/15 text-[var(--ok)]">
                      <Check size={10} aria-hidden />
                    </span>
                  ) : active ? (
                    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center">
                      <Spinner size={11} />
                    </span>
                  ) : (
                    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-faint">
                      {st.index}
                    </span>
                  )}
                  <span className={clsx(done || active ? 'text-fg' : 'text-faint', active && 'font-medium')}>
                    {st.title}
                  </span>
                </li>
              )
            })}
          </ol>
        </section>

        {/* 量化范围与产出统计 */}
        <section className="grid grid-cols-2 gap-2 rounded-lg border p-2.5 text-2xs sm:grid-cols-3">
          <div>
            <div className="text-faint">参与数据源</div>
            <div className="mt-0.5 font-medium text-fg">
              {sum.sources_scanned ?? 0} 个
              {(sum.source_names ?? []).length > 0 && (
                <span className="ml-1 font-mono text-faint">（{(sum.source_names ?? []).join('、')}）</span>
              )}
            </div>
          </div>
          <div>
            <div className="text-faint">扫描物理表 / 列</div>
            <div className="mt-0.5 font-medium text-fg">
              {sum.tables_scanned ?? 0} 张表 / {sum.columns_scanned ?? 0} 列
            </div>
          </div>
          <div>
            <div className="text-faint">关联口径工作流</div>
            <div className="mt-0.5 font-medium text-fg">{sum.workflows_scanned ?? 0} 个</div>
          </div>
          <div>
            <div className="text-faint">业务文档 / 分段</div>
            <div className="mt-0.5 font-medium text-fg">
              {sum.documents_scanned ?? 0} 份 / {sum.chunks_scanned ?? 0} 段
            </div>
          </div>
          <div>
            <div className="text-faint">模型输出进度</div>
            <div className="mt-0.5 font-medium text-fg">
              {sum.model_chars ?? 0} 字 · 识别 {sum.ops_detected ?? 0} 项操作
            </div>
          </div>
          <div>
            <div className="text-faint">落库待确认项</div>
            <div className="mt-0.5 font-medium text-fg">
              绑定 {sum.proposed_bindings ?? 0} 处 · 成员 {sum.added_members ?? 0} 项
            </div>
          </div>
        </section>

        {/* 识别到的实体列表 */}
        {touched.length > 0 ? (
          <section className="space-y-1.5">
            <div className="font-medium text-fg">本次新增或更新的实体（点击跳转审阅）</div>
            <div className="flex flex-wrap gap-1.5">
              {touched.map((ent) => (
                <button
                  key={ent.id}
                  type="button"
                  className="btn btn-xs"
                  onClick={() => onPickEntity(ent.id)}
                  data-discover-touched-entity={ent.key}
                >
                  <span>{ent.label}</span>
                  <span className="font-mono text-faint">（{ent.key}）</span>
                </button>
              ))}
            </div>
          </section>
        ) : candKeys.length > 0 ? (
          <section className="space-y-1.5">
            <div className="font-medium text-fg">实时识别到的候选实体标识</div>
            <div className="flex flex-wrap gap-1.5 text-2xs">
              {candKeys.map((k) => (
                <span key={k} className="rounded border bg-panel px-2 py-0.5 font-mono text-fg">
                  {k}
                </span>
              ))}
            </div>
          </section>
        ) : null}

        {/* 平台核验过滤原因 */}
        {skipped.length > 0 && (
          <section className="space-y-1">
            <div className="font-medium text-fg">未通过真实表列核验已自动过滤（{skipped.length} 项）</div>
            <ul className="max-h-28 list-disc space-y-0.5 overflow-y-auto rounded border bg-bg py-1.5 pr-2 pl-5 text-2xs text-faint">
              {skipped.map((reason, idx) => (
                <li key={idx}>{reason}</li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </Modal>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, Plus, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { ErrorState, Notice, Spinner, toast } from '../../components/ui'
import type {
  CaliberDraft,
  CanonicalEntity,
  DraftMetric,
  MetricOntologyLink,
  OntologyFactorRole,
  OntologyGraph,
  OntologyLinkKind,
  OntologyStatus,
  WorkflowOntology,
} from '../../typesLedger'

const LINK_KIND_LABEL: Record<OntologyLinkKind, string> = {
  factor: '构成因子',
  reconciles_with: '跨源镜像对账',
  upstream_driver: '跨源上游驱动',
}

const FACTOR_ROLE_LABEL: Record<OntologyFactorRole, string> = {
  numerator: '分子',
  denominator: '分母',
  multiplier: '乘数',
  addend: '加项',
  subtrahend: '减项',
  component: '组成部分',
}

const STATUS_LABEL: Record<OntologyStatus, string> = {
  confirmed: '已确认',
  verified: '已验证',
  proposed: '待确认',
  rejected: '已驳回',
}

function OntologyTopologySvg({
  metrics,
  workflowOntology,
  publishedNames,
}: {
  metrics: DraftMetric[]
  workflowOntology: WorkflowOntology | null
  publishedNames: Map<string, string>
}) {
  const leftNodes: { id: string; label: string; sub: string }[] = []
  const rightNodes: { id: string; label: string; sub: string }[] = []
  const edges: {
    fromId: string
    toId: string
    kind: OntologyLinkKind | 'lineage'
    status: OntologyStatus
    label: string
  }[] = []

  const leftSeen = new Set<string>()
  const rightSeen = new Set<string>()

  const centerNodes = metrics
    .filter((m) => m.id)
    .map((m) => ({ id: `self:${m.id!}`, metricId: m.id!, label: m.name || m.id! }))

  const lineageMap = new Map<string, { source: string; tables: string[] }[]>()
  for (const card of workflowOntology?.cards ?? []) {
    for (const m of card.metrics ?? []) {
      lineageMap.set(m.metric_id, m.lineage ?? [])
    }
  }

  for (const m of metrics) {
    if (!m.id) continue
    const selfId = `self:${m.id}`
    for (const link of m.links ?? []) {
      if (link.status === 'rejected') continue
      const tKey = `${link.target.workflow_id || 'self'}:${link.target.card_id}:${link.target.metric_id}`
      const targetName = publishedNames.get(tKey)
        ?? metrics.find((x) => x.id === link.target.metric_id)?.name
        ?? link.target.metric_id
      const sub = link.target.workflow_id ? `${link.target.workflow_id.slice(0, 8)} · ${link.target.card_id}` : '当前口径卡'
      if (link.kind === 'factor') {
        const nodeId = `right:${tKey}`
        if (!rightSeen.has(nodeId)) {
          rightSeen.add(nodeId)
          rightNodes.push({ id: nodeId, label: targetName, sub })
        }
        edges.push({
          fromId: selfId,
          toId: nodeId,
          kind: 'factor',
          status: link.status ?? 'confirmed',
          label: link.role ? FACTOR_ROLE_LABEL[link.role] : '构成因子',
        })
      } else {
        const nodeId = `left:${tKey}`
        if (!leftSeen.has(nodeId)) {
          leftSeen.add(nodeId)
          leftNodes.push({ id: nodeId, label: targetName, sub })
        }
        edges.push({
          fromId: nodeId,
          toId: selfId,
          kind: link.kind,
          status: link.status ?? 'confirmed',
          label: LINK_KIND_LABEL[link.kind],
        })
      }
    }
    for (const lin of lineageMap.get(m.id) ?? []) {
      const tableText = (lin.tables ?? []).join('、') || '取数表'
      const nodeId = `table:${lin.source}:${tableText}`
      if (!rightSeen.has(nodeId)) {
        rightSeen.add(nodeId)
        rightNodes.push({ id: nodeId, label: tableText, sub: `数据源：${lin.source}` })
      }
      edges.push({
        fromId: selfId,
        toId: nodeId,
        kind: 'lineage',
        status: 'verified',
        label: '血缘',
      })
    }
  }

  const maxRows = Math.max(leftNodes.length, centerNodes.length, rightNodes.length, 1)
  const rowH = 54
  const height = Math.max(150, maxRows * rowH + 36)
  const width = 680

  const posMap = new Map<string, { x: number; y: number; w: number; h: number }>()
  const placeCol = (list: { id: string }[], x: number) => {
    const startY = Math.max(18, (height - list.length * rowH) / 2)
    list.forEach((n, idx) => {
      posMap.set(n.id, { x, y: startY + idx * rowH, w: 176, h: 38 })
    })
  }
  placeCol(leftNodes, 16)
  placeCol(centerNodes, 252)
  placeCol(rightNodes, 488)

  if (centerNodes.length === 0) return null

  return (
    <div className="space-y-1.5 rounded border bg-panel p-2.5" data-ontology-topology="">
      <div className="flex flex-wrap items-center justify-between gap-2 text-2xs text-faint">
        <span className="font-semibold text-dim">一跳本体拓扑与反向表血缘预览</span>
        <span className="flex flex-wrap items-center gap-3">
          <span>实线：已确认 / 已验证</span>
          <span className="text-[var(--warn)]">虚线：建议态（待确认，不参与自动穿透）</span>
        </span>
      </div>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="h-auto w-full min-w-[540px]"
          role="img"
          aria-label="指标本体一跳拓扑图"
          data-ontology-svg=""
        >
          {edges.map((edge, idx) => {
            const a = posMap.get(edge.fromId)
            const b = posMap.get(edge.toId)
            if (!a || !b) return null
            const x1 = a.x + a.w
            const y1 = a.y + a.h / 2
            const x2 = b.x
            const y2 = b.y + b.h / 2
            const mx = (x1 + x2) / 2
            const stroke = edge.status === 'proposed'
              ? 'var(--warn)'
              : edge.kind === 'lineage'
                ? 'var(--border-strong)'
                : 'var(--accent)'
            return (
              <g key={idx}>
                <path
                  d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={1.5}
                  strokeDasharray={edge.status === 'proposed' ? '4 3' : undefined}
                />
                <text
                  x={mx}
                  y={(y1 + y2) / 2 - 4}
                  textAnchor="middle"
                  fontSize="9"
                  fill="currentColor"
                  opacity={0.72}
                >
                  {edge.label}
                </text>
              </g>
            )
          })}
          {[...leftNodes.map((n) => ({ ...n, role: 'left' as const })),
            ...centerNodes.map((n) => ({ id: n.id, label: n.label, sub: '本卡指标', role: 'center' as const })),
            ...rightNodes.map((n) => ({ ...n, role: 'right' as const }))].map((node) => {
            const p = posMap.get(node.id)
            if (!p) return null
            return (
              <g key={node.id} transform={`translate(${p.x}, ${p.y})`}>
                <rect
                  width={p.w}
                  height={p.h}
                  rx={6}
                  fill="var(--bg-elev)"
                  stroke={node.role === 'center' ? 'var(--accent)' : 'var(--border)'}
                  strokeWidth={node.role === 'center' ? 1.5 : 1}
                />
                <text x={8} y={16} fontSize="11" fontWeight={node.role === 'center' ? 600 : 500} fill="currentColor">
                  {node.label.length > 14 ? `${node.label.slice(0, 14)}…` : node.label}
                </text>
                <text x={8} y={30} fontSize="9" fill="currentColor" opacity={0.6}>
                  {node.sub.length > 22 ? `${node.sub.slice(0, 22)}…` : node.sub}
                </text>
              </g>
            )
          })}
        </svg>
      </div>
    </div>
  )
}

export function OntologyPanel({
  draft,
  onSaved,
}: {
  draft: CaliberDraft
  onSaved: (fresh: CaliberDraft) => void
}) {
  const metrics = useMemo(() => (draft.metrics ?? []).filter((m) => !!m.id), [draft.metrics])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [entities, setEntities] = useState<CanonicalEntity[]>([])
  const [graph, setGraph] = useState<OntologyGraph | null>(null)
  const [workflowOntology, setWorkflowOntology] = useState<WorkflowOntology | null>(null)

  const [selectedMetricId, setSelectedMetricId] = useState<string>(metrics[0]?.id ?? '')
  const [kind, setKind] = useState<OntologyLinkKind>('reconciles_with')
  const [pickedTarget, setPickedTarget] = useState('')
  const [targetWorkflowId, setTargetWorkflowId] = useState('')
  const [targetCardId, setTargetCardId] = useState('')
  const [targetMetricId, setTargetMetricId] = useState('')
  const [role, setRole] = useState<OntologyFactorRole>('component')
  const [entityKey, setEntityKey] = useState('')
  const [lagPeriods, setLagPeriods] = useState('0')
  const [absTol, setAbsTol] = useState('')
  const [relTol, setRelTol] = useState('0.02')
  const [rationale, setRationale] = useState('')

  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (!metrics.some((m) => m.id === selectedMetricId)) {
      setSelectedMetricId(metrics[0]?.id ?? '')
    }
  }, [metrics, selectedMetricId])

  const loadContext = useCallback(async () => {
    try {
      const [entRes, graphRes, wfRes] = await Promise.all([
        api.ontology.listEntities().catch(() => ({ items: [] as CanonicalEntity[] })),
        api.ontology.graph().catch(() => null),
        draft.workflow_id ? api.ontology.workflow(draft.workflow_id).catch(() => null) : Promise.resolve(null),
      ])
      if (!mounted.current) return
      setEntities(entRes.items ?? [])
      setGraph(graphRes)
      setWorkflowOntology(wfRes)
    } catch {
      // 辅助拓扑信息读取失败不阻塞编辑
    }
  }, [draft.workflow_id])

  useEffect(() => {
    if (open) void loadContext()
  }, [open, loadContext, draft.revision])

  const publishedOptions = useMemo(() => {
    const out: { key: string; workflow_id: string; card_id: string; metric_id: string; label: string }[] = []
    if (draft.card_id) {
      for (const m of metrics) {
        if (!m.id) continue
        out.push({
          key: `${draft.workflow_id ?? ''}:${draft.card_id}:${m.id}`,
          workflow_id: draft.workflow_id ?? '',
          card_id: draft.card_id,
          metric_id: m.id,
          label: `当前口径卡 · ${m.name}（${m.id}）`,
        })
      }
    }
    for (const card of graph?.cards ?? []) {
      if (card.workflow_id === draft.workflow_id && card.card_id === draft.card_id) continue
      for (const m of card.metrics ?? []) {
        const key = `${card.workflow_id}:${card.card_id}:${m.metric_id}`
        out.push({
          key,
          workflow_id: card.workflow_id,
          card_id: card.card_id,
          metric_id: m.metric_id,
          label: `${card.caliber || card.workflow_name} v${card.version} · ${m.name}（${m.metric_id}）`,
        })
      }
    }
    return out
  }, [graph, metrics, draft.workflow_id, draft.card_id])

  const publishedNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const opt of publishedOptions) {
      map.set(`${opt.workflow_id || 'self'}:${opt.card_id}:${opt.metric_id}`, opt.label.split(' · ').pop() ?? opt.metric_id)
    }
    return map
  }, [publishedOptions])

  if (!metrics.length || !draft.card_id || draft.revision == null) return null

  const totalLinks = metrics.reduce((acc, m) => acc + (m.links?.filter((l) => l.status !== 'rejected').length ?? 0), 0)
  const pendingLinks = metrics.reduce((acc, m) => acc + (m.links?.filter((l) => l.status === 'proposed').length ?? 0), 0)

  const patchMetricLinks = async (metricId: string, nextLinks: MetricOntologyLink[], toastMsg: string) => {
    setBusy(true)
    setError(null)
    try {
      const reqId = globalThis.crypto?.randomUUID?.() ?? `ontology-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations: [{ op: 'set', metric_id: metricId, path: '/links', value: nextLinks }],
        expected_revision: draft.revision!,
        request_id: reqId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id! },
      })
      if (!mounted.current) return
      onSaved(fresh)
      toast.ok(toastMsg)
      void loadContext()
    } catch (e) {
      if (mounted.current) setError(e)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const addLink = async () => {
    const metric = metrics.find((m) => m.id === selectedMetricId)
    if (!metric?.id) return
    const newLink: MetricOntologyLink = {
      kind,
      target: {
        workflow_id: targetWorkflowId.trim(),
        card_id: targetCardId.trim(),
        metric_id: targetMetricId.trim(),
      },
      status: 'confirmed',
      source: 'human',
      ...(kind === 'factor' ? { role } : {}),
      ...(entityKey ? { entity_key: entityKey } : {}),
      ...(kind === 'upstream_driver' && Number(lagPeriods) > 0 ? { lag_periods: Number(lagPeriods) } : {}),
      ...(kind === 'reconciles_with' && absTol.trim() !== '' ? { abs_tol: Number(absTol) } : {}),
      ...(kind === 'reconciles_with' && relTol.trim() !== '' ? { rel_tol: Number(relTol) } : {}),
      ...(rationale.trim() ? { rationale: rationale.trim() } : {}),
    }
    const existing = metric.links ?? []
    await patchMetricLinks(metric.id, [...existing, newLink], '指标本体关系已保存；验证并发布该版本后生效')
    setRationale('')
  }

  const updateLinkStatus = async (metric: DraftMetric, linkIdx: number, status: OntologyStatus) => {
    if (!metric.id) return
    const next = (metric.links ?? []).map((item, i) => (i === linkIdx ? { ...item, status } : item))
    await patchMetricLinks(
      metric.id,
      next,
      status === 'confirmed' ? '已确认该本体关系，发布后可参与跨源穿透' : '已驳回该候选本体关系',
    )
  }

  const removeLink = async (metric: DraftMetric, linkIdx: number) => {
    if (!metric.id) return
    const next = (metric.links ?? []).filter((_, i) => i !== linkIdx)
    await patchMetricLinks(metric.id, next, '已移除该本体关系')
  }

  const handlePickTarget = (val: string) => {
    setPickedTarget(val)
    const found = publishedOptions.find((o) => o.key === val)
    if (found) {
      setTargetWorkflowId(found.workflow_id)
      setTargetCardId(found.card_id)
      setTargetMetricId(found.metric_id)
    }
  }

  return (
    <details
      className="rounded border p-2 text-xs"
      data-ontology-panel=""
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer font-semibold">
        业务本体与跨源关系
        {totalLinks > 0 ? ` · 已声明 ${totalLinks} 条` : '（可选配置）'}
        {pendingLinks > 0 ? ` · 待确认 ${pendingLinks} 条` : ''}
      </summary>
      <div className="mt-3 space-y-3">
        <p className="text-faint">
          声明当前口径指标与其他口径指标之间的构成因子、跨源镜像对账或上游业务驱动关系。问数遇到异常时，仅沿「已确认」或「已验证」的关系自动穿透排查。
        </p>

        <OntologyTopologySvg
          metrics={metrics}
          workflowOntology={workflowOntology}
          publishedNames={publishedNames}
        />

        {!!workflowOntology?.issues?.length && (
          <Notice tone="warn">
            {workflowOntology.issues.map((iss, i) => (
              <p key={i}>{iss.message}</p>
            ))}
          </Notice>
        )}

        {/* 已有本体关系列表 */}
        <div className="space-y-2" data-ontology-links-list="">
          {metrics.map((metric) => {
            const links = metric.links ?? []
            const lineage = workflowOntology?.cards
              ?.flatMap((c) => c.metrics)
              .find((m) => m.metric_id === metric.id)?.lineage ?? []
            return (
              <div key={metric.id} className="rounded border bg-elev p-2.5" data-ontology-metric={metric.id}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium text-fg">{metric.name}（{metric.id}）</span>
                  {lineage.length > 0 && (
                    <span className="text-2xs text-faint" data-ontology-lineage={metric.id}>
                      反向表血缘：{lineage.map((l) => `${l.source}（${l.tables.join('、')}）`).join('；')}
                    </span>
                  )}
                </div>
                {links.length === 0 ? (
                  <p className="mt-1 text-2xs text-faint">尚未声明跨指标本体关系。</p>
                ) : (
                  <ul className="mt-2 space-y-1.5">
                    {links.map((link, idx) => {
                      const st = link.status ?? 'confirmed'
                      const targetText = `${link.target.workflow_id ? `${link.target.workflow_id.slice(0, 8)} / ` : ''}${link.target.card_id} / ${link.target.metric_id}`
                      return (
                        <li
                          key={link.id ?? idx}
                          className="flex flex-wrap items-center justify-between gap-2 rounded border bg-panel px-2 py-1.5 text-2xs"
                          data-ontology-link-row={link.id ?? String(idx)}
                        >
                          <div className="space-x-1.5">
                            <span className="font-semibold text-fg">{LINK_KIND_LABEL[link.kind] ?? link.kind}</span>
                            {link.role && <span className="text-dim">（{FACTOR_ROLE_LABEL[link.role] ?? link.role}）</span>}
                            <span className="font-mono text-dim">→ {targetText}</span>
                            {link.entity_key && <span className="text-faint">· 对齐实体：{link.entity_key}</span>}
                            {link.rel_tol != null && <span className="text-faint">· 相对容差：{link.rel_tol}</span>}
                            {link.abs_tol != null && <span className="text-faint">· 绝对容差：{link.abs_tol}</span>}
                            {!!link.lag_periods && <span className="text-faint">· 滞后 {link.lag_periods} 期</span>}
                            {link.rationale && <span className="text-faint">· {link.rationale}</span>}
                          </div>
                          <div className="flex items-center gap-1">
                            <span
                              className={clsx(
                                'rounded border px-1.5 py-0.5',
                                st === 'confirmed' || st === 'verified'
                                  ? 'border-[var(--ok)]/30 text-[var(--ok)]'
                                  : st === 'proposed'
                                    ? 'border-[var(--warn)]/40 text-[var(--warn)]'
                                    : 'border-[var(--border)] text-faint',
                              )}
                            >
                              {STATUS_LABEL[st] ?? st}
                            </span>
                            {st === 'proposed' && (
                              <>
                                <button
                                  type="button"
                                  className="btn btn-xs text-[var(--ok)]"
                                  disabled={busy}
                                  onClick={() => void updateLinkStatus(metric, idx, 'confirmed')}
                                  data-ontology-link-confirm={link.id ?? String(idx)}
                                >
                                  <Check size={11} aria-hidden /> 确认
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-xs text-faint"
                                  disabled={busy}
                                  onClick={() => void updateLinkStatus(metric, idx, 'rejected')}
                                  data-ontology-link-reject={link.id ?? String(idx)}
                                >
                                  <X size={11} aria-hidden /> 驳回
                                </button>
                              </>
                            )}
                            <button
                              type="button"
                              className="btn btn-xs btn-ghost"
                              disabled={busy}
                              onClick={() => void removeLink(metric, idx)}
                            >
                              移除
                            </button>
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            )
          })}
        </div>

        {/* 新增本体关系表单 */}
        <fieldset disabled={busy} className="space-y-2 rounded border p-2.5" data-ontology-add-form="">
          <legend className="px-1 text-2xs font-semibold text-dim">添加指标本体关系</legend>
          <div className="grid gap-2 sm:grid-cols-3">
            <label className="block">
              <span className="text-2xs text-dim">源指标</span>
              <select
                className="field mt-0.5 w-full"
                value={selectedMetricId}
                onChange={(e) => setSelectedMetricId(e.target.value)}
                data-ontology-source-metric=""
              >
                {metrics.map((m) => (
                  <option key={m.id} value={m.id!}>{m.name}（{m.id}）</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-2xs text-dim">关系类型</span>
              <select
                className="field mt-0.5 w-full"
                value={kind}
                onChange={(e) => setKind(e.target.value as OntologyLinkKind)}
                data-ontology-kind-select=""
              >
                <option value="reconciles_with">跨源镜像对账</option>
                <option value="factor">构成因子（公式拆解）</option>
                <option value="upstream_driver">跨源上游驱动</option>
              </select>
            </label>
            <label className="block">
              <span className="text-2xs text-dim">从已发布口径快速选择目标</span>
              <select
                className="field mt-0.5 w-full"
                value={pickedTarget}
                onChange={(e) => handlePickTarget(e.target.value)}
                data-ontology-target-picker=""
              >
                <option value="">手动输入或选择已发布指标…</option>
                {publishedOptions.map((opt) => (
                  <option key={opt.key} value={opt.key}>{opt.label}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            <label className="block">
              <span className="text-2xs text-dim">目标工作流标识（本卡可留空）</span>
              <input
                className="field mt-0.5 w-full font-mono"
                placeholder="例如：wf_erp_settlement"
                value={targetWorkflowId}
                onChange={(e) => setTargetWorkflowId(e.target.value)}
                data-ontology-target-workflow=""
              />
            </label>
            <label className="block">
              <span className="text-2xs text-dim">目标口径卡节点标识</span>
              <input
                className="field mt-0.5 w-full font-mono"
                placeholder="例如：metrics_card"
                value={targetCardId}
                onChange={(e) => setTargetCardId(e.target.value)}
                data-ontology-target-card=""
              />
            </label>
            <label className="block">
              <span className="text-2xs text-dim">目标指标标识</span>
              <input
                className="field mt-0.5 w-full font-mono"
                placeholder="例如：settled_gmv"
                value={targetMetricId}
                onChange={(e) => setTargetMetricId(e.target.value)}
                data-ontology-target-metric=""
              />
            </label>
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            {kind === 'factor' && (
              <label className="block">
                <span className="text-2xs text-dim">因子角色</span>
                <select
                  className="field mt-0.5 w-full"
                  value={role}
                  onChange={(e) => setRole(e.target.value as OntologyFactorRole)}
                >
                  {(Object.keys(FACTOR_ROLE_LABEL) as OntologyFactorRole[]).map((r) => (
                    <option key={r} value={r}>{FACTOR_ROLE_LABEL[r]}</option>
                  ))}
                </select>
              </label>
            )}
            {kind !== 'factor' && (
              <label className="block">
                <span className="text-2xs text-dim">跨源对齐标准实体</span>
                <select
                  className="field mt-0.5 w-full"
                  value={entityKey}
                  onChange={(e) => setEntityKey(e.target.value)}
                  data-ontology-entity-select=""
                >
                  <option value="">不限定或按全局汇总对齐</option>
                  {entities.map((ent) => (
                    <option key={ent.key} value={ent.key}>{ent.label}（{ent.key}）</option>
                  ))}
                </select>
              </label>
            )}
            {kind === 'reconciles_with' && (
              <>
                <label className="block">
                  <span className="text-2xs text-dim">相对容差（如 0.02 表示 2%）</span>
                  <input
                    className="field mt-0.5 w-full font-mono"
                    value={relTol}
                    onChange={(e) => setRelTol(e.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="text-2xs text-dim">绝对容差（可选）</span>
                  <input
                    className="field mt-0.5 w-full font-mono"
                    placeholder="例如：100"
                    value={absTol}
                    onChange={(e) => setAbsTol(e.target.value)}
                  />
                </label>
              </>
            )}
            {kind === 'upstream_driver' && (
              <label className="block">
                <span className="text-2xs text-dim">滞后期数（0–12）</span>
                <input
                  className="field mt-0.5 w-full font-mono"
                  value={lagPeriods}
                  onChange={(e) => setLagPeriods(e.target.value)}
                />
              </label>
            )}
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-[220px] flex-1">
              <span className="text-2xs text-dim">业务依据说明（可选）</span>
              <input
                className="field mt-0.5 w-full"
                placeholder="说明这两个指标为何应当对账一致或存在上游驱动关系"
                value={rationale}
                onChange={(e) => setRationale(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={busy || !selectedMetricId || !targetCardId.trim() || !targetMetricId.trim()}
              onClick={() => void addLink()}
              data-ontology-add-submit=""
            >
              {busy ? <Spinner size={11} /> : <Plus size={11} aria-hidden />} 添加并保存关系
            </button>
          </div>
        </fieldset>

        {error != null && <ErrorState error={error} compact />}
      </div>
    </details>
  )
}

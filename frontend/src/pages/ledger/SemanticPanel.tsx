import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { ErrorState, Notice, toast } from '../../components/ui'
import type { DataSourceName } from '../../types'
import type { CaliberDraft, CanonicalEntity, DraftMetric, QuerySemantics } from '../../typesLedger'

const words = (value: string) => value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean)

function AliasesInput({ values, onChange, className = 'field' }: {
  values: string[]; onChange: (values: string[]) => void; className?: string
}) {
  const serialized = values.join('，')
  const [text, setText] = useState(serialized)
  useEffect(() => setText(serialized), [serialized])
  return <input className={className} aria-label="常用叫法（逗号分隔）" placeholder="常用叫法（逗号分隔）"
    value={text} onChange={e => setText(e.target.value)} onBlur={() => onChange(words(text))} />
}

const isSyntheticUnresolved = (text: string) =>
  text.includes('尚未生成可用实现，请补充字段映射或确认口径')
  || (text.startsWith('另有 ') && text.includes('项指标尚未生成可用实现'))

function resolveItemDiagnostic(
  draft: CaliberDraft,
  item: { id: string; name: string },
  isImplemented: boolean,
): { kind: 'needs_info' | 'validation_failed'; reasons: string[]; questions: string[] } | null {
  if (isImplemented) return null
  const direct = draft.definition_diagnostics?.[item.id]
  if (direct && ((direct.reasons?.length ?? 0) > 0 || (direct.questions?.length ?? 0) > 0)) {
    const reasons = Array.from(new Set(direct.reasons ?? []))
    const questions = Array.from(new Set(direct.questions ?? []))
    return {
      kind: direct.kind === 'validation_failed' || (reasons.length > 0 && questions.length === 0)
        ? 'validation_failed' : 'needs_info',
      reasons,
      questions,
    }
  }
  const dropped = draft.dropped ?? []
  const matchedReasons = Array.from(new Set(
    dropped
      .filter(d => d.reason && (d.name === item.name || (item.name && d.name.includes(item.name))))
      .map(d => d.reason),
  ))
  const sharedReasons = matchedReasons.length > 0 ? [] : Array.from(new Set(
    dropped
      .filter(d => d.reason && (d.name === 'sql' || d.name === 'metrics' || d.name === '重写这一组')
        && d.reason !== '没有一条指标通过校验')
      .map(d => d.reason),
  )).slice(0, 2)
  const reasons = matchedReasons.length > 0 ? matchedReasons : sharedReasons
  const questions = Array.from(new Set(
    (draft.open_questions ?? []).filter(q => !isSyntheticUnresolved(q) && item.name && q.includes(item.name)),
  ))
  if (!reasons.length && !questions.length) return null
  return {
    kind: reasons.length > 0 && questions.length === 0 ? 'validation_failed' : 'needs_info',
    reasons: reasons.slice(0, 4),
    questions: questions.slice(0, 4),
  }
}

export function DefinitionInventory({ draft, sources, onStarted }: {
  draft: CaliberDraft; sources: DataSourceName[]; onStarted: (draftId: string) => void
}) {
  const [picked, setPicked] = useState<string[]>(draft.selected_definition_ids ?? [])
  const [partial, setPartial] = useState(false)
  const [source, setSource] = useState(draft.datasource_id ?? '')
  const [filter, setFilter] = useState<'all' | 'unresolved' | 'implemented' | 'excluded'>('all')
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [error, setError] = useState<unknown>(null)
  useEffect(() => {
    const available = sources.filter(s => s.enabled !== false)
    if (!draft.datasource_id && !source && available.length === 1) setSource(available[0].id)
  }, [sources, source, draft.datasource_id])
  const catalog = draft.semantic_inventory
  if (!catalog) return null
  const canStart = draft.status === 'path_pending' && !draft.workflow_id
  const implemented = new Set((draft.metrics ?? []).flatMap(m => [m.name, m.source_definition_id]))
  const metrics = catalog.items.filter(i => i.kind === 'metric')
  const eligible = metrics.filter(i => i.status !== 'unavailable')
  const counts = new Map<string, number>()
  eligible.forEach(i => counts.set(i.name, (counts.get(i.name) ?? 0) + 1))
  const automatic = eligible.filter(i => counts.get(i.name) === 1)
  const selectedCount = partial ? picked.length : automatic.length
  const sourceAvailable = sources.some(s => s.id === source && s.enabled !== false)
  const progress = draft.implementation_progress
  const isItemImplemented = (item: typeof metrics[number]) =>
    implemented.has(item.id) || (counts.get(item.name) === 1 && implemented.has(item.name))
  const isItemExcluded = (item: typeof metrics[number]) =>
    item.status === 'unavailable'
    || (counts.get(item.name) ?? 0) > 1
    || (!!draft.selected_definition_ids?.length && !draft.selected_definition_ids.includes(item.id))
  const implementedItems = metrics.filter(isItemImplemented)
  const excludedItems = metrics.filter(i => !isItemImplemented(i) && isItemExcluded(i))
  const unresolvedItems = metrics.filter(i => !isItemImplemented(i) && !isItemExcluded(i))
  const filteredMetrics = canStart || filter === 'all' ? metrics
    : filter === 'unresolved' ? unresolvedItems
      : filter === 'implemented' ? implementedItems : excludedItems
  const COLLAPSE_LIMIT = 12
  const visibleMetrics = canStart || expanded || filteredMetrics.length <= COLLAPSE_LIMIT
    ? filteredMetrics
    : filteredMetrics.slice(0, COLLAPSE_LIMIT)
  const start = async () => {
    setBusy(true); setError(null)
    try {
      await api.caliberLedger.implementDefinitions(draft.id, { ...(partial ? { selected: picked } : {}), datasource_id: source })
      toast.ok('已开始自动处理；进度会持续保存，需要补充的信息会单独列出')
      if (mounted.current) onStarted(draft.id)
    } catch (e) { setError(e) } finally { setBusy(false) }
  }
  const noticeTone = !canStart && draft.status !== 'drafting' && progress && progress.completed === 0 && progress.blocked > 0
    ? 'warn' : 'info'
  const noticeText = canStart
    ? '默认自动处理全部可实现指标，保存进度并继续处理后续项；缺少来源或定义冲突的指标会单独列出。生成后仍需验证口径和计算结果。'
    : draft.status === 'drafting'
      ? '正在对照数据源分批生成取数实现并自动保存进度；未通过校验或缺少字段映射的指标会记录具体原因。'
      : progress && progress.completed === 0 && progress.blocked > 0
        ? `本轮尚未生成可用的取数实现（共 ${progress.blocked} 项待处理）。请查看下方「继续处理未完成指标」中的待补充要点，填写来源表名、字段映射或固定条件后继续生成；也可先勾选少量核心指标先行验证。`
        : progress && progress.blocked > 0
          ? `已生成 ${progress.completed} 项指标实现，仍有 ${progress.blocked} 项待补充信息或未通过校验。可在下方「继续处理未完成指标」补充说明后生成独立候选，核对无误再合并回原稿。`
          : '所选指标已生成取数实现；请在下方核对试运行结果、完成验证并发布。'
  return <section className="space-y-3 rounded-lg border p-3" data-semantic-inventory="">
    <h3 className="text-xs font-semibold">指标定义清单 · {metrics.length} 项</h3>
    <Notice tone={noticeTone}>{noticeText}</Notice>
    {progress && <div className="space-y-1 text-xs" role="status" data-implementation-progress="">
      <p>本次处理 {progress.total} 项：已生成 {progress.completed} 项，待处理 {progress.pending} 项，需补充信息 {progress.blocked} 项</p>
      <progress aria-label="指标实现进度" className="w-full" max={Math.max(1, progress.total)} value={progress.completed} />
      {draft.status === 'drafting' && !!progress.active_names.length && <p className="text-dim">正在处理：{progress.active_names.join('、')}</p>}
      {!!draft.definition_issues?.length && <p className="text-dim">另有 {draft.definition_issues.length} 项因来源缺失或定义冲突暂未自动处理，详见清单。</p>}
    </div>}
    {canStart && <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <span>{partial ? `已选择 ${picked.length} 项` : `将自动处理 ${automatic.length} 项`}</span>
      <button className="btn btn-sm" disabled={busy} onClick={() => setPartial(!partial)}>
        {partial ? '恢复处理全部指标' : '仅处理部分指标'}
      </button>
    </div>}
    {canStart && <div className="flex flex-wrap items-center gap-2">
      <label className="text-xs">用于实现的数据源 <select className="field" value={source} onChange={e => setSource(e.target.value)}>
        <option value="">请选择已探查的数据源</option>
        {sources.filter(s => s.enabled !== false).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select></label>
      <button className="btn btn-primary btn-sm" disabled={busy || !sourceAvailable || !selectedCount} onClick={() => void start()}>
        {busy ? '正在提交…' : partial ? `开始处理选中的 ${selectedCount} 项` : '自动处理全部可实现指标'}
      </button>
    </div>}
    {!canStart && metrics.length > 6 && (
      <div className="flex flex-wrap items-center gap-1.5 text-2xs" role="group" aria-label="按实现状态筛选定义">
        {([
          ['all', `全部（${metrics.length}）`, true],
          ['unresolved', `待补充或未过（${unresolvedItems.length}）`, unresolvedItems.length > 0],
          ['implemented', `已有实现（${implementedItems.length}）`, implementedItems.length > 0],
          ['excluded', `暂不处理（${excludedItems.length}）`, excludedItems.length > 0],
        ] as const).filter(([, , show]) => show).map(([key, label]) => (
          <button key={key} type="button"
            className={`btn btn-xs ${filter === key ? 'btn-primary' : 'btn-ghost'}`}
            onClick={() => { setFilter(key); setExpanded(false) }}>
            {label}
          </button>
        ))}
      </div>
    )}
    {!!catalog.warnings?.length && <Notice tone="warn">{catalog.warnings.map((w, i) => <p key={i}>第 {w.line} 行：{w.reason}。{w.quote}</p>)}</Notice>}
    <div className="space-y-2">
      {visibleMetrics.map(item => {
        const itemDone = isItemImplemented(item)
        const diagnostic = resolveItemDiagnostic(draft, item, itemDone)
        const statusText = item.status === 'unavailable' ? '原文注明无数据源'
          : itemDone ? '已有实现 · 待核验'
            : (counts.get(item.name) ?? 0) > 1 ? '同名定义 · 需确认口径'
              : draft.selected_definition_ids?.length && !draft.selected_definition_ids.includes(item.id) ? '未纳入本次处理'
                : draft.status === 'drafting' ? '待处理'
                  : diagnostic?.kind === 'validation_failed' ? '校验未通过 · 待补充或重试'
                    : progress ? '未完成 · 需补充信息' : '待实现'
        return <details key={item.id} className="rounded border p-2 text-xs">
          <summary className="cursor-pointer">
            {canStart && partial && <input type="checkbox" className="mr-2" aria-label={`选择 ${item.name}`}
              disabled={busy || item.status === 'unavailable'} checked={picked.includes(item.id)}
              onClick={e => e.stopPropagation()}
              onChange={e => setPicked(e.target.checked ? [...picked, item.id] : picked.filter(id => id !== item.id))} />}
            {item.name}
            <span className={`ml-2 ${diagnostic ? 'text-[var(--warn)]' : 'text-faint'}`}>{statusText}</span>
          </summary>
          <dl className="mt-2 space-y-1 whitespace-pre-wrap">
            {(draft.definition_issues ?? []).filter(i => i.definition_id === item.id).map(i => (
              <p key={i.definition_id} className="text-[var(--warn)]">{i.reason}</p>
            ))}
            {diagnostic && (
              <div className="rounded bg-elev px-2 py-1.5 text-2xs">
                {diagnostic.questions.map((q, idx) => (
                  <p key={`q:${idx}`} className="text-[var(--warn)]">待确认：{q}</p>
                ))}
                {diagnostic.reasons.map((r, idx) => (
                  <p key={`r:${idx}`} className="text-dim">未通过原因：{r}</p>
                ))}
              </div>
            )}
            {Object.entries(item.fields).map(([name, value]) => <div key={name}><dt className="inline font-semibold">{name}：</dt><dd className="inline">{value || '未说明'}</dd></div>)}
            {!!item.dependencies.length && <div>依赖：{item.dependencies.join('、')}</div>}
          </dl>
          <details className="mt-2 text-faint"><summary>原文 · 第 {item.source.line} 行</summary><pre className="whitespace-pre-wrap">{item.source.quote}</pre></details>
        </details>
      })}
      {!canStart && filteredMetrics.length > COLLAPSE_LIMIT && (
        <button type="button" className="btn btn-xs btn-ghost"
          onClick={() => setExpanded(cur => !cur)}>
          {expanded ? '收起定义列表' : `展开其余 ${filteredMetrics.length - COLLAPSE_LIMIT} 项定义`}
        </button>
      )}
    </div>
    <details className="text-xs"><summary className="cursor-pointer">基础量与全局规则（随选中指标一起用于实现）</summary>
      <div className="mt-2 space-y-2 whitespace-pre-wrap">
        {catalog.items.filter(i => i.kind === 'input').map(i => <p key={i.id}><b>{i.name}</b>：{i.formula}；{i.policy}</p>)}
        {catalog.rules.map((r, i) => <p key={i}>{r.text}</p>)}
      </div>
    </details>
    {error != null && <ErrorState error={error} compact />}
  </section>
}

export function QuerySemanticEditor({ draft, metric, onSaved }: {
  draft: CaliberDraft; metric: DraftMetric; onSaved: (fresh: CaliberDraft) => void
}) {
  const [value, setValue] = useState<QuerySemantics>(metric.semantic ?? {
    version: 1, enabled: false, aliases: metric.synonyms ?? [], grain: metric.grain ?? '', aggregation: '', time: null, entities: [],
  })
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [canonicalEntities, setCanonicalEntities] = useState<CanonicalEntity[]>([])
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    if (!open) return
    void api.ontology.listEntities().then((res) => {
      if (mounted.current) setCanonicalEntities(res.items ?? [])
    }).catch(() => {})
  }, [open])
  const [error, setError] = useState<unknown>(null)
  const pending = useRef({ signature: '', id: '' })
  const [dirty, setDirty] = useState(false)
  const serverValue = JSON.stringify(metric.semantic ?? {
    version: 1, enabled: false, aliases: metric.synonyms ?? [], grain: metric.grain ?? '', aggregation: '', time: null, entities: [],
  })
  const editingBase = useRef(serverValue)
  const conflict = dirty && editingBase.current !== serverValue
  useEffect(() => {
    if (!dirty) {
      editingBase.current = serverValue
      setValue(JSON.parse(serverValue) as QuerySemantics)
    }
  }, [serverValue, dirty])
  const fields = draft.entry_fields ?? []
  if (!metric.id || !draft.card_id || draft.revision == null) return null
  const patch = (change: Partial<QuerySemantics>) => {
    if (!dirty) editingBase.current = serverValue
    setDirty(true); setValue(current => ({ ...current, ...change }))
  }
  const save = async () => {
    if (conflict) return
    const signature = JSON.stringify({ revision: draft.revision, value })
    if (pending.current.signature !== signature) pending.current = {
      signature, id: globalThis.crypto?.randomUUID?.() ?? `semantic-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    }
    setBusy(true); setError(null)
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations: [{ op: 'set', metric_id: metric.id!, path: '/semantic', value }], expected_revision: draft.revision!,
        request_id: pending.current.id, target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id! },
      })
      if (!mounted.current) return
      setDirty(false)
      onSaved(fresh)
      toast.ok('问数定义已保存；验证并发布该版本后生效')
      pending.current = { signature: '', id: '' }
    } catch (e) { setError(e) } finally { setBusy(false) }
  }
  const selectField = (label: string, current: string, update: (s: string) => void) => <label className="text-xs">{label}
    <select className="field ml-1" value={current} onChange={e => update(e.target.value)}>
      <option value="">选择入口参数</option>
      {fields.map(f => <option key={f.name} value={f.name}>{f.description || f.name}（{f.name}）</option>)}
    </select>
  </label>
  return <details className="rounded border p-2 text-xs" data-query-semantics=""
    onToggle={e => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
    <summary className="cursor-pointer font-semibold">{metric.name} · 问数定义{metric.semantic?.enabled ? '（已启用）' : '（待确认）'}</summary>
    <fieldset disabled={busy} className="mt-3 space-y-3">
      <p className="text-faint">这里定义常用问法、日期和对象如何对应已验证的取数参数。汇总规则应与取数实现一致；改动后须重新验证和发布。</p>
      <label className="block">常用叫法（逗号分隔）<AliasesInput className="field w-full" values={value.aliases} onChange={aliases => patch({ aliases })} /></label>
      <label className="block">输出粒度<input className="field w-full" placeholder="例如：指定产线、指定生产日的单值" value={value.grain} onChange={e => patch({ grain: e.target.value })} /></label>
      <label className="block">汇总与缺失规则<textarea className="field w-full" placeholder="例如：按实际开班时长加权；缺失值处理按原文定义" value={value.aggregation} onChange={e => patch({ aggregation: e.target.value })} /></label>
      <label className="block"><input type="checkbox" checked={value.time != null} onChange={e => patch({ time: e.target.checked
        ? { start: '', end: '', timezone: 'Asia/Shanghai', basis: 'calendar_day', periods: ['day'], description: '' } : null })} /> 按日期查询</label>
      {value.time && <div className="space-y-2 rounded border p-2">
        {selectField('开始（含）', value.time.start, start => patch({ time: { ...value.time!, start } }))}{' '}
        {selectField('结束（不含）', value.time.end, end => patch({ time: { ...value.time!, end } }))}
        <label className="block">业务时区<input className="field ml-1" value={value.time.timezone} onChange={e => patch({ time: { ...value.time!, timezone: e.target.value } })} /></label>
        <label className="block">日期归属<select className="field ml-1" value={value.time.basis} onChange={e => patch({ time: { ...value.time!, basis: e.target.value as 'calendar_day' | 'production_day' } })}>
          <option value="calendar_day">自然日</option><option value="production_day">生产日（按已实现的班次规则）</option>
        </select></label>
        <div>支持的查询周期：{(['day', 'month'] as const).map(period => <label key={period} className="ml-2">
          <input type="checkbox" checked={(value.time!.periods ?? ['day']).includes(period)}
            onChange={e => patch({ time: { ...value.time!, periods: e.target.checked
              ? [...(value.time!.periods ?? ['day']), period] : (value.time!.periods ?? ['day']).filter(p => p !== period) } })} />
          {period === 'day' ? '单日' : '整月'}
        </label>)}</div>
        <label className="block">日期规则说明<input className="field w-full" value={value.time.description} onChange={e => patch({ time: { ...value.time!, description: e.target.value } })} /></label>
      </div>}
      {value.entities.map((entity, index) => {
        const edit = (change: Partial<typeof entity>) => patch({ entities: value.entities.map((e, i) => i === index ? { ...e, ...change } : e) })
        return <div key={index} className="space-y-2 rounded border p-2">
          <label>对象类型 <input className="field" placeholder="工厂 / 产线" value={entity.label} onChange={e => edit({ label: e.target.value })} /></label>{' '}
          {selectField('绑定参数', entity.field, field => edit({ field }))}{' '}
          <label>标准实体桥 <select className="field ml-1" value={entity.entity_key ?? ''}
            onChange={e => edit({ entity_key: e.target.value || null })} data-semantic-entity-key={index}>
            <option value="">未关联统一实体</option>
            {canonicalEntities.map(ce => <option key={ce.key} value={ce.key}>{ce.label}（{ce.key}）</option>)}
          </select></label>
          <p className="text-faint">只填写已核实的对象。工厂编号不能当作产线编号。</p>
          {entity.members.map((member, n) => <div key={n} className="flex flex-wrap gap-1">
            {(['value', 'label'] as const).map(key => <input key={key} className="field" aria-label={`${entity.label} ${key}`}
              placeholder={{ value: '参数值', label: '对象名称' }[key]} value={member[key]}
              onChange={e => edit({ members: entity.members.map((m, j) => j === n ? { ...m, [key]: e.target.value } : m) })} />)}
            <AliasesInput values={member.aliases} onChange={aliases => edit({ members: entity.members.map((m, j) => j === n ? { ...m, aliases } : m) })} />
            <button type="button" className="btn btn-xs" onClick={() => edit({ members: entity.members.filter((_, j) => j !== n) })}>移除对象</button>
          </div>)}
          <button type="button" className="btn btn-xs" onClick={() => edit({ members: [...entity.members, { value: '', label: '', aliases: [] }] })}>添加对象</button>{' '}
          <button type="button" className="btn btn-xs" onClick={() => patch({ entities: value.entities.filter((_, i) => i !== index) })}>移除此类对象</button>
        </div>
      })}
      <button type="button" className="btn btn-xs" onClick={() => patch({ entities: [...value.entities, { field: '', label: '', members: [] }] })}>添加对象类型</button>
      <label className="block"><input type="checkbox" checked={value.enabled} onChange={e => patch({ enabled: e.target.checked })} /> 已确认上述定义与取数实现一致，发布后启用直接问数</label>
      {conflict && <Notice tone="warn">
        此指标的问数定义已被更新。你的修改仍保留，请先载入最新配置后再编辑，避免覆盖他人的修改。
        <button type="button" className="btn btn-sm mt-2" onClick={() => {
          setValue(JSON.parse(serverValue) as QuerySemantics); editingBase.current = serverValue; setDirty(false); setError(null)
        }}>载入最新配置（放弃本地修改）</button>
      </Notice>}
      {dirty && <p role="status" className="text-faint">有未保存的修改；保存其他指标不会覆盖这里的内容。</p>}
      <button type="button" className="btn btn-sm" disabled={conflict} onClick={() => void save()}>{busy ? '正在保存…' : '保存问数定义'}</button>
    </fieldset>
    {error != null && <ErrorState error={error} compact />}
  </details>
}


/** Unfinished definitions are prepared separately, then merged with revision checks. */
export function ContinuationPanel({ draft, sources, onOpened }: {
  draft: CaliberDraft; sources?: DataSourceName[]; onOpened: (id: string) => void
}) {
  const [guidance, setGuidance] = useState('')
  const [subsetMode, setSubsetMode] = useState(false)
  const [pickedMissing, setPickedMissing] = useState<string[]>([])
  const [pickedSource, setPickedSource] = useState(draft.datasource_id ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const origin = draft.origin?.continuation_from
  const implemented = new Set((draft.metrics ?? []).map(metric => metric.source_definition_id).filter(Boolean))
  const missing = (draft.semantic_inventory?.items ?? []).filter(item => item.kind === 'metric'
    && item.status !== 'unavailable' && !implemented.has(item.id)
    && draft.selected_definition_ids?.includes(item.id))
  const available = draft.status !== 'drafting' && draft.status !== 'rejected' && draft.status !== 'attached'
  const canContinue = available && draft.status !== 'path_pending' && !origin
    && (!!draft.workflow_id || !!draft.implementation_progress || draft.status === 'questions_open' || draft.status === 'failed')
    && missing.length > 0
  if (!origin && !canContinue) return null
  const unavailableNames = new Set(
    (draft.definition_issues ?? []).map(i => i.name).filter(Boolean),
  )
  const realQuestions = (draft.open_questions ?? []).filter(q =>
    !isSyntheticUnresolved(q) && !Array.from(unavailableNames).some(name => q.startsWith(`${name}：`)),
  )
  const fallbackReasons = realQuestions.length > 0 ? [] : Array.from(new Set([
    ...Object.values(draft.definition_diagnostics ?? {}).flatMap(d => [...(d.questions ?? []), ...(d.reasons ?? [])]),
    ...(draft.dropped ?? [])
      .filter(d => d.reason && d.reason !== '没有一条指标通过校验')
      .map(d => d.name && d.name !== 'sql' && d.name !== 'metrics' ? `${d.name}：${d.reason}` : d.reason),
  ])).slice(0, 4)
  const promptHints = (realQuestions.length > 0 ? realQuestions : fallbackReasons).slice(0, 4)
  const enabledSources = (sources ?? []).filter(s => s.enabled !== false)
  const canSwitchSource = !draft.workflow_id && enabledSources.length > 0
  const fillTemplate = () => {
    const lines = [
      '来源表与关联键：',
      '字段映射说明：',
      '固定过滤条件：',
      ...(promptHints.length > 0 ? [`补充确认：${promptHints.slice(0, 2).join('；')}`] : []),
    ]
    setGuidance(cur => cur.trim() ? `${cur.trim()}\n${lines.join('\n')}` : lines.join('\n'))
  }
  const toggleSubset = () => {
    if (!subsetMode) {
      setPickedMissing(cur => cur.length > 0 ? cur : missing.slice(0, Math.min(5, missing.length)).map(i => i.id))
      setSubsetMode(true)
    } else {
      setSubsetMode(false)
    }
  }
  const act = async (merge: boolean) => {
    setBusy(true); setError(null)
    try {
      let id: string
      if (merge && origin) {
        const original = await api.caliberLedger.draft(origin.draft_id)
        if (!mounted.current) return
        const fresh = await api.caliberLedger.mergeContinuation(draft.id, {
          expected_revision: original.revision ?? 0, candidate_revision: draft.revision ?? 0,
        })
        id = fresh.id
        toast.ok('已将补充指标合并到原稿；请重新验证并发布')
      } else {
        const out = await api.caliberLedger.continueDefinitions(draft.id, {
          expected_revision: draft.revision ?? 0,
          guidance: guidance.trim(),
          ...(subsetMode ? { selected: pickedMissing } : {}),
          ...(canSwitchSource && pickedSource && pickedSource !== draft.datasource_id ? { datasource_id: pickedSource } : {}),
        })
        id = out.draft_id
        toast.ok('已开始准备未完成指标，原稿保留')
      }
      if (mounted.current) onOpened(id)
    } catch (e) { if (mounted.current) setError(e) }
    finally { if (mounted.current) setBusy(false) }
  }
  return <section className="space-y-2 rounded-lg border p-3 text-xs" data-draft-continuation="">
    {origin ? <>
      <h3 className="font-semibold">补充指标候选</h3>
      <p className="text-dim">{origin.merge_required
        ? '原稿和已有修改已保留。候选完成后可合并新增指标；发生定义冲突时会保留两份草稿供修正。合并后须重新验证。'
        : '补充指标已合并到原稿，请在原稿中继续验证与发布。'}</p>
      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => onOpened(origin.draft_id)}>查看原稿</button>{' '}
      {origin.merge_required && <button type="button" className="btn btn-primary btn-sm"
        disabled={busy || !available || !draft.workflow_id || !draft.metrics?.length}
        onClick={() => void act(true)}>{busy ? '正在合并…' : '合并补充指标到原稿'}</button>}
    </> : <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">继续处理未完成指标 · {missing.length} 项</h3>
        {missing.length > 1 && (
          <button type="button" className="btn btn-xs" disabled={busy} onClick={toggleSubset}>
            {subsetMode ? '恢复处理全部未完成指标' : '仅先验证部分未完成指标'}
          </button>
        )}
      </div>
      <p className="text-dim">{draft.workflow_id
        ? '补充来源或字段说明后，只处理缺少的指标。已有实现和人工修改保留，生成结果会先放入独立候选，随后可合并回原稿。'
        : '本轮尚未生成取数图。补充来源表、字段映射或固定条件后，系统将生成独立候选，完成后可一键接受进当前草稿。'}</p>
      {promptHints.length > 0 && (
        <div className="space-y-1 rounded border bg-elev px-2.5 py-2 text-2xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-dim">待补充要点</span>
            <button type="button" className="btn btn-xs btn-ghost" disabled={busy} onClick={fillTemplate}>
              填入说明模板
            </button>
          </div>
          <ul className="list-disc space-y-0.5 pl-4 text-dim">
            {promptHints.map((hint, idx) => <li key={idx}>{hint}</li>)}
          </ul>
        </div>
      )}
      {canSwitchSource && (
        <label className="block">用于生成的数据源{' '}
          <select className="field ml-1" value={pickedSource} disabled={busy}
            onChange={e => setPickedSource(e.target.value)}>
            <option value="">请选择已探查的数据源</option>
            {enabledSources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
      )}
      {subsetMode && (
        <div className="max-h-40 space-y-1 overflow-y-auto rounded border bg-elev p-2 text-2xs">
          <p className="text-faint">先勾选少量核心指标先行验证（已选 {pickedMissing.length} 项）：</p>
          {missing.map(item => (
            <label key={item.id} className="flex items-center gap-1.5">
              <input type="checkbox" disabled={busy} checked={pickedMissing.includes(item.id)}
                onChange={e => setPickedMissing(e.target.checked
                  ? [...pickedMissing, item.id]
                  : pickedMissing.filter(id => id !== item.id))} />
              <span>{item.name}</span>
            </label>
          ))}
        </div>
      )}
      <label className="block">补充说明<textarea className="field mt-1 w-full" value={guidance} disabled={busy}
        maxLength={2000} placeholder="例如：OEE 基础数据在 ANALYTICS.v_shift_kpi，不合格数在 ANALYTICS.v_scrap_record"
        onChange={e => setGuidance(e.target.value)} /></label>
      <button type="button" className="btn btn-primary btn-sm"
        disabled={busy || (subsetMode && pickedMissing.length === 0) || (canSwitchSource && !pickedSource)}
        onClick={() => void act(false)}>
        {busy ? '正在提交…' : subsetMode ? `继续处理选中的 ${pickedMissing.length} 项` : '继续处理未完成指标'}</button>
    </>}
    {error != null && <ErrorState error={error} compact />}
  </section>
}

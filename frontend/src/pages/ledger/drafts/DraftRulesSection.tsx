import { useCallback, useRef, useState } from 'react'
import { Check, Undo2 } from 'lucide-react'
import { api, ApiError } from '../../../api/client'
import { ErrorState, Notice, Spinner, toast } from '../../../components/ui'
import { LEDGER_UI } from '../../../lib/termsLedger'
import type { CaliberDraft, DraftBinding, DraftChangeReceipt, DraftOperation } from '../../../typesLedger'
import { choiceLabel, declaringMetrics, newRequestId, ruleInitialValues, rulePlan, undoOperations } from './helpers'

const R = LEDGER_UI.rules

export function useDraftRules({
  setDetail, refreshDetail, loadValidations, loadList,
}: {
  setDetail: React.Dispatch<React.SetStateAction<CaliberDraft | null>>
  refreshDetail: (id: string) => Promise<void>
  loadValidations: (id: string) => void
  loadList: () => void
}) {
  const [ruleValues, setRuleValues] = useState<Record<string, string>>({})
  const rulePending = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })
  const [ruleBusy, setRuleBusy] = useState<'save' | 'undo' | null>(null)
  const [ruleError, setRuleError] = useState<unknown>(null)
  const [ruleStale, setRuleStale] = useState<{ current: number | null; base: number | null } | null>(null)
  const [ruleResult, setRuleResult] = useState<DraftChangeReceipt | null>(null)
  const [ruleReplayed, setRuleReplayed] = useState(false)
  const [undoRecord, setUndoRecord] = useState<{ operations: DraftOperation[]; count: number } | null>(null)

  const resetRules = useCallback(() => {
    setRuleValues({})
    setRuleBusy(null)
    setRuleError(null)
    setRuleStale(null)
    setRuleResult(null)
    setRuleReplayed(false)
    setUndoRecord(null)
    rulePending.current = { signature: '', requestId: '' }
  }, [])

  const syncRulesFromBinding = useCallback((binding?: DraftBinding) => {
    setRuleValues(ruleInitialValues(binding))
  }, [])

  /**
   * 保存这次业务规则的修改：把**所有**受影响指标的字段操作放在一次请求里（PATCH 的 operations），带上读到的
   * 基准版本、目标卡和幂等键。一次原子变更：全成或全不成；回执（change）说清落成哪一版、绑定同步到哪一步。
   * 被拒（基准版本过期）时什么都不改：这里的写法选择留着，刷新详情拿到新版本后由人显式再点一次。
   */
  const saveRules = useCallback(async (draft: CaliberDraft, operations: DraftOperation[]) => {
    if (!operations.length) { toast.warn(R.saveNeedChange); return }
    if (!draft.card_id || draft.revision == null) { toast.warn(R.noCard); return }
    setRuleBusy('save')
    setRuleError(null)
    const signature = `${draft.id}|${JSON.stringify(operations)}`
    if (rulePending.current.signature !== signature) {
      rulePending.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations,
        expected_revision: draft.revision,
        request_id: rulePending.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id },
      })
      setDetail(fresh)
      setRuleResult(fresh.change ?? null)
      setRuleReplayed(!!fresh.replayed)
      setRuleStale(null)
      setUndoRecord({ operations: undoOperations(draft.binding, operations), count: operations.length })
      setRuleValues(ruleInitialValues(fresh.binding))
      rulePending.current = { signature: '', requestId: '' }
      toast.ok(R.saved(fresh.change?.revision ?? fresh.revision ?? null))
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        setRuleStale({ current: Number.isFinite(told) ? told : null, base: draft.revision ?? null })
        void refreshDetail(draft.id)
      } else {
        setRuleError(e)
      }
    } finally {
      setRuleBusy(null)
    }
  }, [loadList, loadValidations, refreshDetail, setDetail])

  /** 撤销这次修改：逆操作 + 当前版本 + 新的请求键，再新增一版；不是整份覆盖，也不改已经存下的版本 */
  const undoRules = useCallback(async (draft: CaliberDraft, record: { operations: DraftOperation[]; count: number }) => {
    if (draft.revision == null || !record.operations.length) return
    setRuleBusy('undo')
    setRuleError(null)
    const signature = `undo|${draft.id}|${JSON.stringify(record.operations)}`
    if (rulePending.current.signature !== signature) {
      rulePending.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        operations: record.operations,
        expected_revision: draft.revision,
        request_id: rulePending.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id ?? undefined },
      })
      setDetail(fresh)
      setRuleResult(fresh.change ?? null)
      setRuleReplayed(!!fresh.replayed)
      setRuleStale(null)
      setUndoRecord(null)
      setRuleValues(ruleInitialValues(fresh.binding))
      rulePending.current = { signature: '', requestId: '' }
      toast.ok(R.undone(fresh.change?.revision ?? fresh.revision ?? null))
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        setRuleStale({ current: Number.isFinite(told) ? told : null, base: draft.revision ?? null })
        void refreshDetail(draft.id)
      } else {
        setRuleError(e)
      }
    } finally {
      setRuleBusy(null)
    }
  }, [loadList, loadValidations, refreshDetail, setDetail])

  return {
    ruleValues, setRuleValues, ruleBusy, ruleError, ruleStale, ruleResult, ruleReplayed, undoRecord,
    resetRules, syncRulesFromBinding, saveRules, undoRules,
  }
}

/**
 * 业务规则（K06 的工作台）：服务端的能力描述说得出这张卡能按字段改哪几项时才开表单。改完先列「会改到哪几条指标、
 * 现在什么写法 → 保存后什么写法」，再把所有受影响指标的字段操作**一次**提交（原子变更）；回执说清落成哪一版、
 * 取数同步到哪一步、旧验证怎么失效，并按保存前的值给出「撤销这次修改」。这块只写业务说法：取数语句、内部编号、
 * 字段路径和 JSON 都不上来。
 */
export function DraftRulesSection({
  draft, ruleValues, setRuleValues, ruleBusy, ruleError, ruleStale, ruleResult, ruleReplayed, undoRecord,
  onSave, onUndo,
}: {
  draft: CaliberDraft
  ruleValues: Record<string, string>
  setRuleValues: React.Dispatch<React.SetStateAction<Record<string, string>>>
  ruleBusy: 'save' | 'undo' | null
  ruleError: unknown
  ruleStale: { current: number | null; base: number | null } | null
  ruleResult: DraftChangeReceipt | null
  ruleReplayed: boolean
  undoRecord: { operations: DraftOperation[]; count: number } | null
  onSave: (draft: CaliberDraft, operations: DraftOperation[]) => void
  onUndo: (draft: CaliberDraft, record: { operations: DraftOperation[]; count: number }) => void
}) {
  const binding = draft.binding
  if (!binding) return null
  const fields = binding.editable_fields ?? []
  const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
  const names = new Map(metrics.map((metric) => [metric.id as string, metric.name]))
  const declared = new Set(Object.keys(binding.metric_values ?? {}))
  const plan = rulePlan(binding, ruleValues)
  const changes = plan.changes.map((change) => ({ ...change, name: names.get(change.metricId) ?? change.metricId }))
  const readOnly = !draft.workflow_id ? R.noWorkflow
    : !draft.card_id ? R.noCard
      : draft.revision == null ? R.noRevision
        : declared.size === 0 ? R.unbound : null
  const label = R.status[binding.status] ?? binding.status
  const tone = binding.status === 'aligned' ? 'var(--ok)'
    : binding.status === 'unbound' ? 'var(--text-faint)' : 'var(--warn)'
  const saved = ruleResult?.binding
  const untouched = metrics.length - declared.size
  return (
    <div className="rounded-md border bg-elev px-3 py-2" data-draft-rules={binding.status}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-2xs font-semibold text-dim">{R.title}</h3>
        <span className="text-2xs" data-draft-rules-status={binding.status} style={{ color: tone }}>{label}</span>
        <span className="text-[10px] text-faint">{R.hint}</span>
      </div>
      {binding.status === 'aligned' ? (
        <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-rules-note="">{R.alignedNote}</p>
      ) : (
        <>
          <p className="mt-1 text-2xs leading-relaxed text-[var(--warn)]" data-draft-rules-reason="">
            {R.reason}：{binding.reason}
          </p>
          {binding.fix && binding.fix !== 'none' && (
            <p className="mt-0.5 text-2xs leading-relaxed text-dim" data-draft-rules-todo="">
              {R.fixLabel}：{R.fix[binding.fix] ?? binding.fix}
            </p>
          )}
        </>
      )}

      {readOnly ? (
        <p className="mt-1.5 text-2xs leading-relaxed text-faint" data-draft-rules-readonly="">{R.readOnly}：{readOnly}</p>
      ) : (
        <>
          <ul className="mt-1.5 space-y-1.5">
            {fields.map((field) => {
              const declaring = declaringMetrics(binding, field.field)
              const distinct = [...new Set(declaring.map((id) => binding.metric_values[id][field.field]))]
              const chosen = ruleValues[field.field] ?? ''
              return (
                <li key={field.field} className="flex flex-wrap items-center gap-x-2 gap-y-1"
                    data-draft-rule-field={field.field}>
                  <span className="text-2xs font-medium text-dim">{field.label}</span>
                  <select className="field h-[26px] text-2xs" value={chosen} aria-label={field.label}
                          disabled={!declaring.length || ruleBusy != null} data-draft-rule-select={field.field}
                          title={distinct.length > 1 ? R.mixedHint : undefined}
                          onChange={(e) => setRuleValues((cur) => ({ ...cur, [field.field]: e.target.value }))}>
                    <option value="">{R.keep}</option>
                    {field.values.map((value) => (
                      <option key={value} value={value}>{choiceLabel(field, value)}</option>
                    ))}
                  </select>
                  <span className="text-[10px] text-faint" data-draft-rule-current={field.field}>
                    {!declaring.length ? R.fieldNone
                      : distinct.length > 1 ? R.mixed
                        : `${R.before}：${choiceLabel(field, distinct[0])}`}
                  </span>
                  <span className="text-[10px] text-faint [overflow-wrap:anywhere]">{field.help}</span>
                </li>
              )
            })}
          </ul>
          {untouched > 0 && (
            <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-rule-untouched={untouched}>
              {R.unaffected(untouched)}。{R.unaffectedHint}
            </p>
          )}

          <div className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-rule-preview="">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="text-2xs font-semibold text-dim">{R.previewTitle}</span>
              <span className="tnum text-2xs text-faint" data-draft-rule-affected={changes.length}>
                {changes.length ? R.affected(changes.length) : R.noChange}
              </span>
            </div>
            {changes.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {changes.map((change) => (
                  <li key={`${change.metricId}:${change.field.field}`} data-draft-rule-change={change.metricId}
                      className="text-2xs leading-relaxed text-dim">
                    <span className="font-medium [overflow-wrap:anywhere]">{change.name}</span>
                    <span className="text-faint"> · {change.field.label}：</span>
                    <span className="text-faint">{choiceLabel(change.field, change.from)}</span>
                    <span className="text-faint"> → </span>
                    <span>{choiceLabel(change.field, change.to)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-xs btn-primary" data-draft-rule-save=""
                    disabled={ruleBusy != null || !plan.operations.length}
                    onClick={() => onSave(draft, plan.operations)}>
              {ruleBusy === 'save' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
              {ruleBusy === 'save' ? R.saving : R.save}
            </button>
            {undoRecord && (
              <button type="button" className="btn btn-xs" data-draft-rule-undo="" title={R.undoHint}
                      disabled={ruleBusy != null}
                      onClick={() => onUndo(draft, undoRecord)}>
                {ruleBusy === 'undo' ? <Spinner size={11} /> : <Undo2 size={11} aria-hidden />}
                {ruleBusy === 'undo' ? R.undoing : R.undo(undoRecord.count)}
              </button>
            )}
          </div>
        </>
      )}

      {ruleStale && (
        <div className="mt-1.5" data-draft-rule-stale="">
          <Notice tone="warn">{R.stale(ruleStale.current, ruleStale.base)}</Notice>
        </div>
      )}
      {ruleError != null && <div className="mt-1.5"><ErrorState error={ruleError} compact /></div>}
      {ruleReplayed && !ruleResult && (
        <div className="mt-1.5"><Notice tone="info">{R.replayed}</Notice></div>
      )}

      {ruleResult && (
        <div className="mt-1.5 rounded border bg-panel px-2 py-1.5 text-2xs leading-relaxed"
             data-draft-rule-result={ruleResult.applied ? 'applied' : 'none'}>
          <p className="text-dim" data-draft-rule-result-version="">
            {R.saved(ruleResult.revision)}
            {ruleReplayed && <span className="text-faint"> · {R.replayed}</span>}
          </p>
          {ruleResult.applied ? (
            <>
              <p data-draft-rule-binding={saved?.status ?? ''}>
                {saved?.synced ? R.savedAligned : R.savedNotSynced}
                {!saved?.synced && saved?.reason ? `（${saved.reason}）` : ''}
              </p>
              <p className="text-faint" data-draft-rule-invalidated="">{R.invalidated}</p>
              <p className="text-faint">{R.nextStep}</p>
              {!saved?.synced && saved?.fix && saved.fix !== 'none' && (
                <p className="text-dim" data-draft-rule-fix="">
                  {R.fixLabel}：{R.fix[saved.fix] ?? saved.fix}。{R.fixNote}
                </p>
              )}
            </>
          ) : (
            <p className="text-faint">{R.savedNoChange}</p>
          )}
        </div>
      )}
    </div>
  )
}

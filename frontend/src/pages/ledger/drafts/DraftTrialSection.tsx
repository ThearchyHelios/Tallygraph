import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Play } from 'lucide-react'
import { api } from '../../../api/client'
import { Notice, Spinner } from '../../../components/ui'
import { LEDGER_UI } from '../../../lib/termsLedger'
import { TrialResult, TrialValidation } from '../TrialResult'
import type { CaliberDraft, DraftExpectationSource, DraftTrialOut } from '../../../typesLedger'
import { coversText, defaultText, entryInitialValues, entryInputs, entryReady, qualityReportOf } from './helpers'

const D = LEDGER_UI.drafts
const V = LEDGER_UI.validation

export function useDraftTrial({
  detail, setDetail, openSeq, loadList, loadValidations,
}: {
  detail: CaliberDraft | null
  setDetail: React.Dispatch<React.SetStateAction<CaliberDraft | null>>
  openSeq: React.MutableRefObject<number>
  loadList: () => void
  loadValidations: (id: string) => void
}) {
  const [trial, setTrial] = useState<DraftTrialOut | null>(null)
  const trialRequestSeq = useRef(0)
  const [trialPollId, setTrialPollId] = useState<string | null>(null)
  const [trialTimedOut, setTrialTimedOut] = useState(false)
  const [trialError, setTrialError] = useState<unknown>(null)
  const [entryValues, setEntryValues] = useState<Record<string, string>>({})
  const [prefilled, setPrefilled] = useState<Record<string, boolean>>({})
  const [trialStarting, setTrialStarting] = useState(false)

  const resetTrial = useCallback(() => {
    setTrial(null)
    setTrialPollId(null)
    setTrialTimedOut(false)
    setTrialError(null)
  }, [])

  const initEntryFields = useCallback((draft: CaliberDraft) => {
    const marks: Record<string, boolean> = {}
    setEntryValues(entryInitialValues(draft.entry_fields ?? [], marks))
    setPrefilled(marks)
  }, [])

  /** 草稿有了试运行就取一次结果；还在 running 就交给下面的轮询 */
  useEffect(() => {
    const id = detail?.id
    const run = detail?.trial?.run_id
    if (!id || !run) return
    let alive = true
    setTrial((cur) => (cur?.run_id === run && detail?.trial?.status !== 'stale' ? cur : {
      run_id: run, status: detail?.trial?.status ?? 'running', metrics: [], error: detail?.trial?.error ?? '',
    }))
    const request = ++trialRequestSeq.current
    api.caliberLedger.draftTrial(id).then((out) => {
      if (!alive || request !== trialRequestSeq.current) return
      setTrial(out)
      setTrialError(null)
      setTrialTimedOut(false)
      if (out.status === 'running') setTrialPollId(id)
    }, (e) => {
      if (!alive || request !== trialRequestSeq.current) return
      setTrialError(e)
      setTrialPollId(null)
    })
    return () => { alive = false }
  }, [detail?.id, detail?.version, detail?.trial?.run_id, detail?.trial?.status])

  /** 试运行还在跑：每两秒取一次，超过一分钟就停；收尾时顺带刷新详情（状态可能已变成待签字发布） */
  useEffect(() => {
    if (!trialPollId) return
    const until = Date.now() + 60_000
    let alive = true
    let settled = false
    const seq = openSeq.current
    let timer: ReturnType<typeof setTimeout>
    const poll = () => {
      const request = ++trialRequestSeq.current
      api.caliberLedger.draftTrial(trialPollId).then((out) => {
        if (!alive || seq !== openSeq.current || request !== trialRequestSeq.current) return
        setTrial(out)
        setTrialError(null)
        if (out.status === 'running') {
          if (Date.now() > until) {
            settled = true
            setTrialPollId(null)
            setTrialTimedOut(true)
          }
          return
        }
        settled = true
        setTrialPollId(null)
        setTrialTimedOut(false)
        loadList()
        // 试运行到终态时服务端会把这次跑完的验证记录补记一条：跟着取一次，结论按记录说
        loadValidations(trialPollId)
        api.caliberLedger.draft(trialPollId).then((fresh) => {
          if (seq !== openSeq.current) return
          setDetail((cur) => (cur && cur.id === fresh.id && (cur.revision ?? 0) <= (fresh.revision ?? 0) ? fresh : cur))
        }, () => undefined)
      }, (e) => {
        if (!alive || seq !== openSeq.current || request !== trialRequestSeq.current) return
        settled = true
        setTrialPollId(null)
        setTrialError(e)
      }).finally(() => {
        if (alive && !settled && seq === openSeq.current) timer = setTimeout(poll, 2000)
      })
    }
    timer = setTimeout(poll, 2000)
    return () => { alive = false; clearTimeout(timer) }
  }, [trialPollId, loadList, loadValidations, openSeq, setDetail])

  /**
   * 按填好的入口字段再试运行一次：自动试跑缺必填输入时停在 needs_input，人在这里补上。
   * 带 expect 时这一条会落成一条带独立期望的验证记录（对得上就是对得上，对不上照实记）；不带就是只记试跑结果。
   * 入口字段的写法变化只影响这一次请求的参数，不会创建口径定义版本。
   */
  const runTrial = useCallback(async (
    draft: CaliberDraft,
    expect?: { metrics: Record<string, unknown>; source: DraftExpectationSource },
  ) => {
    const fields = draft.entry_fields ?? []
    if (!entryReady(fields, entryValues)) return
    setTrialStarting(true)
    setTrialError(null)
    try {
      const out = await api.caliberLedger.runDraftTrial(draft.id, entryInputs(fields, entryValues), expect)
      setTrial(out)
      setTrialTimedOut(false)
      if (out.status === 'running' && out.run_id) setTrialPollId(draft.id)
      const fresh = await api.caliberLedger.draft(draft.id)
      setDetail((cur) => cur?.id === fresh.id ? fresh : cur)
      loadValidations(draft.id)
    } catch (e) {
      setTrialError(e)
    } finally {
      setTrialStarting(false)
    }
  }, [entryValues, loadValidations, setDetail])

  return {
    trial, trialTimedOut, trialError, entryValues, setEntryValues, prefilled, trialStarting,
    resetTrial, initEntryFields, runTrial,
  }
}

/** 自动试运行：状态、每个指标的值（名字和值成对）、失败原因；没有试运行就整块不画 */
export function DraftTrialSection({
  draft, trial, trialTimedOut, trialError,
}: {
  draft: CaliberDraft
  trial: DraftTrialOut | null
  trialTimedOut: boolean
  trialError: unknown
}) {
  const status = draft.trial?.status === 'stale' ? 'stale' : trial?.status ?? draft.trial?.status
  if (!status) return null
  const rows = trial?.metrics ?? []
  const quality = qualityReportOf(trial?.validation)
  const brief = trial?.validation
  const own = brief?.record ?? null
  const color = status === 'succeeded' ? (quality?.status === 'pass' ? 'var(--ok)' : 'var(--warn)') : status === 'failed' ? 'var(--err)'
    : status === 'needs_input' || status === 'stale' ? 'var(--warn)' : 'var(--accent)'
  const label = status === 'succeeded' ? D.trialSucceeded : status === 'failed' ? D.trialFailed
    : status === 'needs_input' ? D.trialNeedsInput : status === 'stale' ? D.trialStale : D.trialRunning
  const unitOf = (id: string) => (draft.metrics ?? []).find((m) => m.id === id)?.unit
  return (
    <div className="rounded-md border bg-elev px-3 py-2" data-draft-trial={status}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-2xs font-semibold text-dim">{D.trial}</h3>
        <span className="text-2xs" style={{ color }}>{label}</span>
        {trial?.run_id && (
          <Link to={`/runs/${trial.run_id}`} className="text-2xs text-accent hover:underline" data-draft-trial-run="">
            {D.trialRun}
          </Link>
        )}
        <span className="text-[10px] text-faint">{D.trialHint}</span>
      </div>
      {status === 'running' && (
        <p className="mt-1 flex items-center gap-1.5 text-2xs text-dim">
          <Spinner size={11} /> {D.trialRunning}
        </p>
      )}
      {status === 'running' && trialTimedOut && <div className="mt-1"><Notice tone="warn">{D.trialTimeout}</Notice></div>}
      {status === 'failed' && (
        <div className="mt-1">
          <Notice tone="err">{D.trialFailed}：{trial?.error || draft.trial?.error || D.loadFailed}</Notice>
        </div>
      )}
      {status === 'needs_input' && (
        <p className="mt-1 text-2xs text-dim" data-draft-trial-needs-input="">
          {D.trialNeedsInputHint}：{trial?.error || draft.trial?.error || ''}
        </p>
      )}
      {status === 'stale' && <p className="mt-1 text-2xs text-dim">{D.trialStaleHint}</p>}
      {status === 'succeeded' && (
        <>
          {rows.length > 0 ? (
            <ul className="mt-1 grid gap-x-4 gap-y-0.5 text-2xs sm:grid-cols-[1fr_auto]">
              {rows.map((row) => {
                const unit = unitOf(row.id)
                return (
                  <li key={row.id} className="contents" data-draft-trial-metric={row.id}>
                    <span className="text-dim [overflow-wrap:anywhere]">{row.name || row.id}</span>
                    <span className="tnum text-right font-medium">
                      {row.rendered ?? (row.value == null ? '—' : String(row.value))}{unit ? ` ${unit}` : ''}
                    </span>
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="mt-1 text-2xs text-faint">{D.trialNoMetrics}</p>
          )}
          <TrialValidation report={quality} />
          <TrialResult queries={trial?.queries} />
        </>
      )}
      {trialError != null && <div className="mt-1"><Notice tone="warn">{D.trialUnavailable}</Notice></div>}

      {own ? (
        <p className="mt-1.5 text-2xs leading-relaxed text-dim" data-draft-trial-record={own.status}
           data-trial-record-covers={own.covers_current === true ? 'yes' : own.covers_current === false ? 'no' : 'unknown'}>
          <span className="text-faint">{V.trialRecord}：</span>
          {V.status[own.status] ?? own.status}
          <span className="text-faint"> · {coversText(own.covers_current)}</span>
          <span className="text-faint">
            {' · '}{own.expected
              ? `${V.expected}：${V.sources[own.expected.source] ?? own.expected.source}`
              : V.noExpectation}
          </span>
        </p>
      ) : status === 'running' ? (
        <p className="mt-1.5 flex items-center gap-1.5 text-2xs text-dim" data-draft-trial-waiting="">
          <Spinner size={11} /> {V.trialWaiting}
        </p>
      ) : (
        <p className="mt-1.5 text-2xs leading-relaxed text-faint" data-draft-trial-no-record="">
          {brief?.note || V.trialNoRecord}
        </p>
      )}
    </div>
  )
}

/** 入口字段：发布后按这些字段收参数 */
export function DraftEntryFieldsSection({
  draft, entryValues, setEntryValues, prefilled, trialStarting, onRunTrial,
}: {
  draft: CaliberDraft
  entryValues: Record<string, string>
  setEntryValues: React.Dispatch<React.SetStateAction<Record<string, string>>>
  prefilled: Record<string, boolean>
  trialStarting: boolean
  onRunTrial: (draft: CaliberDraft) => void
}) {
  const fields = draft.entry_fields
  const notes = draft.entry_notes ?? []
  if (!fields) return null
  const canTrial = !!draft.workflow_id
  return (
    <div data-draft-entry-fields="">
      <h3 className="mb-1 text-2xs font-semibold text-dim">{D.entryFields}</h3>
      <p className="mb-1 text-2xs text-faint">{D.entryFieldsHint}</p>
      {fields.length > 0 ? (
        <ul className="space-y-0.5 text-2xs leading-relaxed">
          {fields.map((field) => {
            const hasDefault = field.default !== undefined && field.default !== null && field.default !== ''
            return (
              <li key={field.name} className="flex flex-wrap items-center gap-x-2 gap-y-1"
                  data-draft-entry-field={field.name}>
                <code className="font-medium text-dim">{field.name}</code>
                <span className="text-faint">{field.type || 'string'}</span>
                <span className="text-faint">{field.required ? D.required : D.optional}</span>
                {hasDefault && <span className="text-faint">{D.defaultValue(defaultText(field.default))}</span>}
                {field.description && <span className="text-dim [overflow-wrap:anywhere]">{field.description}</span>}
                {canTrial && (field.type === 'boolean' ? (
                  <input type="checkbox" className="accent-[var(--accent)]" disabled={trialStarting}
                         data-draft-entry-input={field.name}
                         checked={(entryValues[field.name] ?? '') === 'true'}
                         onChange={(e) => setEntryValues((cur) => ({
                           ...cur, [field.name]: e.target.checked ? 'true' : 'false',
                         }))} />
                ) : (
                  <input className="field w-36 text-2xs" value={entryValues[field.name] ?? ''}
                         disabled={trialStarting} data-draft-entry-input={field.name}
                         placeholder={prefilled[field.name] ? D.entryPrefilled : ''}
                         onChange={(e) => setEntryValues((cur) => ({ ...cur, [field.name]: e.target.value }))} />
                ))}
              </li>
            )
          })}
        </ul>
      ) : <p className="text-2xs text-faint">{D.entryFieldNone}</p>}
      {notes.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-2xs leading-relaxed text-warn" data-draft-entry-notes="">
          {notes.map((note, i) => <li key={`${i}:${note}`}>{note}</li>)}
        </ul>
      )}
      {canTrial && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-xs btn-primary"
                  disabled={trialStarting || !entryReady(fields, entryValues)}
                  onClick={() => onRunTrial(draft)} data-draft-trial-run-button="">
            {trialStarting ? <Spinner size={11} /> : <Play size={11} aria-hidden />}
            {trialStarting ? D.trialRunStarting : D.trialRunAction}
          </button>
          {Object.keys(prefilled).length > 0 && (
            <span className="text-[10px] text-faint">{D.entryPrefilled}</span>
          )}
        </div>
      )}
    </div>
  )
}

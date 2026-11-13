import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Play } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../../api/client'
import { ErrorState, Spinner, toast } from '../../../components/ui'
import { formatDateTime } from '../../../lib/format'
import { LEDGER_UI } from '../../../lib/termsLedger'
import { useAtLeast } from '../../../store/auth'
import type {
  CaliberDraft, DraftExpectationSource, DraftValidationsOut, DraftValidationRecord,
} from '../../../typesLedger'
import {
  coversText, entryReady, expectValue, RECORDS_SHOWN, scopeCovers, uncoveredMetrics, valueText,
} from './helpers'

const V = LEDGER_UI.validation

export function useDraftValidations({
  openSeq, refreshDetail,
}: {
  openSeq: React.MutableRefObject<number>
  refreshDetail: (id: string) => Promise<void>
}) {
  const [validations, setValidations] = useState<DraftValidationsOut | null>(null)
  const [validationsError, setValidationsError] = useState<unknown>(null)
  const [reviewOut, setReviewOut] = useState<Record<string, DraftValidationRecord['review']>>({})
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({})
  const [reviewBusy, setReviewBusy] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState<unknown>(null)
  const [expectValues, setExpectValues] = useState<Record<string, string>>({})
  const [expectSource, setExpectSource] = useState<DraftExpectationSource>('human_confirmed')
  const [recordsShown, setRecordsShown] = useState(RECORDS_SHOWN)

  const resetValidations = useCallback(() => {
    setValidations(null)
    setValidationsError(null)
    setReviewOut({})
    setReviewNotes({})
    setReviewError(null)
    setExpectValues({})
    setExpectSource('human_confirmed')
    setRecordsShown(RECORDS_SHOWN)
  }, [])

  const loadValidations = useCallback((id: string) => {
    const seq = openSeq.current
    api.caliberLedger.draftValidations(id).then(
      (out) => { if (seq === openSeq.current) { setValidations(out); setValidationsError(null) } },
      (e) => { if (seq === openSeq.current) { setValidations(null); setValidationsError(e) } },
    )
  }, [openSeq])

  const reviewRecord = useCallback(async (
    draft: CaliberDraft,
    recordId: string,
    decision: 'confirmed' | 'rejected',
  ) => {
    setReviewBusy(recordId)
    setReviewError(null)
    try {
      const note = (reviewNotes[recordId] ?? '').trim()
      const out = await api.caliberLedger.reviewValidation(draft.id, recordId,
        { decision, ...(note ? { note } : {}) })
      setReviewOut((cur) => ({ ...cur, [recordId]: out.record.review ?? null }))
      setReviewNotes((cur) => ({ ...cur, [recordId]: '' }))
      toast.ok(V.reviewDone(decision === 'confirmed' ? V.reviewConfirmed : V.reviewRejected))
      loadValidations(draft.id)
      void refreshDetail(draft.id)
    } catch (e) {
      setReviewError(e)
    } finally {
      setReviewBusy(null)
    }
  }, [loadValidations, refreshDetail, reviewNotes])

  return {
    validations, validationsError, reviewOut, reviewNotes, setReviewNotes,
    reviewBusy, reviewError, expectValues, setExpectValues, expectSource, setExpectSource,
    recordsShown, setRecordsShown, resetValidations, loadValidations, reviewRecord,
  }
}

export function DraftValidationSection({
  draft, validations, validationsError, reviewOut, reviewNotes, setReviewNotes,
  reviewBusy, reviewError, expectValues, setExpectValues, expectSource, setExpectSource,
  recordsShown, setRecordsShown, entryValues, trialStarting, onReviewRecord, onRunTrial,
}: {
  draft: CaliberDraft
  validations: DraftValidationsOut | null
  validationsError: unknown
  reviewOut: Record<string, DraftValidationRecord['review']>
  reviewNotes: Record<string, string>
  setReviewNotes: React.Dispatch<React.SetStateAction<Record<string, string>>>
  reviewBusy: string | null
  reviewError: unknown
  expectValues: Record<string, string>
  setExpectValues: React.Dispatch<React.SetStateAction<Record<string, string>>>
  expectSource: DraftExpectationSource
  setExpectSource: (source: DraftExpectationSource) => void
  recordsShown: number
  setRecordsShown: React.Dispatch<React.SetStateAction<number>>
  entryValues: Record<string, string>
  trialStarting: boolean
  onReviewRecord: (draft: CaliberDraft, recordId: string, decision: 'confirmed' | 'rejected') => void
  onRunTrial: (draft: CaliberDraft, expect?: { metrics: Record<string, unknown>; source: DraftExpectationSource }) => void
}) {
  const canReview = useAtLeast('builder')
  if (!validations && !validationsError) return null
  const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
  const names = new Map(metrics.map((metric) => [metric.id as string, metric.name]))
  const summary = validations?.summary
  const records = validations?.records ?? []
  const baseline = validations?.field_baseline
  const basisRecord = summary?.record_id ? records.find((record) => record.id === summary.record_id) ?? null : null
  const basisReview = basisRecord ? (reviewOut[basisRecord.id] ?? basisRecord.review) : null
  const computedMissed = basisRecord ? uncoveredMetrics(basisRecord, basisReview) : []
  const serverMissed = summary?.unconfirmed
  const missed = serverMissed?.length ? serverMissed : computedMissed
  const confirmedNames = (summary?.confirmed?.length ? summary.confirmed : basisReview?.scope ?? [])
    .filter((id) => (basisRecord?.metric_ids ?? []).includes(id))
  const scopeIncomplete = basisRecord !== null && basisReview?.decision === 'confirmed' && missed.length > 0
  const partial = summary?.verdict === 'verified'
    && (scopeIncomplete || (basisRecord !== null && !scopeCovers(basisRecord, basisReview)))
  const nameOf = (id: string) => names.get(id) ?? id
  const verdict = partial ? 'unverified' : summary?.verdict ?? 'none'
  const tone = verdict === 'verified' ? 'var(--ok)'
    : verdict === 'mismatched' || verdict === 'failed' || verdict === 'rejected' ? 'var(--err)'
      : verdict === 'none' || verdict === 'stale' ? 'var(--text-faint)' : 'var(--warn)'

  const fields = draft.entry_fields ?? []
  const filled = metrics.filter((metric) => (expectValues[metric.id as string] ?? '').trim())
  const ready = filled.length === metrics.length
  const wanted: Record<string, unknown> = {}
  for (const metric of metrics) wanted[metric.id as string] = expectValue(expectValues[metric.id as string] ?? '')

  return (
    <div className="rounded-md border bg-elev px-3 py-2" data-draft-validation={verdict}
         data-validation-partial={partial ? 'recalled' : scopeIncomplete ? 'scope' : 'no'}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-2xs font-semibold text-dim">{V.title}</h3>
        <span className="text-2xs" data-draft-validation-verdict={verdict} style={{ color: tone }}>
          {V.verdict[verdict] ?? verdict}
        </span>
        {summary?.method?.version != null && <span className="text-[10px] text-faint">{V.method(summary.method.version)}</span>}
      </div>
      {summary && <p className="mt-1 text-2xs leading-relaxed text-dim" data-draft-validation-reason="">{summary.reason}</p>}
      {(partial || scopeIncomplete) && (
        <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]" data-draft-validation-partial="">
          {V.partialVerdict(confirmedNames.map(nameOf).join('、'), missed.map(nameOf).join('、') || '—',
            basisRecord?.metric_ids?.length ?? 0)}
        </p>
      )}
      {summary && (
        <p className="mt-0.5 text-2xs text-faint" data-draft-validation-basis="">
          {basisRecord
            ? V.basis(basisRecord.created_at ? formatDateTime(basisRecord.created_at) : '—',
              V.status[basisRecord.status] ?? basisRecord.status)
            : V.basisNone}
        </p>
      )}
      {summary && (
        <p className="mt-0.5 tnum text-2xs text-faint" data-draft-validation-counts="">
          {V.counts(summary.counts?.total ?? 0, summary.counts?.covering ?? 0)}
        </p>
      )}
      {!!summary?.semantic_gaps?.length && (
        <p className="mt-0.5 text-2xs text-faint" data-draft-validation-gaps="">
          {V.semanticGaps(summary.semantic_gaps.length)}
        </p>
      )}
      {baseline && (
        <p className="mt-0.5 text-2xs leading-relaxed text-faint" data-draft-baseline={baseline.status}>
          {V.baseline}：{baseline.status === 'known'
            ? V.baselineKnown(baseline.accepted_revision ?? null, (baseline.metric_ids ?? []).length,
              baseline.field_count ?? 0)
            : V.baselineUnknown}
          {baseline.status === 'known' ? `。${V.baselineHint}` : ''}
        </p>
      )}
      {validationsError != null && <div className="mt-1"><ErrorState error={validationsError} compact /></div>}
      {reviewError != null && <div className="mt-1"><ErrorState error={reviewError} compact /></div>}

      {records.length === 0 ? (
        <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-validations-empty="">
          {V.empty}：{V.emptyBody}
        </p>
      ) : (
        <>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2">
            <h4 className="text-2xs font-semibold text-faint">{V.records}</h4>
            <span className="text-[10px] text-faint">{V.fieldsHint}</span>
          </div>
          <ul className="mt-1 space-y-1.5">
            {records.slice(0, recordsShown).map((record) => {
              const review = reviewOut[record.id] ?? record.review
              const expected = record.expected
              const ids = record.metric_ids ?? []
              const covering = record.covers_current
              return (
                <li key={record.id} className="rounded border bg-panel px-2 py-1.5"
                    data-draft-validation-record={record.id} data-record-status={record.status}>
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
                    <span className="font-medium text-dim" data-record-status-label="">
                      {V.status[record.status] ?? record.status}
                    </span>
                    <span className={clsx(covering === true ? 'text-dim' : 'text-faint')}
                          data-record-covers={covering === true ? 'yes' : covering === false ? 'no' : 'unknown'}>
                      {coversText(covering)}
                    </span>
                    {record.created_at && <span className="text-faint">{formatDateTime(record.created_at)}</span>}
                    {record.run_id && (
                      <Link to={`/runs/${record.run_id}`} className="text-accent hover:underline" data-record-run="">
                        {V.openRun}
                      </Link>
                    )}
                  </div>

                  <div className="mt-1 text-2xs leading-relaxed" data-record-expect={expected ? 'given' : 'none'}>
                    <span className="text-faint">{V.expected}</span>
                    {expected ? (
                      <>
                        <span className="text-faint"> · {V.expectedSource}：{V.sources[expected.source] ?? expected.source}</span>
                        <ul className="mt-0.5 space-y-0.5">
                          {ids.map((metricId) => (
                            <li key={metricId} className="flex flex-wrap items-baseline gap-x-2"
                                data-record-expect-value={metricId}>
                              <span className="text-dim [overflow-wrap:anywhere]">{names.get(metricId) ?? metricId}</span>
                              <span className="tnum">{valueText(expected.metrics?.[metricId])}</span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : (
                      <span className="text-[var(--warn)]" data-record-no-expect="">：{V.expectedNone}</span>
                    )}
                  </div>

                  <div className="mt-1 text-2xs leading-relaxed">
                    <span className="text-faint">{V.actual}</span>
                    <ul className="mt-0.5 space-y-0.5">
                      {ids.map((metricId) => {
                        const actual = record.actual?.[metricId] ?? {}
                        const shown = actual.masked ? V.masked : (actual.rendered || valueText(actual.value) || V.noValue)
                        return (
                          <li key={metricId} className="flex flex-wrap items-baseline gap-x-2"
                              data-record-actual={metricId} data-record-actual-ok={String(actual.ok ?? 'unknown')}>
                            <span className="text-dim [overflow-wrap:anywhere]">{names.get(metricId) ?? metricId}</span>
                            <span className="tnum">{shown}</span>
                            {actual.rows != null && <span className="text-faint">{V.rowsHint(actual.rows)}</span>}
                            <span style={{ color: actual.ok === true ? 'var(--ok)'
                              : actual.ok === false ? 'var(--err)' : 'var(--text-faint)' }}>
                              {actual.ok === true ? V.ok : actual.ok === false ? V.notOk : V.unknown}
                            </span>
                          </li>
                        )
                      })}
                    </ul>
                  </div>

                  {!!record.problems?.length && (
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-2xs leading-relaxed text-[var(--warn)]"
                        data-record-problems="">
                      {record.problems.map((problem, i) => <li key={`${i}:${problem}`}>{problem}</li>)}
                    </ul>
                  )}

                  <div className="mt-1 border-t pt-1" data-record-review={review?.decision ?? 'none'}>
                    {review ? (
                      <p className="text-2xs leading-relaxed text-dim" data-record-review-line="">
                        {review.decision === 'rejected' ? V.reviewRejected : V.reviewConfirmed}
                        <span className="text-faint"> · {V.reviewBy(review.actor || '—', !!review.verified)}</span>
                        {review.created_at ? <span className="text-faint"> · {formatDateTime(review.created_at)}</span> : ''}
                        {!!review.scope?.length && (
                          <span className="text-faint">
                            {' · '}{V.reviewScope}：{review.scope.map((id) => names.get(id) ?? id).join('、')}
                          </span>
                        )}
                        {review.decision === 'confirmed' && !scopeCovers(record, review) && (
                          <span className="text-[var(--warn)]" data-record-review-partial="">
                            {' · '}{V.reviewPartial}
                            {uncoveredMetrics(record, review).length > 0
                              ? `（没盖住${uncoveredMetrics(record, review).map((id) => names.get(id) ?? id).join('、')}）`
                              : ''}
                          </span>
                        )}
                        {review.covers_current === true ? (
                          <span className="text-faint" data-record-review-covers="yes"> · {V.reviewCovers}</span>
                        ) : review.covers_current === false ? (
                          <span className="text-faint" data-record-review-covers="no"> · {V.reviewNotCovering}</span>
                        ) : (
                          <span className="text-faint" data-record-review-covers="unknown">
                            {' · '}{V.reviewCoversUnknown}
                          </span>
                        )}
                        {review.note && <span className="text-faint"> · {review.note}</span>}
                      </p>
                    ) : <p className="text-2xs text-faint">{V.noReview}</p>}
                    {canReview && (
                      <div className="mt-1 flex flex-wrap items-center gap-2">
                        <input className="field w-44 text-2xs" data-draft-review-note={record.id}
                               aria-label={V.reviewNote} placeholder={V.reviewNotePlaceholder}
                               value={reviewNotes[record.id] ?? ''} disabled={reviewBusy === record.id}
                               onChange={(e) => setReviewNotes((cur) => ({ ...cur, [record.id]: e.target.value }))} />
                        <button type="button" className="btn btn-xs" data-draft-review="confirmed"
                                disabled={reviewBusy === record.id}
                                onClick={() => onReviewRecord(draft, record.id, 'confirmed')}>
                          {reviewBusy === record.id ? <Spinner size={11} /> : <Check size={11} aria-hidden />} {V.confirm}
                        </button>
                        <button type="button" className="btn btn-xs btn-ghost" data-draft-review="rejected"
                                disabled={reviewBusy === record.id}
                                onClick={() => onReviewRecord(draft, record.id, 'rejected')}>
                          {V.reject}
                        </button>
                        <span className="text-[10px] text-faint">{V.reviewScopeHint}</span>
                      </div>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
          {records.length > recordsShown && (
            <button type="button" className="btn btn-xs btn-ghost mt-1" data-draft-validations-more=""
                    onClick={() => setRecordsShown((cur) => cur + RECORDS_SHOWN)}>
              {V.more(records.length - recordsShown)}
            </button>
          )}
        </>
      )}

      {metrics.length > 0 && (
        <details className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-expect="">
          <summary className="cursor-pointer text-2xs font-semibold text-dim">{V.trialWithExpect}</summary>
          <p className="mt-0.5 text-2xs leading-relaxed text-faint">
            {V.trialWithExpectHint}。{V.expectedHint}
          </p>
          <ul className="mt-1 space-y-0.5">
            {metrics.map((metric) => (
              <li key={metric.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs">
                <span className="text-dim [overflow-wrap:anywhere]">{V.expectFor(metric.name)}</span>
                <input className="field w-32 text-2xs" data-draft-expect-input={metric.id}
                       aria-label={V.expectFor(metric.name)} disabled={trialStarting}
                       value={expectValues[metric.id as string] ?? ''}
                       onChange={(e) => setExpectValues((cur) => ({ ...cur, [metric.id as string]: e.target.value }))} />
              </li>
            ))}
          </ul>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-2xs">
            <label className="flex items-center gap-1 text-faint">
              {V.expectSourceLabel}
              <select className="field h-[26px] text-2xs" value={expectSource} data-draft-expect-source=""
                      disabled={trialStarting}
                      onChange={(e) => setExpectSource(e.target.value as DraftExpectationSource)}>
                {(Object.keys(V.sources) as DraftExpectationSource[]).map((key) => (
                  <option key={key} value={key}>{V.sources[key]}</option>
                ))}
              </select>
            </label>
            <button type="button" className="btn btn-xs" data-draft-expect-submit=""
                    disabled={trialStarting || !ready || !entryReady(fields, entryValues)}
                    onClick={() => onRunTrial(draft, { metrics: wanted, source: expectSource })}>
              {trialStarting ? <Spinner size={11} /> : <Play size={11} aria-hidden />}
              {trialStarting ? V.expectSubmitting : V.expectSubmit}
            </button>
            {!ready && <span className="text-faint" data-draft-expect-needall="">{V.expectNeedAll}</span>}
          </div>
        </details>
      )}
    </div>
  )
}

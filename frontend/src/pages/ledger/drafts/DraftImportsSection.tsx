import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, FileSearch, RotateCw, X } from 'lucide-react'
import clsx from 'clsx'
import { api, ApiError } from '../../../api/client'
import { ErrorState, Notice, Spinner, toast } from '../../../components/ui'
import { formatDateTime } from '../../../lib/format'
import { LEDGER_UI } from '../../../lib/termsLedger'
import type {
  CaliberDraft, DraftImportAcceptOut, DraftImportItem, DraftImportRun, DraftValidationsOut,
} from '../../../typesLedger'
import {
  cellText, cleanPaths, fieldLabel, itemAccepts, newRequestId, pickKey, prunePicks, unfinishedItems,
} from './helpers'

const I = LEDGER_UI.imports

export interface ImportStaleInfo {
  code: 'stale_revision' | 'import_stale_l'
  current: number | null
  base: number | null
  names: string
}

export function useDraftImports({
  openSeq, refreshDetail, loadValidations, loadList,
}: {
  openSeq: React.MutableRefObject<number>
  refreshDetail: (id: string) => Promise<void>
  loadValidations: (id: string) => void
  loadList: () => void
}) {
  const [importRuns, setImportRuns] = useState<DraftImportRun[] | null>(null)
  const [importsError, setImportsError] = useState<unknown>(null)
  const [importId, setImportId] = useState<string | null>(null)
  const [importBusy, setImportBusy] = useState<string | null>(null)
  const [importError, setImportError] = useState<unknown>(null)
  const [importPicks, setImportPicks] = useState<Record<string, 'accept' | 'reject'>>({})
  const [alignPicks, setAlignPicks] = useState<Record<string, string>>({})
  const [importStale, setImportStale] = useState<ImportStaleInfo | null>(null)
  const [importResult, setImportResult] = useState<DraftImportAcceptOut | null>(null)
  const [importAccepted, setImportAccepted] = useState(false)
  const [importRejected, setImportRejected] = useState(false)
  const [importNote, setImportNote] = useState('')
  const importRequest = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })
  const [importPollId, setImportPollId] = useState<string | null>(null)

  const resetImports = useCallback(() => {
    setImportRuns(null)
    setImportsError(null)
    setImportId(null)
    setImportBusy(null)
    setImportError(null)
    setImportPicks({})
    setAlignPicks({})
    setImportStale(null)
    setImportResult(null)
    setImportAccepted(false)
    setImportRejected(false)
    setImportNote('')
    setImportPollId(null)
    importRequest.current = { signature: '', requestId: '' }
  }, [])

  const putImport = useCallback((run: DraftImportRun) => {
    setImportRuns((cur) => [run, ...(cur ?? []).filter((row) => row.run_id !== run.run_id)])
  }, [])

  const loadImports = useCallback((id: string) => {
    const seq = openSeq.current
    api.caliberLedger.imports(id).then(
      (out) => {
        if (seq !== openSeq.current) return
        setImportRuns(out.runs)
        setImportsError(null)
        setImportId((cur) => (cur && out.runs.some((run) => run.run_id === cur) ? cur : (out.runs[0]?.run_id ?? null)))
        const running = out.runs.find((run) => run.status === 'queued' || run.status === 'running')
        if (running) setImportPollId(running.run_id)
      },
      (e) => { if (seq === openSeq.current) { setImportRuns(null); setImportsError(e) } },
    )
  }, [openSeq])

  /**
   * 正在跑的那一次重导入：每两秒取一次现状（后台在抽候选、算三方），到终态就停。被中断的不会自动续跑，
   * 页面上由人点「恢复」显式排回后台。
   */
  useEffect(() => {
    if (!importPollId) return
    const until = Date.now() + 5 * 60_000
    let alive = true
    const timer = setInterval(() => {
      api.caliberLedger.importRun(importPollId).then((run) => {
        if (!alive) return
        putImport(run)
        if (run.status === 'queued' || run.status === 'running') {
          if (Date.now() > until) setImportPollId(null)
          return
        }
        setImportPollId(null)
      }, (e) => {
        if (!alive) return
        setImportPollId(null)
        setImportError(e)
      })
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [importPollId, putImport])

  const reloadImportRun = useCallback(async (runId: string): Promise<DraftImportRun | null> => {
    try {
      const fresh = await api.caliberLedger.importRun(runId)
      putImport(fresh)
      setImportPicks((cur) => prunePicks(cur, fresh))
      return fresh
    } catch (e) {
      setImportError(e)
      return null
    }
  }, [putImport])

  const refreshImportRun = useCallback(async (runId: string): Promise<DraftImportRun | null> => {
    try {
      const fresh = await api.caliberLedger.refreshImport(runId)
      putImport(fresh)
      setImportPicks((cur) => prunePicks(cur, fresh))
      return fresh
    } catch (e) {
      setImportError(e)
      return null
    }
  }, [putImport])

  const createImport = useCallback(async (draft: CaliberDraft) => {
    if (!draft.workflow_id) { toast.warn(I.readOnly); return }
    setImportBusy('create')
    setImportError(null)
    setImportStale(null)
    setImportResult(null)
    setImportAccepted(false)
    setImportRejected(false)
    const signature = `import|${draft.id}|${draft.revision ?? ''}`
    if (importRequest.current.signature !== signature) {
      importRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const run = await api.caliberLedger.createImport(draft.id, importRequest.current.requestId)
      putImport(run)
      setImportId(run.run_id)
      setImportPicks({})
      setAlignPicks({})
      importRequest.current = { signature: '', requestId: '' }
      if (run.status === 'queued' || run.status === 'running') setImportPollId(run.run_id)
      toast.ok(I.started)
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }, [putImport])

  const alignImportItem = useCallback(async (run: DraftImportRun, item: DraftImportItem, metricId: string) => {
    setImportBusy(`align:${item.item_id}`)
    setImportError(null)
    try {
      const fresh = await api.caliberLedger.alignImportItem(run.run_id, item.item_id, metricId)
      putImport(fresh)
      setImportPicks((cur) => {
        const next: Record<string, 'accept' | 'reject'> = {}
        for (const [key, value] of Object.entries(cur)) if (!key.startsWith(`${item.item_id}\u0000`)) next[key] = value
        return next
      })
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }, [putImport])

  const acceptImportFields = useCallback(async (
    draft: CaliberDraft,
    run: DraftImportRun,
    items: { item_id: string; metric_id?: string; fields: string[] }[],
  ) => {
    const picked = items.filter((item) => item.fields.length)
    if (!picked.length) { toast.warn(I.nothingSelected); return }
    if (draft.revision == null) { toast.warn(I.needRevision); return }
    setImportBusy('accept')
    setImportError(null)
    setImportStale(null)
    setImportResult(null)
    setImportRejected(false)
    const signature = `accept|${run.run_id}|${JSON.stringify(picked)}`
    if (importRequest.current.signature !== signature) {
      importRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const out = await api.caliberLedger.acceptImport(run.run_id, {
        items: picked, expected_revision: draft.revision, request_id: importRequest.current.requestId,
      })
      putImport(out)
      setImportResult(out)
      setImportAccepted(true)
      setImportPicks({})
      importRequest.current = { signature: '', requestId: '' }
      toast.ok(I.accepted(out.revision))
      void refreshDetail(draft.id)
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      const code = e instanceof ApiError && e.status === 409 ? e.code : null
      if (code === 'stale_revision' || code === 'import_stale_l') {
        const body = e instanceof ApiError ? (e.body as { current_version?: unknown
          fields?: { path?: string }[] } | undefined) : undefined
        const told = Number(body?.current_version)
        const names = (body?.fields ?? []).map((row) => fieldLabel(String(row.path ?? ''), draft.binding)).join('、')
        setImportStale({
          code: code === 'import_stale_l' ? 'import_stale_l' : 'stale_revision',
          current: Number.isFinite(told) ? told : null, base: draft.revision ?? null, names,
        })
        if (code === 'import_stale_l') await refreshImportRun(run.run_id)
        else await reloadImportRun(run.run_id)
        void refreshDetail(draft.id)
      } else {
        setImportError(e)
      }
    } finally {
      setImportBusy(null)
    }
  }, [loadList, loadValidations, putImport, refreshDetail, refreshImportRun, reloadImportRun])

  const rejectImportFields = useCallback(async (
    run: DraftImportRun,
    items: { item_id: string; metric_id?: string; seq?: number; fields: string[] }[],
  ) => {
    const picked = items.filter((item) => item.fields.length)
    if (!picked.length) { toast.warn(I.nothingSelected); return }
    setImportBusy('reject')
    setImportError(null)
    setImportResult(null)
    setImportAccepted(false)
    try {
      const fresh = await api.caliberLedger.rejectImport(run.run_id, {
        items: picked, ...(importNote.trim() ? { note: importNote.trim() } : {}),
      })
      putImport(fresh)
      setImportRejected(true)
      setImportPicks({})
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }, [importNote, putImport])

  const resumeImportRun = useCallback(async (run: DraftImportRun) => {
    setImportBusy('resume')
    setImportError(null)
    try {
      const fresh = await api.caliberLedger.resumeImport(run.run_id)
      putImport(fresh)
      setImportPollId(fresh.run_id)
      toast.ok(I.resumed)
    } catch (e) {
      setImportError(e)
    } finally {
      setImportBusy(null)
    }
  }, [putImport])

  return {
    importRuns, importsError, importId, setImportId, importBusy, importError,
    importPicks, setImportPicks, alignPicks, setAlignPicks, importStale,
    importResult, importAccepted, importRejected, importNote, setImportNote,
    resetImports, loadImports, createImport, alignImportItem, acceptImportFields, rejectImportFields, resumeImportRun,
  }
}

export function DraftImportsSection({
  draft, validations, importRuns, importsError, importId, setImportId, importBusy, importError,
  importPicks, setImportPicks, alignPicks, setAlignPicks, importStale,
  importResult, importAccepted, importRejected, importNote, setImportNote,
  onCreateImport, onAlignImportItem, onAcceptImportFields, onRejectImportFields, onResumeImportRun,
}: {
  draft: CaliberDraft
  validations: DraftValidationsOut | null
  importRuns: DraftImportRun[] | null
  importsError: unknown
  importId: string | null
  setImportId: (id: string | null) => void
  importBusy: string | null
  importError: unknown
  importPicks: Record<string, 'accept' | 'reject'>
  setImportPicks: React.Dispatch<React.SetStateAction<Record<string, 'accept' | 'reject'>>>
  alignPicks: Record<string, string>
  setAlignPicks: React.Dispatch<React.SetStateAction<Record<string, string>>>
  importStale: ImportStaleInfo | null
  importResult: DraftImportAcceptOut | null
  importAccepted: boolean
  importRejected: boolean
  importNote: string
  setImportNote: (value: string) => void
  onCreateImport: (draft: CaliberDraft) => void
  onAlignImportItem: (run: DraftImportRun, item: DraftImportItem, metricId: string) => void
  onAcceptImportFields: (draft: CaliberDraft, run: DraftImportRun, items: { item_id: string; metric_id?: string; fields: string[] }[]) => void
  onRejectImportFields: (run: DraftImportRun, items: { item_id: string; metric_id?: string; seq?: number; fields: string[] }[]) => void
  onResumeImportRun: (run: DraftImportRun) => void
}) {
  if (!draft.workflow_id) return null
  const runs = importRuns ?? []
  const active = runs.find((run) => run.run_id === importId) ?? null
  const metrics = (draft.metrics ?? []).filter((metric) => metric.id)
  const nameOf = (id: string) => metrics.find((metric) => metric.id === id)?.name ?? I.unknownMetric
  const baseline = validations?.field_baseline ?? draft.field_baseline
  const coverage = baseline?.coverage
  const coverageStatus = coverage?.status ?? (baseline?.status === 'known' ? 'complete' : 'unknown')
  const missingFields = Object.values(coverage?.metrics ?? {})
    .reduce((total, row) => total + (row.missing?.length ?? 0), 0)
  const uncoveredMetrics = coverage?.uncovered_metric_ids?.length ?? 0
  const coverageText = coverageStatus === 'complete' ? I.coverageComplete
    : coverageStatus === 'partial' ? I.coveragePartial(missingFields, uncoveredMetrics)
      : I.coverageUnknown
  const picking = active ? active.items.map((item) => ({
    item,
    paths: item.fields.filter((cell) => importPicks[pickKey(item.item_id, cell.path)] === 'accept'
      && cell.decided == null && cell.acceptable && itemAccepts(item)).map((cell) => cell.path),
    rejects: item.fields.filter((cell) => importPicks[pickKey(item.item_id, cell.path)] === 'reject'
      && cell.decided == null).map((cell) => cell.path),
  })) : []
  const acceptItems = picking.filter((row) => row.paths.length)
    .map((row) => ({ item_id: row.item.item_id, metric_id: row.item.metric_id, fields: row.paths }))
  const rejectItems = picking.filter((row) => row.rejects.length).map((row) => ({
    item_id: row.item.item_id, fields: row.rejects,
    ...(row.item.metric_id ? { metric_id: row.item.metric_id } : { seq: row.item.seq }),
  }))
  const acceptCount = acceptItems.reduce((total, item) => total + item.fields.length, 0)
  const rejectCount = rejectItems.reduce((total, item) => total + item.fields.length, 0)
  const resumable = active != null && (active.status === 'interrupted' || active.status === 'failed')
  const busy = importBusy != null
  return (
    <div className="rounded-md border bg-elev px-3 py-2" data-draft-imports={active?.status ?? 'none'}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-2xs font-semibold text-dim">{I.title}</h3>
        <span className="text-[10px] text-faint">{I.hint}</span>
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-coverage={coverageStatus}>
        {I.coverageLabel}：{coverageText}。{I.coverageHint}
      </p>

      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-xs" data-draft-import-create="" disabled={busy}
                onClick={() => onCreateImport(draft)}>
          {importBusy === 'create' ? <Spinner size={11} /> : <FileSearch size={11} aria-hidden />}
          {importBusy === 'create' ? I.creating : I.create}
        </button>
        <span className="text-[10px] text-faint">{I.createHint}</span>
      </div>
      {importsError != null && <div className="mt-1"><ErrorState error={importsError} compact /></div>}

      {runs.length === 0 ? (
        <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-runs={0}>
          {I.runs}：{I.runsEmpty}
        </p>
      ) : (
        <div className="mt-2" data-draft-import-runs={runs.length}>
          <h4 className="text-2xs font-semibold text-faint">{I.runs}</h4>
          <ul className="mt-0.5 space-y-0.5">
            {runs.map((run) => (
              <li key={run.run_id}>
                <button type="button" className={clsx('btn btn-xs w-full justify-start text-left', 'btn-ghost')}
                        aria-pressed={run.run_id === importId} data-draft-import-run={run.run_id}
                        onClick={() => setImportId(run.run_id)}>
                  <span className="[overflow-wrap:anywhere]">
                    {I.runLabel(run.created_at ? formatDateTime(run.created_at) : '—', run.items_decided, run.items_total)}
                  </span>
                  <span className="text-faint"> · {I.status[run.status] ?? run.status}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {active && (
        <div className="mt-2 rounded border bg-panel px-2 py-1.5" data-draft-import-detail={active.run_id}>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
            <span className="font-medium text-dim" data-draft-import-status={active.status}>
              {I.status[active.status] ?? active.status}
            </span>
            {(active.status === 'queued' || active.status === 'running') && <Spinner size={11} />}
            <span className="text-faint">{I.itemsTotal(active.items_total)}</span>
            {active.conflicts.length > 0 && (
              <span className="text-[var(--warn)]" data-draft-import-conflicts={active.conflicts.length}>
                {I.conflicts(active.conflicts.length)}
              </span>
            )}
            {active.created_at && <span className="text-faint">{I.createdAt(formatDateTime(active.created_at))}</span>}
          </div>
          {active.error && <Notice tone="warn">{active.error}</Notice>}
          {resumable && (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-xs" data-draft-import-resume="" disabled={busy}
                      onClick={() => onResumeImportRun(active)}>
                {importBusy === 'resume' ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />}
                {importBusy === 'resume' ? I.resuming : I.resume}
              </button>
              <span className="text-[10px] text-faint">{I.resumeHint}</span>
              <span className="tnum text-[10px] text-faint" data-draft-import-unfinished={unfinishedItems(active)}>
                {I.unfinished(unfinishedItems(active))}
              </span>
            </div>
          )}

          {active.items.length === 0 ? (
            <p className="mt-1 text-2xs leading-relaxed text-faint" data-draft-import-empty="">{I.noItems}</p>
          ) : (
            <ul className="mt-1.5 space-y-2">
              {active.items.map((item) => {
                const aligned = !!item.metric_id
                const alignMode = String(item.align?.mode ?? '')
                const needsAlign = !aligned || alignMode === 'manual'
                const clean = cleanPaths(item)
                return (
                  <li key={item.item_id} className="rounded border bg-elev px-2 py-1.5"
                      data-draft-import-item={item.metric_id || `#${item.seq}`} data-import-item-status={item.status}>
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-2xs">
                      <span className="font-medium text-dim [overflow-wrap:anywhere]">{item.name || I.unknownMetric}</span>
                      {aligned && <span className="text-faint">· {nameOf(item.metric_id)}</span>}
                      <span className={clsx(item.status === 'accepted' ? 'text-[var(--ok)]'
                        : item.status === 'rejected' || item.status === 'failed' ? 'text-faint' : 'text-[var(--warn)]')}
                            data-draft-import-item-status-label={item.status}>
                        {I.itemStatus[item.status] ?? I.state[item.status] ?? item.status}
                      </span>
                    </div>

                    {needsAlign && (
                      <div className="mt-1 flex flex-wrap items-center gap-2" data-draft-import-align="">
                        <span className="text-2xs text-[var(--warn)]">{I.itemUnaligned}</span>
                        <select className="field h-[26px] text-2xs" data-draft-import-align-select={item.item_id}
                                aria-label={I.align} disabled={busy}
                                value={alignPicks[item.item_id] ?? item.metric_id}
                                onChange={(e) => setAlignPicks((cur) => ({ ...cur, [item.item_id]: e.target.value }))}>
                          <option value="">{I.alignNone}</option>
                          {metrics.map((metric) => (
                            <option key={metric.id} value={metric.id as string}>{metric.name}</option>
                          ))}
                        </select>
                        <button type="button" className="btn btn-xs" data-draft-import-align-submit={item.item_id}
                                disabled={busy} onClick={() => onAlignImportItem(active, item, alignPicks[item.item_id] ?? '')}>
                          {importBusy === `align:${item.item_id}` ? <Spinner size={11} /> : null} {I.alignAction}
                        </button>
                        <span className="text-[10px] text-faint">{I.alignHint}</span>
                      </div>
                    )}

                    {(alignMode === 'manual' || alignMode === 'new') && (
                      <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]"
                         data-draft-import-reason={alignMode}>
                        {I.reasonLabel}：{I.reason[alignMode] ?? alignMode}
                        {item.align?.reason ? ` · ${item.align.reason}` : ''}
                      </p>
                    )}
                    {item.error && <p className="mt-0.5 text-2xs leading-relaxed text-[var(--warn)]">{item.error}</p>}

                    <ul className="mt-1 space-y-1">
                      {item.fields.map((cell) => {
                        const choice = importPicks[pickKey(item.item_id, cell.path)] ?? ''
                        const canAccept = itemAccepts(item) && cell.acceptable && cell.decided !== 'accepted'
                        const values: [string, string | null, string][] = [
                          ['b', cellText(cell, 'b', draft.binding), I.b],
                          ['l', cellText(cell, 'l', draft.binding), I.l],
                          ['r', cellText(cell, 'r', draft.binding), I.r],
                        ]
                        return (
                          <li key={cell.path} className="text-2xs leading-relaxed"
                              data-draft-import-field={`${item.item_id}:${cell.path}`} data-import-field-state={cell.state}>
                            <div className="flex flex-wrap items-baseline gap-x-2">
                              <span className="font-medium text-dim">{fieldLabel(cell.path, draft.binding)}</span>
                              <span className="text-faint" data-draft-import-field-state-label={cell.state}>
                                {I.state[cell.state] ?? cell.state}
                              </span>
                              {cell.decided === 'accepted' && <span className="text-[var(--ok)]">{I.accept}</span>}
                              {cell.decided === 'rejected' && <span className="text-faint">{I.reject}</span>}
                              {cell.state === 'missing' && (
                                <span className="text-[var(--warn)]" data-draft-import-missing="">
                                  {I.missingNoDelete}
                                </span>
                              )}
                            </div>
                            <ul className="mt-0.5 space-y-0.5 pl-2">
                              {values.map(([key, text, label]) => text != null && (
                                <li key={key} className="text-faint" data-draft-import-value={key}>
                                  {label}：<span className="text-dim [overflow-wrap:anywhere]">{text}</span>
                                </li>
                              ))}
                            </ul>
                            <p className="mt-0.5 text-faint">{I.stateHint[cell.state] ?? ''}</p>
                            {cell.decided == null && (
                              <div className="mt-0.5 flex flex-wrap items-center gap-2">
                                <label className="flex items-center gap-1 text-dim">
                                  <input type="radio" className="accent-[var(--accent)]"
                                         name={`pick:${item.item_id}:${cell.path}`} disabled={!canAccept || busy}
                                         data-draft-import-pick-accept={`${item.item_id}:${cell.path}`}
                                         checked={choice === 'accept'}
                                         onChange={() => setImportPicks((cur) => ({
                                           ...cur, [pickKey(item.item_id, cell.path)]: 'accept' }))} />
                                  {I.accept}
                                </label>
                                <label className="flex items-center gap-1 text-dim">
                                  <input type="radio" className="accent-[var(--accent)]"
                                         name={`pick:${item.item_id}:${cell.path}`} disabled={busy}
                                         data-draft-import-pick-reject={`${item.item_id}:${cell.path}`}
                                         checked={choice === 'reject'}
                                         onChange={() => setImportPicks((cur) => ({
                                           ...cur, [pickKey(item.item_id, cell.path)]: 'reject' }))} />
                                  {I.reject}
                                </label>
                              </div>
                            )}
                          </li>
                        )
                      })}
                    </ul>

                    {clean.length > 0 && (
                      <div className="mt-1">
                        <button type="button" className="btn btn-xs" data-draft-import-accept-clean={item.item_id}
                                disabled={busy}
                                onClick={() => onAcceptImportFields(draft, active,
                                  [{ item_id: item.item_id, metric_id: item.metric_id, fields: clean }])}>
                          {I.acceptClean(clean.length)}
                        </button>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}

          {active.items.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-xs btn-primary" data-draft-import-accept=""
                      disabled={busy || !acceptCount}
                      onClick={() => onAcceptImportFields(draft, active, acceptItems)}>
                {importBusy === 'accept' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
                {importBusy === 'accept' ? I.accepting : I.acceptSelected(acceptCount)}
              </button>
              <button type="button" className="btn btn-xs" data-draft-import-reject="" disabled={busy || !rejectCount}
                      onClick={() => onRejectImportFields(active, rejectItems)}>
                {importBusy === 'reject' ? <Spinner size={11} /> : <X size={11} aria-hidden />}
                {importBusy === 'reject' ? I.rejecting : I.rejectSelected(rejectCount)}
              </button>
              {!acceptCount && !rejectCount && <span className="text-[10px] text-faint">{I.nothingSelected}</span>}
              <input className="field h-[26px] w-44 text-2xs" data-draft-import-note="" aria-label={I.reject}
                     placeholder={I.notePlaceholder} value={importNote} disabled={busy}
                     onChange={(e) => setImportNote(e.target.value)} />
            </div>
          )}

          {importStale && (
            <div className="mt-1.5" data-draft-import-stale={importStale.code}>
              <Notice tone="warn">
                {importStale.code === 'import_stale_l'
                  ? I.staleL(importStale.names)
                  : I.stale(importStale.current, importStale.base)}
              </Notice>
            </div>
          )}
          {importError != null && <div className="mt-1"><ErrorState error={importError} compact /></div>}
          {(importAccepted || importRejected) && (
            <div className="mt-1.5 rounded border bg-elev px-2 py-1 text-2xs leading-relaxed"
                 data-draft-import-result={importAccepted ? 'accepted' : 'rejected'}>
              {importAccepted && (
                <>
                  <p className="text-dim" data-draft-import-result-version={importResult?.revision ?? ''}>
                    {I.accepted(importResult?.revision ?? null)}
                    {importResult?.replayed && <span className="text-faint"> · {I.replayed}</span>}
                  </p>
                  <p className="text-faint">{I.acceptedHint}</p>
                </>
              )}
              {importRejected && <p className="text-dim">{I.rejected}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

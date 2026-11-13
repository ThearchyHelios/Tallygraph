/**
 * 从业务文档起草口径（/ledger/drafts；服务端 app/ledger/docdraft.py、docattach.py）。
 *
 * 页面作为编排外壳组装各子面板：
 * - `drafts/helpers.ts`：阶段映射、幂等键、K05/K06/K11/K12 纯函数
 * - `drafts/DraftSharedBlocks.tsx`：发起导入栏、阶段徽标、指标列表、SQL 模板、发布前检查与接运行区
 * - `drafts/DraftTrialSection.tsx`：自动试运行状态轮询与入口字段表单
 * - `drafts/DraftRulesSection.tsx`：业务规则工作台（K06）
 * - `drafts/DraftImportsSection.tsx`：文档更新与三方逐字段重导入（K11）
 * - `drafts/DraftNlSection.tsx`：用一句话改口径预览与确认（K12）
 * - `drafts/DraftValidationSection.tsx`：验证结论、独立期望与审阅记录（K05）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronLeft, FileText, PenLine, RefreshCw, ScrollText } from 'lucide-react'
import clsx from 'clsx'
import { api, ApiError } from '../../api/client'
import { EmptyState, ErrorState, Notice, PageHeader, Skeleton, Spinner, promptDialog, toast } from '../../components/ui'
import { formatDateTime, formatTime } from '../../lib/format'
import { LEDGER_UI } from '../../lib/termsLedger'
import { ContinuationPanel, DefinitionInventory, QuerySemanticEditor } from './SemanticPanel'
import { OntologyPanel } from './OntologyPanel'
import { MigrationPanel } from './MigrationPanel'
import type { DataSourceName, KbDocument, Run } from '../../types'
import type { CaliberDraft, CaliberDraftSummary, DraftCitation } from '../../typesLedger'
import { DRAFT_POLL_LIMIT_MS, DRAFT_POLL_MS, FILTERS, newRequestId, stageOf, type Filter } from './drafts/helpers'
import {
  AlignmentBlock, DraftAttachSection, DraftChecksSection, DraftMetricItem, DraftQuestionList,
  DraftSqlSection, DraftStartSection, StatusPill,
} from './drafts/DraftSharedBlocks'
import { DraftEntryFieldsSection, DraftTrialSection, useDraftTrial } from './drafts/DraftTrialSection'
import { DraftRulesSection, useDraftRules } from './drafts/DraftRulesSection'
import { DraftImportsSection, useDraftImports } from './drafts/DraftImportsSection'
import { DraftNlSection, useDraftNlChange } from './drafts/DraftNlSection'
import { DraftValidationSection, useDraftValidations } from './drafts/DraftValidationSection'

const D = LEDGER_UI.drafts

export function CaliberDrafts() {
  const [drafts, setDrafts] = useState<CaliberDraftSummary[] | null>(null)
  const [listError, setListError] = useState<unknown>(null)
  const [filter, setFilter] = useState<Filter>('')

  const [openId, setOpenId] = useState<string | null>(null)
  const [detail, setDetail] = useState<CaliberDraft | null>(null)
  const [detailError, setDetailError] = useState<unknown>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const [docs, setDocs] = useState<KbDocument[] | null>(null)
  const [docsError, setDocsError] = useState<unknown>(null)
  const [sources, setSources] = useState<DataSourceName[]>([])
  const [pickedDocs, setPickedDocs] = useState<string[]>([])
  const [datasourceId, setDatasourceId] = useState('')
  const [guidance, setGuidance] = useState('')
  const [fixedFilters, setFixedFilters] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [pickedQuestions, setPickedQuestions] = useState<string[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [runId, setRunId] = useState('')
  const [recentRuns, setRecentRuns] = useState<Run[] | null>(null)
  const [attachOut, setAttachOut] = useState<{ workflow_id: string; version: number; warnings: string[] } | null>(null)

  const [pollingId, setPollingId] = useState<string | null>(null)
  const [pollTimedOut, setPollTimedOut] = useState(false)
  const [jobError, setJobError] = useState<unknown>(null)
  const [jobBusy, setJobBusy] = useState<string | null>(null)
  const [droppedExpanded, setDroppedExpanded] = useState(false)

  const [chunks, setChunks] = useState<Record<string, { ordinal: number; content: string }>>({})
  const asked = useRef(new Set<string>())
  const openSeq = useRef(0)
  const pendingWrite = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })

  const loadList = useCallback(() => {
    setListError(null)
    api.caliberLedger.drafts().then(
      (body) => setDrafts(body.drafts),
      (e) => { setDrafts(null); setListError(e) },
    )
  }, [])
  useEffect(loadList, [loadList])

  const refreshDetail = useCallback(async (id: string) => {
    const seq = openSeq.current
    try {
      const fresh = await api.caliberLedger.draft(id)
      if (seq === openSeq.current) setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
    } catch {
      // 读不到就先保持现在这一份：人下次动手时还会再取
    }
  }, [])

  const validationsState = useDraftValidations({ openSeq, refreshDetail })
  const { loadValidations, resetValidations } = validationsState

  const trialState = useDraftTrial({ detail, setDetail, openSeq, loadList, loadValidations })
  const { resetTrial, initEntryFields } = trialState

  const rulesState = useDraftRules({ setDetail, refreshDetail, loadValidations, loadList })
  const { resetRules, syncRulesFromBinding } = rulesState

  const importsState = useDraftImports({ openSeq, refreshDetail, loadValidations, loadList })
  const { resetImports, loadImports } = importsState

  const nlState = useDraftNlChange({ setDetail, syncRulesFromBinding, loadValidations, loadList })
  const { resetNl } = nlState

  useEffect(() => {
    api.kb.documents().then((rows) => { setDocs(rows); setDocsError(null) },
      (e) => { setDocs([]); setDocsError(e) })
    api.datasources.names().then(
      (all) => {
        const enabled = all.filter((s) => s.enabled !== false)
        setSources(enabled)
        setDatasourceId((cur) => cur || (enabled[0]?.id ?? ''))
      },
      () => setSources([]),
    )
  }, [])

  const ensureChunks = useCallback((documentId: string) => {
    if (!documentId || asked.current.has(documentId)) return
    asked.current.add(documentId)
    api.kb.document(documentId).then(({ chunks: rows }) => {
      setChunks((cur) => {
        const next = { ...cur }
        for (const row of rows) next[row.id] = { ordinal: row.ordinal, content: row.content }
        return next
      })
    }, () => { asked.current.delete(documentId) })
  }, [])

  const open = async (id: string) => {
    const seq = ++openSeq.current
    setOpenId(id)
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setPickedQuestions([])
    setAttachOut(null)
    setRunId('')
    setRecentRuns(null)
    setPollingId(null)
    setPollTimedOut(false)
    setJobError(null)
    setDroppedExpanded(false)
    resetTrial()
    resetRules()
    resetValidations()
    resetImports()
    resetNl()
    try {
      const draft = await api.caliberLedger.draft(id)
      if (seq !== openSeq.current) return
      setDetail(draft)
      initEntryFields(draft)
      syncRulesFromBinding(draft.binding)
      if (draft.status === 'drafting') { setPollTimedOut(false); setPollingId(id) }
    } catch (e) {
      if (seq !== openSeq.current) return
      setDetailError(e)
    } finally {
      if (seq === openSeq.current) setDetailLoading(false)
    }
    loadValidations(id)
    loadImports(id)
    api.runs.list({ status: 'succeeded', limit: 20 }).then(
      (runs) => { if (seq === openSeq.current) setRecentRuns(runs) },
      () => { if (seq === openSeq.current) setRecentRuns([]) },
    )
  }

  useEffect(() => {
    if (!pollingId) return
    let until = Date.now() + DRAFT_POLL_LIMIT_MS
    let lastProgress = ''
    let alive = true
    let settled = false
    const seq = openSeq.current
    let timer: ReturnType<typeof setTimeout>
    const poll = () => {
      api.caliberLedger.draft(pollingId).then((draft) => {
        if (!alive || seq !== openSeq.current) return
        const currentProgress = JSON.stringify([draft.progress, draft.implementation_progress])
        if (currentProgress !== lastProgress) {
          lastProgress = currentProgress
          until = Date.now() + DRAFT_POLL_LIMIT_MS
        }
        setDetail((cur) => (cur && cur.id === draft.id && (cur.revision ?? 0) <= (draft.revision ?? 0) ? draft : cur))
        if (draft.status !== 'drafting') {
          settled = true
          setPollingId(null)
          setPollTimedOut(false)
          loadList()
        } else if (Date.now() > until) {
          settled = true
          setPollingId(null)
          setPollTimedOut(true)
          loadList()
        }
      }, (e) => {
        if (!alive || seq !== openSeq.current) return
        settled = true
        setPollingId(null)
        setDetailError(e)
      }).finally(() => {
        if (alive && !settled && seq === openSeq.current) timer = setTimeout(poll, DRAFT_POLL_MS)
      })
    }
    timer = setTimeout(poll, DRAFT_POLL_MS)
    return () => { alive = false; clearTimeout(timer) }
  }, [pollingId, loadList])

  useEffect(() => {
    for (const doc of detail?.documents ?? []) ensureChunks(doc.document_id)
  }, [detail, ensureChunks])

  const shown = useMemo(
    () => (drafts ?? []).filter((d) => !filter || stageOf(d) === filter),
    [drafts, filter],
  )

  const chunkDoc = useMemo(() => {
    const map: Record<string, string> = {}
    for (const doc of detail?.documents ?? []) for (const chunk of doc.chunk_ids) map[chunk] = doc.document_id
    return map
  }, [detail])

  const paragraphOf = useCallback((citation: DraftCitation): number | null => {
    const ordinal = chunks[citation.chunk]?.ordinal
    if (typeof ordinal === 'number') return ordinal
    if (typeof citation.ordinal === 'number') return citation.ordinal
    const documentId = citation.document_id || chunkDoc[citation.chunk]
    const doc = detail?.documents?.find((d) => d.document_id === documentId)
    const at = doc?.chunk_ids.indexOf(citation.chunk) ?? -1
    return at >= 0 ? at + 1 : null
  }, [chunkDoc, chunks, detail?.documents])

  const submit = async (definitions = true) => {
    if (!pickedDocs.length) { toast.warn(D.needDoc); return }
    setSubmitting(true)
    try {
      const out = await (definitions ? api.caliberLedger.importDefinitions : api.caliberLedger.extractDraft)({
        documents: pickedDocs,
        ...(datasourceId ? { datasource_id: datasourceId } : {}),
        ...(guidance.trim() ? { guidance: guidance.trim() } : {}),
        ...(fixedFilters.trim() ? { fixed_filters: fixedFilters.trim() } : {}),
      })
      toast.ok(definitions ? '定义清单已导入，可自动处理全部可实现指标' : D.started)
      setPickedDocs([])
      setGuidance('')
      setFixedFilters('')
      setDatasourceId('')
      setFilter('')
      loadList()
      void open(out.draft_id)
    } catch (e) {
      toast.error(e)
    } finally {
      setSubmitting(false)
    }
  }

  const resolve = async (draft: CaliberDraft) => {
    if (!pickedQuestions.length) { toast.warn(D.resolveNeed); return }
    if (draft.revision != null && draft.workflow_id && !draft.card_id) {
      toast.warn(draft.cards && draft.cards.length > 1 ? D.cardAmbiguous : D.cardMissing)
      return
    }
    setBusy(`resolve:${draft.id}`)
    const questions = [...new Set(pickedQuestions.map((q) => q.trim()).filter(Boolean))].sort()
    const signature = `${draft.id}|${questions.join('\u0000')}`
    if (pendingWrite.current.signature !== signature) {
      pendingWrite.current = { signature, requestId: newRequestId() }
    }
    try {
      const fresh = await api.caliberLedger.patchDraft(draft.id, {
        resolved: questions,
        ...(draft.revision != null ? { expected_revision: draft.revision } : {}),
        ...(draft.card_id ? { target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id } } : {}),
        request_id: pendingWrite.current.requestId,
      })
      setDetail(fresh)
      toast.ok(D.resolved(questions.length))
      setPickedQuestions([])
      pendingWrite.current = { signature: '', requestId: '' }
      loadList()
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409 && e.code === 'stale_revision'
      if (stale) {
        const told = Number((e.body as { current_version?: unknown } | undefined)?.current_version)
        toast.warn(D.writeStale(Number.isFinite(told) ? told : null, draft.revision ?? null))
        void refreshDetail(draft.id)
      } else {
        toast.error(e)
      }
    } finally {
      setBusy(null)
    }
  }

  const attach = async (draft: CaliberDraft) => {
    const id = runId.trim()
    if (!id) { toast.warn(D.attachNeedRun); return }
    setBusy(`attach:${draft.id}`)
    try {
      const out = await api.caliberLedger.attachDraft(draft.id, id)
      setAttachOut(out)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.attached(out.version))
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const dismiss = async (draft: CaliberDraft) => {
    const note = await promptDialog({
      title: D.dismissTitle, label: D.dismissNote, placeholder: D.dismissPlaceholder, confirmLabel: D.dismissConfirm,
    })
    if (note == null) return
    setBusy(`dismiss:${draft.id}`)
    try {
      await api.caliberLedger.dismissDraft(draft.id, note.trim() || undefined)
      setDetail(await api.caliberLedger.draft(draft.id))
      toast.ok(D.dismissed)
      loadList()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const toggleQuestion = (question: string, checked: boolean) => {
    setPickedQuestions((cur) => (checked ? [...cur, question] : cur.filter((q) => q !== question)))
  }

  const changeDraftJob = async (draft: CaliberDraft, action: 'retry' | 'stop') => {
    const seq = openSeq.current
    setJobBusy(draft.id)
    setJobError(null)
    try {
      if (action === 'retry') await api.caliberLedger.retryDraft(draft.id)
      else await api.caliberLedger.stopDraft(draft.id)
      const fresh = await api.caliberLedger.draft(draft.id)
      if (seq === openSeq.current) {
        setDetail(fresh)
        if (fresh.status === 'drafting') { setPollTimedOut(false); setPollingId(draft.id) }
        else setPollingId(null)
      }
      await loadList()
    } catch (error) {
      if (seq === openSeq.current) setJobError(error)
    } finally {
      setJobBusy((id) => id === draft.id ? null : id)
    }
  }

  const renderDetail = (draft: CaliberDraft) => {
    const viewSeq = openSeq.current
    const stage = stageOf(draft)
    const drafting = draft.status === 'drafting'
    const terminal = stage === 'discarded'
    const metrics = draft.metrics ?? []
    const hasInventory = !!draft.semantic_inventory
    const awaitingImplementation = hasInventory && !draft.workflow_id && metrics.length === 0
    const stageNote = awaitingImplementation && draft.status !== 'path_pending' && !drafting
      ? D.stageNoteNeedsGraph
      : stage === 'review' ? D.stageNote(draft.status) : ''
    const rawQuestions = draft.open_questions ?? []
    const questions = hasInventory
      ? rawQuestions.filter((q) =>
        !q.includes('尚未生成可用实现，请补充字段映射或确认口径')
        && !(q.startsWith('另有 ') && q.includes('项指标尚未生成可用实现')))
      : rawQuestions
    const rawDropped = draft.dropped ?? []
    const hasSpecificDropped = rawDropped.some((item) => item.name !== 'metrics')
    const droppedMap = new Map<string, { name: string; reason: string; count: number }>()
    for (const item of rawDropped) {
      if (hasSpecificDropped && item.name === 'metrics' && item.reason === '没有一条指标通过校验') continue
      const key = `${item.name}\u0000${item.reason}`
      const existing = droppedMap.get(key)
      if (existing) existing.count += 1
      else droppedMap.set(key, { name: item.name, reason: item.reason, count: 1 })
    }
    const dropped = Array.from(droppedMap.values())
    const DROPPED_LIMIT = 8
    const visibleDropped = droppedExpanded || dropped.length <= DROPPED_LIMIT
      ? dropped
      : dropped.slice(0, DROPPED_LIMIT)
    const uncovered = draft.uncovered ?? []
    const values = new Map((draft.trial?.status === 'stale' ? [] : trialState.trial?.metrics ?? []).map((row) => [row.id, row]))
    const progress = draft.progress && typeof draft.progress.round === 'number' ? draft.progress : null
    return (
      <div className="space-y-3 border-t px-3 py-3" data-draft-detail="">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-faint">
          <StatusPill stage={stage} status={draft.status} />
          {stageNote && <span>{stageNote}</span>}
          {!!draft.documents?.length && <span>{D.documentsUsed(draft.documents.length)}</span>}
          {typeof draft.blocks === 'number' && draft.blocks > 1 && <span>{D.blocks(draft.blocks)}</span>}
          {draft.created_at && <span title={formatDateTime(draft.created_at)}>{formatTime(draft.created_at)}</span>}
          {draft.created_by && <span>{D.createdBy(draft.created_by)}</span>}
          {draft.model_id && <span title={draft.model_id}>{D.model(draft.model_id)}</span>}
          {draft.workflow_id && stage !== 'discarded' && (
            <Link to={`/studio/${draft.workflow_id}`} className="text-accent hover:underline" data-draft-workflow="">
              {D.openWorkflow}{draft.workflow_name ? `：${draft.workflow_name}` : ''}
              {draft.version != null ? ` · ${D.workflowVersion(draft.version)}` : ''}
            </Link>
          )}
        </div>

        {(drafting || draft.job?.retryable || (draft.status === 'failed' && !draft.workflow_id)) && (
          <div className="flex gap-2">
            {drafting && <button className="btn btn-sm" disabled={jobBusy === draft.id} onClick={() => void changeDraftJob(draft, 'stop')}>停止起草</button>}
            {!drafting && <button className="btn btn-sm" disabled={jobBusy === draft.id} onClick={() => void changeDraftJob(draft, 'retry')}>
              {draft.implementation_progress ? '按原配置重试中断任务' : '重试起草'}</button>}
          </div>
        )}
        {jobError != null && <ErrorState error={jobError} compact />}
        {draft.error && (
          <Notice tone="err">
            {drafting ? D.draftingFailed : D.loadFailed}：{draft.error}
          </Notice>
        )}

        {drafting && pollingId === draft.id && (
          <Notice tone="info">
            <span className="inline-flex items-center gap-1.5"><Spinner size={12} /> {D.drafting}</span>
            {progress && !draft.implementation_progress && (
              <span className="tnum mt-0.5 block" data-draft-progress="">
                {D.progress(progress.round, progress.covered, progress.uncovered)}
              </span>
            )}
            <span className="mt-0.5 block text-faint">{draft.implementation_progress
              ? '系统会自动处理后续指标并保存进度，离开页面不影响执行。需补充的信息会单独列出。' : D.draftingHint}</span>
          </Notice>
        )}
        {drafting && pollTimedOut && (
          <Notice tone="warn">
            {D.draftingTimeout}
            {progress && !draft.implementation_progress && (
              <span className="tnum mt-0.5 block" data-draft-progress="">
                {D.progress(progress.round, progress.covered, progress.uncovered)}
              </span>
            )}
          </Notice>
        )}
        {(draft.doc_truncated || (draft.truncated && !draft.catalog_truncated)) && (
          <Notice tone="warn">{D.docTruncated}</Notice>
        )}
        {draft.catalog_truncated && <Notice tone="warn">{D.catalogTruncated}</Notice>}

        <DefinitionInventory key={`definitions:${draft.id}`} draft={draft} sources={sources}
          onStarted={id => { loadList(); if (viewSeq === openSeq.current) void open(id) }} />
        <ContinuationPanel key={`continuation:${draft.id}`} draft={draft} sources={sources}
          onOpened={id => { loadList(); if (viewSeq === openSeq.current) void open(id) }} />
        <MigrationPanel key={`migration:${draft.id}:${draft.revision}`} draft={draft}
          onPrepared={id => { loadList(); if (viewSeq === openSeq.current) void open(id) }} />
        <DraftTrialSection draft={draft} trial={trialState.trial}
                           trialTimedOut={trialState.trialTimedOut} trialError={trialState.trialError} />
        <DraftRulesSection draft={draft}
                           ruleValues={rulesState.ruleValues}
                           setRuleValues={rulesState.setRuleValues}
                           ruleBusy={rulesState.ruleBusy}
                           ruleError={rulesState.ruleError}
                           ruleStale={rulesState.ruleStale}
                           ruleResult={rulesState.ruleResult}
                           ruleReplayed={rulesState.ruleReplayed}
                           undoRecord={rulesState.undoRecord}
                           onSave={(d, ops) => void rulesState.saveRules(d, ops)}
                           onUndo={(d, rec) => void rulesState.undoRules(d, rec)} />
        {!awaitingImplementation && (
          <DraftValidationSection draft={draft}
                                  validations={validationsState.validations}
                                  validationsError={validationsState.validationsError}
                                  reviewOut={validationsState.reviewOut}
                                  reviewNotes={validationsState.reviewNotes}
                                  setReviewNotes={validationsState.setReviewNotes}
                                  reviewBusy={validationsState.reviewBusy}
                                  reviewError={validationsState.reviewError}
                                  expectValues={validationsState.expectValues}
                                  setExpectValues={validationsState.setExpectValues}
                                  expectSource={validationsState.expectSource}
                                  setExpectSource={validationsState.setExpectSource}
                                  recordsShown={validationsState.recordsShown}
                                  setRecordsShown={validationsState.setRecordsShown}
                                  entryValues={trialState.entryValues}
                                  trialStarting={trialState.trialStarting}
                                  onReviewRecord={(d, recId, dec) => void validationsState.reviewRecord(d, recId, dec)}
                                  onRunTrial={(d, expect) => void trialState.runTrial(d, expect)} />
        )}
        <DraftImportsSection draft={draft}
                             validations={validationsState.validations}
                             importRuns={importsState.importRuns}
                             importsError={importsState.importsError}
                             importId={importsState.importId}
                             setImportId={importsState.setImportId}
                             importBusy={importsState.importBusy}
                             importError={importsState.importError}
                             importPicks={importsState.importPicks}
                             setImportPicks={importsState.setImportPicks}
                             alignPicks={importsState.alignPicks}
                             setAlignPicks={importsState.setAlignPicks}
                             importStale={importsState.importStale}
                             importResult={importsState.importResult}
                             importAccepted={importsState.importAccepted}
                             importRejected={importsState.importRejected}
                             importNote={importsState.importNote}
                             setImportNote={importsState.setImportNote}
                             onCreateImport={(d) => void importsState.createImport(d)}
                             onAlignImportItem={(run, item, metricId) => void importsState.alignImportItem(run, item, metricId)}
                             onAcceptImportFields={(d, run, items) => void importsState.acceptImportFields(d, run, items)}
                             onRejectImportFields={(run, items) => void importsState.rejectImportFields(run, items)}
                             onResumeImportRun={(run) => void importsState.resumeImportRun(run)} />
        <DraftNlSection draft={draft}
                        nlText={nlState.nlText}
                        setNlText={nlState.setNlText}
                        nlPreview={nlState.nlPreview}
                        nlPreviewOf={nlState.nlPreviewOf}
                        nlNotice={nlState.nlNotice}
                        nlBusy={nlState.nlBusy}
                        nlError={nlState.nlError}
                        nlResult={nlState.nlResult}
                        onPreviewNl={(d) => void nlState.previewNl(d)}
                        onConfirmNl={(d) => void nlState.confirmNl(d)} />
        {!awaitingImplementation && (
          <DraftEntryFieldsSection draft={draft}
                                   entryValues={trialState.entryValues}
                                   setEntryValues={trialState.setEntryValues}
                                   prefilled={trialState.prefilled}
                                   trialStarting={trialState.trialStarting}
                                   onRunTrial={(d) => void trialState.runTrial(d)} />
        )}
        {metrics.map(metric => <QuerySemanticEditor key={`${draft.id}:${metric.id}`} draft={draft}
          metric={metric} onSaved={fresh => {
            loadList()
            if (viewSeq !== openSeq.current) return
            setDetail(cur => cur?.id === fresh.id && (cur.revision ?? 0) <= (fresh.revision ?? 0) ? fresh : cur)
            loadValidations(draft.id)
          }} />)}
        <OntologyPanel key={`ontology:${draft.id}`} draft={draft} onSaved={fresh => {
          loadList()
          if (viewSeq !== openSeq.current) return
          setDetail(cur => cur?.id === fresh.id && (cur.revision ?? 0) <= (fresh.revision ?? 0) ? fresh : cur)
          loadValidations(draft.id)
        }} />
        <DraftSqlSection draft={draft} />

        <div>
          <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-2xs font-semibold text-dim">{D.metricsCount(metrics.length)}</h3>
            {pickedQuestions.length > 0 && (
              <div className="flex items-center gap-2">
                <span className="text-2xs text-faint">{D.resolveHint}</span>
                <button type="button" className="btn btn-xs" disabled={busy === `resolve:${draft.id}`}
                        onClick={() => void resolve(draft)} data-draft-resolve="">
                  {busy === `resolve:${draft.id}` ? <Spinner size={11} /> : null} {D.resolve}
                </button>
              </div>
            )}
          </div>
          {metrics.length ? (
            <ul className="space-y-2">
              {metrics.map((metric, index) => (
                <DraftMetricItem key={`${metric.id ?? metric.name}:${index}`}
                                 metric={metric}
                                 index={index}
                                 values={values}
                                 binding={detail?.binding}
                                 chunks={chunks}
                                 paragraphOf={paragraphOf}
                                 pickedQuestions={pickedQuestions}
                                 onToggleQuestion={toggleQuestion} />
              ))}
            </ul>
          ) : !drafting && draft.semantic_inventory ? (
            <p className="text-xs text-faint">
              {draft.status === 'path_pending' ? '定义已保留，请在上方选择指标并绑定数据。' : D.inventoryAwaitingHint}
            </p>
          ) : !drafting ? (
            <EmptyState offline={false} icon={<ScrollText size={20} />} title={D.noMetrics} body={D.noMetricsBody} />
          ) : (
            <Skeleton rows={3} height={16} />
          )}
        </div>

        {questions.length > 0 && (
          <div data-draft-questions-all="">
            <h3 className="mb-1 text-2xs font-semibold text-dim">{D.questionsOther}</h3>
            {hasInventory && (
              <p className="mb-1 text-2xs leading-relaxed text-faint">{D.questionsContinuationHint}</p>
            )}
            <DraftQuestionList questions={questions} prefix="top"
                               pickedQuestions={pickedQuestions} onToggle={toggleQuestion} />
          </div>
        )}

        {dropped.length > 0 && (
          <div data-draft-dropped="">
            <h3 className="text-2xs font-semibold text-dim">{D.dropped}</h3>
            <p className="mb-1 text-2xs leading-relaxed text-faint">{D.droppedHint}</p>
            <ul className="space-y-0.5 text-2xs leading-relaxed">
              {visibleDropped.map((item, i) => (
                <li key={`${item.name}:${i}`} className="text-dim">
                  <span className="font-medium">{item.name}</span>
                  <span className="text-faint">：{item.reason}{item.count > 1 ? `（重复 ${item.count} 次）` : ''}</span>
                </li>
              ))}
            </ul>
            {dropped.length > DROPPED_LIMIT && (
              <button type="button" className="btn btn-xs btn-ghost mt-1"
                onClick={() => setDroppedExpanded((cur) => !cur)}>
                {droppedExpanded ? D.droppedLess : D.droppedMore(dropped.length - DROPPED_LIMIT)}
              </button>
            )}
          </div>
        )}

        {!hasInventory && uncovered.length > 0 && (
          <div data-draft-uncovered="">
            <h3 className="text-2xs font-semibold text-dim">{D.uncovered(uncovered.length)}</h3>
            <p className="mb-1 text-2xs leading-relaxed text-faint">{D.uncoveredHint}</p>
            <p className="text-2xs leading-relaxed text-dim">{uncovered.join('、')}</p>
          </div>
        )}

        {draft.alignment && <AlignmentBlock alignment={draft.alignment} />}

        <DraftChecksSection draft={draft} />

        {!drafting && stage === 'review' && draft.workflow_id && (
          <div className="flex flex-wrap items-center gap-2" data-draft-publish="">
            <Link to={`/studio/${draft.workflow_id}`} className="btn btn-primary btn-sm">
              <PenLine size={12} aria-hidden /> {D.publish}
            </Link>
            <span className="text-2xs text-faint">{D.publishHint}</span>
          </div>
        )}

        <DraftAttachSection draft={draft}
                            runId={runId}
                            setRunId={setRunId}
                            recentRuns={recentRuns}
                            busy={busy}
                            attachOut={attachOut}
                            onAttach={(d) => void attach(d)}
                            showForm={!awaitingImplementation && !drafting && stage === 'review' && draft.status !== 'attached'} />

        {!terminal && (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-xs btn-ghost" disabled={!!busy}
                    onClick={() => void dismiss(draft)} data-draft-dismiss="">
              {busy === `dismiss:${draft.id}` ? <Spinner size={11} /> : null} {D.dismiss}
            </button>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col" data-caliber-drafts="">
      <PageHeader icon={<FileText size={14} />} title={D.title} subtitle={D.subtitle}
                  actions={<>
                    <Link to="/ledger/calibers" className="btn btn-xs btn-ghost">
                      <ChevronLeft size={12} aria-hidden /> {D.back}
                    </Link>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={loadList} title={D.refresh}>
                      <RefreshCw size={12} aria-hidden /> {D.refresh}
                    </button>
                  </>} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl space-y-3 p-4">
          <DraftStartSection docs={docs}
                             docsError={docsError}
                             sources={sources}
                             pickedDocs={pickedDocs}
                             setPickedDocs={setPickedDocs}
                             datasourceId={datasourceId}
                             setDatasourceId={setDatasourceId}
                             guidance={guidance}
                             setGuidance={setGuidance}
                             fixedFilters={fixedFilters}
                             setFixedFilters={setFixedFilters}
                             submitting={submitting}
                             onSubmit={(definitions) => void submit(definitions)} />

          <div className="flex flex-wrap items-center gap-1" role="group" aria-label={D.filterLabel}>
            {FILTERS.map((f) => (
              <button key={f || 'all'} type="button"
                      className={clsx('btn btn-xs', filter === f ? 'btn-primary' : 'btn-ghost')}
                      aria-pressed={filter === f} onClick={() => setFilter(f)} data-draft-filter={f || 'all'}>
                {f ? D.filter[f] : D.filter.all}
                {drafts && <span className="tnum ml-1 text-[10px] opacity-70">
                  {f ? drafts.filter((d) => stageOf(d) === f).length : drafts.length}
                </span>}
              </button>
            ))}
          </div>

          {listError ? <ErrorState error={listError} onRetry={loadList} compact />
            : !drafts ? <Skeleton rows={5} height={38} />
            : !shown.length ? (
              <EmptyState offline={false} icon={<FileText size={22} />} title={D.empty}
                          body={filter || drafts.length ? D.emptyFiltered : D.emptyBody} />
            ) : shown.map((draft) => {
              const expanded = openId === draft.id
              return (
                <section key={draft.id} className="overflow-hidden rounded-lg border bg-panel" data-draft={draft.id}>
                  <button type="button" className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-hover"
                          onClick={() => {
                            if (expanded) {
                              openSeq.current++
                              setOpenId(null)
                              setDetail(null)
                              setPollingId(null)
                              resetTrial()
                            } else {
                              void open(draft.id)
                            }
                          }}
                          aria-expanded={expanded} data-draft-toggle="">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span className="text-sm font-medium [overflow-wrap:anywhere]">{draft.title}</span>
                        <StatusPill stage={stageOf(draft)} status={draft.status} />
                        {draft.error && <span className="text-2xs text-[var(--err)]">{D.draftingFailed}</span>}
                      </div>
                      <div className="mt-0.5 text-2xs text-faint">
                        {draft.definition_count != null
                          ? `${draft.definition_count} 项定义 · ${draft.metric_count} 项已实现 · ${draft.question_count} 个待确认问题`
                          : D.summary(draft.metric_count, draft.question_count)}
                        {draft.created_at ? ` · ${formatTime(draft.created_at)}` : ''}
                      </div>
                    </div>
                  </button>
                  {expanded && (
                    detailLoading ? <div className="border-t px-3 py-3"><Skeleton rows={4} height={16} /></div>
                      : detailError ? <div className="border-t px-3 py-3"><ErrorState error={detailError} compact
                                                                    onRetry={() => void open(draft.id)} /></div>
                        : detail && detail.id === draft.id ? renderDetail(detail) : null
                  )}
                </section>
              )
            })}
        </div>
      </div>
    </div>
  )
}

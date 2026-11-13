import { useCallback, useRef, useState } from 'react'
import { Check, Sparkles } from 'lucide-react'
import { api } from '../../../api/client'
import { ErrorState, Spinner, toast } from '../../../components/ui'
import { LEDGER_UI } from '../../../lib/termsLedger'
import type { CaliberDraft, DraftBinding, NlChangeProposal } from '../../../typesLedger'
import { newRequestId, nlChanges, sameOperations } from './helpers'

const D = LEDGER_UI.drafts
const N = LEDGER_UI.nl

export interface NlAppliedResult {
  revision: number | null
  invalidated: boolean
  replayed: boolean
  mismatch: boolean | null
}

export function useDraftNlChange({
  setDetail, syncRulesFromBinding, loadValidations, loadList,
}: {
  setDetail: React.Dispatch<React.SetStateAction<CaliberDraft | null>>
  syncRulesFromBinding: (binding?: DraftBinding) => void
  loadValidations: (id: string) => void
  loadList: () => void
}) {
  const [nlText, setNlText] = useState('')
  const [nlPreview, setNlPreview] = useState<NlChangeProposal | null>(null)
  const [nlPreviewOf, setNlPreviewOf] = useState('')
  const [nlNotice, setNlNotice] = useState<'idle' | 'preview' | 'clarify' | 'applied'>('idle')
  const [nlBusy, setNlBusy] = useState<'preview' | 'confirm' | null>(null)
  const [nlError, setNlError] = useState<unknown>(null)
  const [nlResult, setNlResult] = useState<NlAppliedResult | null>(null)
  const nlRequest = useRef<{ signature: string; requestId: string }>({ signature: '', requestId: '' })

  const resetNl = useCallback(() => {
    setNlText('')
    setNlPreview(null)
    setNlPreviewOf('')
    setNlNotice('idle')
    setNlBusy(null)
    setNlError(null)
    setNlResult(null)
    nlRequest.current = { signature: '', requestId: '' }
  }, [])

  const previewNl = useCallback(async (draft: CaliberDraft) => {
    const instruction = nlText.trim()
    if (!instruction) { toast.warn(N.needText); return }
    if (!draft.card_id) { toast.warn(draft.cards && draft.cards.length > 1 ? D.cardAmbiguous : D.cardMissing); return }
    setNlBusy('preview')
    setNlError(null)
    setNlResult(null)
    setNlPreview(null)
    try {
      const out = await api.caliberLedger.changeProposal(draft.id, {
        instruction,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id },
      })
      setNlPreview(out.proposal)
      setNlPreviewOf(instruction)
      setNlNotice(out.proposal.ready ? 'preview' : 'clarify')
      const signature = `nl|${draft.id}|${instruction}`
      if (nlRequest.current.signature !== signature) {
        nlRequest.current = { signature, requestId: newRequestId() }
      }
    } catch (e) {
      setNlNotice('idle')
      setNlError(e)
    } finally {
      setNlBusy(null)
    }
  }, [nlText])

  const confirmNl = useCallback(async (draft: CaliberDraft) => {
    if (!nlPreviewOf || !nlPreview?.ready) { toast.warn(N.needConfirm); return }
    if (nlText.trim() !== nlPreviewOf) { toast.warn(N.needPreviewAgain); return }
    if (draft.revision == null) { toast.warn(N.noRevision); return }
    setNlBusy('confirm')
    setNlError(null)
    const signature = `nl|${draft.id}|${nlPreviewOf}`
    if (nlRequest.current.signature !== signature) {
      nlRequest.current = { signature, requestId: newRequestId() }
    }
    try {
      const out = await api.caliberLedger.nlChange(draft.id, {
        instruction: nlPreviewOf,
        confirm: true,
        expected_revision: draft.revision,
        request_id: nlRequest.current.requestId,
        target: { workflow_id: draft.workflow_id ?? undefined, card_id: draft.card_id ?? undefined },
      })
      const fresh = 'proposal' in out ? null : out
      const revision = fresh?.change?.revision ?? fresh?.revision ?? null
      const same = sameOperations(nlPreview.operations, fresh?.change?.operations)
      setNlResult({
        revision,
        invalidated: fresh?.change?.validation?.invalidated ?? false,
        replayed: !!fresh?.replayed,
        mismatch: same === null ? null : !same,
      })
      setNlNotice('applied')
      nlRequest.current = { signature: '', requestId: '' }
      toast.ok(N.applied(revision))
      if (fresh) {
        setDetail((cur) => (cur && cur.id === fresh.id ? fresh : cur))
        syncRulesFromBinding(fresh.binding)
      }
      loadValidations(draft.id)
      loadList()
    } catch (e) {
      setNlError(e)
    } finally {
      setNlBusy(null)
    }
  }, [loadList, loadValidations, nlPreview, nlPreviewOf, nlText, setDetail, syncRulesFromBinding])

  return {
    nlText, setNlText, nlPreview, nlPreviewOf, nlNotice, nlBusy, nlError, nlResult,
    resetNl, previewNl, confirmNl,
  }
}

export function DraftNlSection({
  draft, nlText, setNlText, nlPreview, nlPreviewOf, nlNotice, nlBusy, nlError, nlResult,
  onPreviewNl, onConfirmNl,
}: {
  draft: CaliberDraft
  nlText: string
  setNlText: (value: string) => void
  nlPreview: NlChangeProposal | null
  nlPreviewOf: string
  nlNotice: 'idle' | 'preview' | 'clarify' | 'applied'
  nlBusy: 'preview' | 'confirm' | null
  nlError: unknown
  nlResult: NlAppliedResult | null
  onPreviewNl: (draft: CaliberDraft) => void
  onConfirmNl: (draft: CaliberDraft) => void
}) {
  if (!draft.workflow_id) return null
  const proposal = nlPreview
  const changes = proposal?.ready ? nlChanges(draft, proposal.operations) : []
  const clarifications = proposal?.clarifications ?? []
  const touched = !!nlPreviewOf && nlText.trim() !== nlPreviewOf
  const confirmable = !touched && proposal?.ready && changes.length > 0 && draft.revision != null
  return (
    <div className="rounded-md border bg-elev px-3 py-2" data-draft-nl={nlNotice} data-draft-nl-touched={touched}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 className="text-2xs font-semibold text-dim">{N.title}</h3>
        <span className="text-[10px] text-faint">{N.hint}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-start gap-2">
        <textarea className="field min-h-[40px] flex-1 text-2xs" rows={2} data-draft-nl-input=""
                  placeholder={N.placeholder} value={nlText} disabled={nlBusy != null}
                  onChange={(e) => setNlText(e.target.value)} />
        <button type="button" className="btn btn-xs" data-draft-nl-preview="" disabled={nlBusy != null}
                onClick={() => onPreviewNl(draft)}>
          {nlBusy === 'preview' ? <Spinner size={11} /> : <Sparkles size={11} aria-hidden />}
          {nlBusy === 'preview' ? N.previewing : N.preview}
        </button>
      </div>

      {clarifications.length > 0 && (
        <div className="mt-1.5 rounded border bg-panel px-2 py-1.5" data-draft-nl-clarify={clarifications.length}>
          <p className="text-2xs font-medium text-[var(--warn)]">{N.clarifyTitle}</p>
          <ul className="mt-0.5 space-y-0.5 text-2xs leading-relaxed text-dim">
            {clarifications.map((item, index) => (
              <li key={`${item.kind}:${index}`} data-draft-nl-clarify-item={item.kind}>
                <span className="[overflow-wrap:anywhere]">{item.question}</span>
                {item.kind === 'no_match' && <span className="text-faint"> · {N.clarifyNoMatch}</span>}
                {!!item.options?.length && (
                  <span className="text-faint">
                    {' · '}{item.options.map((option) => option.label).join('、')}
                  </span>
                )}
              </li>
            ))}
          </ul>
          <p className="mt-0.5 text-2xs text-faint">{N.clarifyHint}</p>
        </div>
      )}

      {changes.length > 0 && (
        <div className="mt-1.5 rounded border bg-panel px-2 py-1.5" data-draft-nl-changes={changes.length}>
          <p className="tnum text-2xs font-medium text-dim" data-draft-nl-count={changes.length}>
            {N.changeCount(changes.length)}
          </p>
          <p className="text-[10px] text-faint" data-draft-nl-of={nlPreviewOf ? 'given' : 'none'}>
            {touched ? N.changedNotice : N.previewedAs(nlPreviewOf)}
          </p>
          <ul className="mt-0.5 space-y-0.5">
            {changes.map((change, index) => (
              <li key={`${change.metricId}:${change.label}:${index}`} className="text-2xs leading-relaxed text-dim"
                  data-draft-nl-change={change.metricId}>
                <span className="font-medium [overflow-wrap:anywhere]">{change.name}</span>
                <span className="text-faint"> · {change.label}：</span>
                <span className="text-faint">{N.now} {change.from}</span>
                <span className="text-faint"> → </span>
                <span>{N.after} {change.to}</span>
              </li>
            ))}
          </ul>
          {confirmable ? (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-xs btn-primary" data-draft-nl-confirm=""
                      disabled={nlBusy != null} onClick={() => onConfirmNl(draft)}>
                {nlBusy === 'confirm' ? <Spinner size={11} /> : <Check size={11} aria-hidden />}
                {nlBusy === 'confirm' ? N.confirming : N.confirm}
              </button>
              <span className="text-[10px] text-faint">{N.confirmHint}</span>
            </div>
          ) : touched ? (
            <p className="mt-1 text-2xs text-[var(--warn)]" data-draft-nl-repreview="">{N.needPreviewAgain}</p>
          ) : (
            <p className="mt-1 text-2xs text-faint" data-draft-nl-norevision="">{N.noRevision}</p>
          )}
        </div>
      )}

      {nlError != null && <div className="mt-1"><ErrorState error={nlError} compact /></div>}
      {nlResult && (
        <div className="mt-1.5 rounded border bg-panel px-2 py-1 text-2xs leading-relaxed"
             data-draft-nl-result={nlResult.revision == null ? 'applied' : `v${nlResult.revision}`}>
          <p className="text-dim" data-draft-nl-result-version={nlResult.revision ?? ''}>
            {N.applied(nlResult.revision)}
            {nlResult.replayed && <span className="text-faint"> · {N.replayed}</span>}
          </p>
          {nlResult.invalidated && <p className="text-faint">{N.invalidated}</p>}
          {nlResult.mismatch === true && (
            <p className="text-[var(--warn)]" data-draft-nl-mismatch="">{N.mismatch}</p>
          )}
        </div>
      )}
    </div>
  )
}

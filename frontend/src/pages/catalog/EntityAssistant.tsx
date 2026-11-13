import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Database, FileText, Paperclip, Send, Sparkles, Square, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { DataSourceName, KbDocument } from '../../types'
import type {
  EntityApplyPatchOut,
  EntityAssistantEvent,
  EntityAssistantMessage,
  EntityAttachmentOut,
  EntityPatchChange,
} from '../../typesLedger'
import { isComposing, Notice, Spinner, toast } from '../../components/ui'
import { humanizeError } from '../../lib/errors'
import { Markdown } from '../../run/Markdown'

const DEFAULT_ACCEPT = '.md,.markdown,.txt,.pdf,.docx,.pptx,.html,.htm,.xlsx,.csv,.tsv,.json,.yaml,.yml'

const errText = (err: unknown): string => {
  const h = humanizeError(err)
  return [h.title, h.reason].filter(Boolean).join('：')
}

type AssistantPart =
  | { kind: 'text'; text: string }
  | {
      kind: 'query'
      source: string
      sql: string
      columns: string[]
      rows: unknown[][]
      rowCount: number
      truncated: boolean
      error?: string | null
    }
  | {
      kind: 'proposal'
      id: string
      changes: EntityPatchChange[]
    }

interface UserTurn {
  id: string
  role: 'user'
  content: string
}

interface AssistantTurn {
  id: string
  role: 'assistant'
  parts: AssistantPart[]
  state: 'streaming' | 'done' | 'stopped' | 'error'
  error?: string
  queries?: number
}

type ChatTurn = UserTurn | AssistantTurn

export function EntityAssistant({
  currentSourceId,
  currentSourceName,
  sources,
  focusEntityKey,
  focusEntityLabel,
  onApplied,
  onClose,
}: {
  currentSourceId: string
  currentSourceName: string
  sources: DataSourceName[]
  focusEntityKey: string | null
  focusEntityLabel: string | null
  onApplied: (result: EntityApplyPatchOut) => void
  onClose: () => void
}) {
  const enabledSources = useMemo(() => sources.filter((s) => s.enabled !== false), [sources])
  const resolveDefaultScope = () => {
    if (currentSourceId && enabledSources.some((s) => s.id === currentSourceId)) {
      return [currentSourceId]
    }
    const byName = enabledSources.find((s) => s.name === currentSourceName)
    if (byName) return [byName.id]
    return enabledSources.length > 0 ? [enabledSources[0].id] : []
  }

  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>(() => resolveDefaultScope())
  const [scopeOpen, setScopeOpen] = useState(false)
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<EntityAttachmentOut[]>([])
  const [kbDocs, setKbDocs] = useState<KbDocument[]>([])
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([])
  const [collections, setCollections] = useState<string[]>(['default'])
  const [acceptExt, setAcceptExt] = useState(DEFAULT_ACCEPT)
  const [saveToKb, setSaveToKb] = useState(false)
  const [collection, setCollection] = useState('default')
  const [uploading, setUploading] = useState(false)
  const [docError, setDocError] = useState<string | null>(null)

  const [changeStates, setChangeStates] = useState<Record<string, 'pending' | 'dropped'>>({})
  const [allProposed, setAllProposed] = useState<EntityPatchChange[]>([])
  const [expectedVersions, setExpectedVersions] = useState<Record<string, number>>({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedNotice, setSavedNotice] = useState<string | null>(null)

  const abortRef = useRef<(() => void) | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const seqRef = useRef(0)

  const streaming = turns.some((t) => t.role === 'assistant' && t.state === 'streaming')

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const [docsRes, colsRes, fmtRes] = await Promise.allSettled([
        api.kb.documents(),
        api.kb.collections(),
        api.kb.formats(),
      ])
      if (cancelled) return
      if (docsRes.status === 'fulfilled') {
        setKbDocs(docsRes.value.filter((d) => d.status !== 'failed'))
      }
      if (colsRes.status === 'fulfilled' && colsRes.value.length > 0) {
        setCollections(Array.from(new Set(['default', ...colsRes.value.map((c) => c.collection).filter(Boolean)])))
      }
      if (fmtRes.status === 'fulfilled') {
        const exts = Array.from(new Set([...(fmtRes.value.extensions ?? []), ...(fmtRes.value.tabular ?? [])]))
        if (exts.length > 0) setAcceptExt(exts.join(','))
      }
    })()
    return () => {
      cancelled = true
      abortRef.current?.()
    }
  }, [])

  useEffect(() => {
    setSelectedSourceIds(resolveDefaultScope())
    setScopeOpen(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSourceId, currentSourceName])

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [turns])

  const selectedSourceSummary = useMemo(() => {
    if (selectedSourceIds.length === 0) return '未选数据源（仅文档）'
    const names = enabledSources
      .filter((s) => selectedSourceIds.includes(s.id))
      .map((s) => s.name)
    if (names.length === 0) return currentSourceName || '当前数据源'
    if (names.length <= 2) return names.join('、')
    return `${names.slice(0, 2).join('、')} 等 ${names.length} 个源`
  }, [enabledSources, selectedSourceIds, currentSourceName])

  const toggleSourceScope = (id: string) => {
    setSelectedSourceIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  // 按最后一次同 id 提案去重，筛出当前保留在待保存区的修改项
  const pendingItems = useMemo(() => {
    const map = new Map<string, EntityPatchChange>()
    for (const ch of allProposed) {
      map.set(ch.id, ch)
    }
    const items: EntityPatchChange[] = []
    for (const ch of map.values()) {
      if ((changeStates[ch.id] ?? 'pending') === 'pending') {
        items.push(ch)
      }
    }
    return items
  }, [allProposed, changeStates])

  const uploadFile = async (file: File | null) => {
    if (!file) return
    setUploading(true)
    setDocError(null)
    try {
      const res = await api.ontology.uploadAttachment(file, {
        saveToKb,
        collection,
      })
      setAttachments((prev) => [...prev, res])
      if (res.saved_to_kb && res.document_id) {
        toast.ok(`已挂载《${res.title}》并存入知识库`)
        const docs = await api.kb.documents().catch(() => null)
        if (docs) setKbDocs(docs.filter((d) => d.status !== 'failed'))
      } else {
        toast.ok(`已挂载《${res.title}》`)
      }
    } catch (err) {
      setDocError(errText(err))
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const buildHistory = (list: ChatTurn[]): EntityAssistantMessage[] => {
    const out: EntityAssistantMessage[] = []
    for (const t of list) {
      if (t.role === 'user') {
        out.push({ role: 'user', content: t.content })
      } else {
        const txt = t.parts
          .filter((p): p is { kind: 'text'; text: string } => p.kind === 'text')
          .map((p) => p.text)
          .join('')
          .trim()
        if (txt) out.push({ role: 'assistant', content: txt })
      }
    }
    return out
  }

  const sendMessage = (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    if (!text || streaming) return
    setInput('')
    setSaveError(null)
    setSavedNotice(null)

    const seq = ++seqRef.current
    const userTurn: UserTurn = { id: `u${seq}`, role: 'user', content: text }
    const replyId = `a${seq}`
    const assistantTurn: AssistantTurn = { id: replyId, role: 'assistant', parts: [], state: 'streaming' }
    const nextTurns = [...turns, userTurn, assistantTurn]
    setTurns(nextTurns)

    const updateReply = (fn: (cur: AssistantTurn) => AssistantTurn) => {
      setTurns((prev) => prev.map((t) => (t.id === replyId && t.role === 'assistant' ? fn(t) : t)))
    }

    const cancel = api.ontology.streamAssistant(
      {
        messages: buildHistory([...turns, userTurn]),
        source_ids: selectedSourceIds,
        focus_entity_key: focusEntityKey,
        attachment_ids: attachments.map((a) => a.attachment_id),
        document_ids: selectedKbIds,
        pending_ops: pendingItems.map((p) => p.op_payload),
      },
      (ev: EntityAssistantEvent) => {
        if (ev.type === 'text') {
          updateReply((cur) => {
            const parts = [...cur.parts]
            const last = parts[parts.length - 1]
            if (last && last.kind === 'text') {
              parts[parts.length - 1] = { kind: 'text', text: last.text + ev.delta }
            } else {
              parts.push({ kind: 'text', text: ev.delta })
            }
            return { ...cur, parts }
          })
        } else if (ev.type === 'query') {
          updateReply((cur) => ({
            ...cur,
            parts: [
              ...cur.parts,
              {
                kind: 'query',
                source: ev.source,
                sql: ev.sql,
                columns: ev.columns ?? [],
                rows: ev.rows ?? [],
                rowCount: ev.row_count ?? 0,
                truncated: Boolean(ev.truncated),
                error: ev.error,
              },
            ],
          }))
        } else if (ev.type === 'proposal') {
          const incoming = ev.changes ?? []
          if (incoming.length > 0) {
            setAllProposed((prev) => [...prev, ...incoming])
            setChangeStates((prev) => {
              const copy = { ...prev }
              for (const ch of incoming) copy[ch.id] = 'pending'
              return copy
            })
            if (ev.expected_versions) {
              setExpectedVersions((prev) => ({ ...prev, ...ev.expected_versions }))
            }
            updateReply((cur) => ({
              ...cur,
              parts: [
                ...cur.parts,
                {
                  kind: 'proposal',
                  id: `p_${Date.now()}_${incoming.length}`,
                  changes: incoming,
                },
              ],
            }))
          }
        } else if (ev.type === 'done') {
          updateReply((cur) => ({ ...cur, state: 'done', queries: ev.queries ?? 0 }))
        } else if (ev.type === 'error') {
          updateReply((cur) => ({ ...cur, state: 'error', error: ev.message }))
        }
      },
      (end) => {
        abortRef.current = null
        if (end.aborted) {
          updateReply((cur) => (cur.state === 'streaming' ? { ...cur, state: 'stopped' } : cur))
        } else if (end.error) {
          updateReply((cur) => ({ ...cur, state: 'error', error: end.error }))
        } else {
          updateReply((cur) => (cur.state === 'streaming' ? { ...cur, state: 'done' } : cur))
        }
      },
    )
    abortRef.current = cancel
  }

  const stopStream = () => {
    abortRef.current?.()
    abortRef.current = null
  }

  const toggleProposalItem = (id: string) => {
    setChangeStates((prev) => ({
      ...prev,
      [id]: (prev[id] ?? 'pending') === 'pending' ? 'dropped' : 'pending',
    }))
  }

  const clearPending = () => {
    setAllProposed([])
    setChangeStates({})
    setExpectedVersions({})
    setSaveError(null)
  }

  const savePending = async () => {
    if (pendingItems.length === 0 || saving) return
    setSaving(true)
    setSaveError(null)
    setSavedNotice(null)
    try {
      const touchedKeys = new Set(pendingItems.map((p) => p.entity_key))
      const versions: Record<string, number> = {}
      for (const k of touchedKeys) {
        if (k in expectedVersions) versions[k] = expectedVersions[k]
      }
      const res = await api.ontology.applyPatch({
        operations: pendingItems.map((p) => p.op_payload),
        expected_versions: Object.keys(versions).length > 0 ? versions : undefined,
      })
      clearPending()
      setSavedNotice(`已保存 ${res.applied_count} 项实体变更（新建 ${res.created_entities} 个，更新 ${res.updated_entities} 个）`)
      toast.ok('标准业务实体修改已生效')
      onApplied(res)
    } catch (err) {
      setSaveError(errText(err))
    } finally {
      setSaving(false)
    }
  }

  const availableKbToAdd = kbDocs.filter((d) => !selectedKbIds.includes(d.id))

  return (
    <aside
      className="flex h-full w-[380px] shrink-0 flex-col border-l bg-panel text-xs 2xl:w-[420px]"
      aria-label="实体助手"
      data-entity-assistant=""
    >
      {/* 顶栏 */}
      <div className="flex items-center justify-between border-b px-3 py-2.5">
        <div className="flex items-center gap-1.5">
          <Sparkles size={14} className="text-[var(--accent)]" aria-hidden />
          <span className="font-semibold text-fg">实体助手</span>
          {focusEntityLabel && (
            <span className="truncate rounded border px-1.5 py-0.5 text-2xs text-faint">
              当前：{focusEntityLabel}
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={() => {
            stopStream()
            onClose()
          }}
          aria-label="关闭实体助手"
          data-entity-assistant-close=""
        >
          <X size={13} aria-hidden />
        </button>
      </div>

      {/* 参与分析的数据源范围（默认仅当前数据源，需要跨源对齐时按需勾选） */}
      {enabledSources.length > 0 && (
        <div className="border-b bg-bg/40 px-3 py-2 text-2xs" data-entity-assistant-scope="">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5 text-dim">
              <Database size={11} className="shrink-0 text-faint" aria-hidden />
              <span className="shrink-0">数据范围：</span>
              <span className="truncate font-mono text-fg" data-entity-assistant-scope-summary="">
                {selectedSourceSummary}
              </span>
            </div>
            {enabledSources.length > 1 && (
              <button
                type="button"
                className="btn btn-xs btn-ghost shrink-0"
                disabled={streaming}
                onClick={() => setScopeOpen((v) => !v)}
                data-entity-assistant-scope-toggle=""
              >
                {scopeOpen ? '收起' : `跨源选择（${selectedSourceIds.length}/${enabledSources.length}）`}
              </button>
            )}
          </div>
          {scopeOpen && enabledSources.length > 1 && (
            <div className="mt-2 space-y-1.5 rounded border bg-panel p-2" data-entity-assistant-scope-panel="">
              <div className="flex items-center justify-between text-faint">
                <span>勾选本次要一起对齐的数据源</span>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost"
                    onClick={() => setSelectedSourceIds(resolveDefaultScope())}
                  >
                    仅当前源
                  </button>
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost"
                    onClick={() =>
                      setSelectedSourceIds(
                        selectedSourceIds.length === enabledSources.length ? [] : enabledSources.map((s) => s.id),
                      )
                    }
                  >
                    {selectedSourceIds.length === enabledSources.length ? '清空' : '全选'}
                  </button>
                </div>
              </div>
              <div className="grid max-h-28 gap-1 overflow-y-auto sm:grid-cols-2">
                {enabledSources.map((src) => (
                  <label key={src.id} className="flex cursor-pointer items-center gap-1.5 rounded px-1.5 py-0.5 hover:bg-elev">
                    <input
                      type="checkbox"
                      checked={selectedSourceIds.includes(src.id)}
                      onChange={() => toggleSourceScope(src.id)}
                      data-entity-assistant-source={src.id}
                    />
                    <span className="truncate font-mono text-fg">{src.name}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 业务文档与对照表挂载区 */}
      <div className="space-y-1.5 border-b bg-bg/50 px-3 py-2" data-entity-doc-bar="">
        <div className="flex flex-wrap items-center justify-between gap-1.5">
          <div className="flex items-center gap-1.5">
            <input
              ref={fileRef}
              type="file"
              accept={acceptExt}
              className="hidden"
              onChange={(e) => void uploadFile(e.target.files?.[0] ?? null)}
              data-entity-doc-input=""
            />
            <button
              type="button"
              className="btn btn-xs"
              disabled={uploading || streaming}
              onClick={() => fileRef.current?.click()}
              data-entity-doc-upload=""
            >
              {uploading ? <Spinner size={11} /> : <Paperclip size={11} aria-hidden />}
              {uploading ? '正在解析…' : '挂载文档/对照表'}
            </button>

            {availableKbToAdd.length > 0 && (
              <select
                className="field max-w-[155px] py-0.5 text-2xs"
                value=""
                onChange={(e) => {
                  const id = e.target.value
                  if (id && !selectedKbIds.includes(id)) setSelectedKbIds((prev) => [...prev, id])
                }}
                aria-label="关联已有知识库文档"
                data-entity-doc-kb-select=""
              >
                <option value="">关联知识库文档…</option>
                {availableKbToAdd.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.title}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-2xs text-dim">
          <label className="inline-flex cursor-pointer items-center gap-1">
            <input
              type="checkbox"
              checked={saveToKb}
              onChange={(e) => setSaveToKb(e.target.checked)}
              data-entity-doc-save-kb=""
            />
            <span>同时存入知识库供后续复用</span>
          </label>
          {saveToKb && (
            <select
              className="field py-0 text-2xs"
              value={collection}
              onChange={(e) => setCollection(e.target.value)}
              aria-label="选择知识库集合"
              data-entity-doc-collection=""
            >
              {collections.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          )}
        </div>

        {docError && <Notice tone="err">{docError}</Notice>}

        {(attachments.length > 0 || selectedKbIds.length > 0) && (
          <div className="flex flex-wrap gap-1 pt-0.5">
            {attachments.map((att) => (
              <span
                key={att.attachment_id}
                className="inline-flex max-w-full items-center gap-1 rounded border bg-panel px-1.5 py-0.5 text-2xs"
                data-entity-attached-doc={att.title}
              >
                <FileText size={10} className="shrink-0 text-faint" aria-hidden />
                <span className="truncate">{att.title}</span>
                {att.saved_to_kb && <span className="text-[var(--ok)]">· 已存知识库</span>}
                <button
                  type="button"
                  className="text-faint hover:text-fg"
                  onClick={() => setAttachments((prev) => prev.filter((x) => x.attachment_id !== att.attachment_id))}
                  aria-label={`移除 ${att.title}`}
                >
                  <X size={10} aria-hidden />
                </button>
              </span>
            ))}
            {selectedKbIds.map((docId) => {
              const doc = kbDocs.find((d) => d.id === docId)
              const title = doc?.title ?? docId
              return (
                <span
                  key={docId}
                  className="inline-flex max-w-full items-center gap-1 rounded border bg-panel px-1.5 py-0.5 text-2xs"
                  data-entity-attached-doc={title}
                >
                  <FileText size={10} className="shrink-0 text-faint" aria-hidden />
                  <span className="truncate">{title}</span>
                  <span className="text-faint">· 知识库</span>
                  <button
                    type="button"
                    className="text-faint hover:text-fg"
                    onClick={() => setSelectedKbIds((prev) => prev.filter((x) => x !== docId))}
                    aria-label={`移除 ${title}`}
                  >
                    <X size={10} aria-hidden />
                  </button>
                </span>
              )
            })}
          </div>
        )}
      </div>

      {/* 对话记录 */}
      <div
        ref={logRef}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3"
        aria-busy={streaming}
        data-entity-assistant-log=""
      >
        <div className="space-y-1.5 rounded-lg border bg-bg px-3 py-2.5 text-xs leading-relaxed text-dim">
          <p>
            告诉我需要添加或调整的业务实体，或者直接挂载业务文档、编码对照表；默认只分析当前数据源，如需跨库对齐可在上方点「跨源选择」加入目标库。
          </p>
          <div className="flex flex-wrap gap-1.5 pt-1">
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              disabled={streaming}
              onClick={() =>
                sendMessage(
                  selectedSourceIds.length > 1
                    ? '请盘点当前所选数据源之间值得优先对齐的标准业务实体并给出建议'
                    : '请盘点当前选定数据源中值得建立或完善的标准业务实体、表列绑定与口径参数，并给出建议',
                )
              }
              data-entity-assistant-quick=""
            >
              {selectedSourceIds.length > 1 ? '盘点所选源可对齐实体' : '盘点当前源业务实体'}
            </button>
          </div>
        </div>

        {turns.map((turn) =>
          turn.role === 'user' ? (
            <div key={turn.id} className="flex justify-end" data-entity-assistant-msg="user">
              <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-[var(--accent)]/15 px-3 py-2 text-fg">
                {turn.content}
              </div>
            </div>
          ) : (
            <div key={turn.id} className="space-y-2" data-entity-assistant-msg="assistant" data-turn-state={turn.state}>
              {turn.parts.map((part, idx) => {
                if (part.kind === 'text') {
                  return (
                    <div
                      key={idx}
                      className="min-w-0 break-words text-xs text-fg"
                      data-entity-assistant-text=""
                    >
                      <Markdown text={part.text} dense />
                    </div>
                  )
                }
                if (part.kind === 'query') {
                  return (
                    <details
                      key={idx}
                      className="rounded-lg border bg-bg text-2xs"
                      data-entity-assistant-query={part.error ? 'error' : 'ok'}
                    >
                      <summary className="cursor-pointer px-2.5 py-1.5 text-dim">
                        只读核查 · <span className="font-mono text-fg">{part.source}</span>
                        {part.error ? '（查询未成功）' : `（${part.rowCount} 行）`}
                      </summary>
                      <div className="space-y-1.5 border-t px-2.5 py-2">
                        <pre className="mono max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-panel px-2 py-1">
                          {part.sql}
                        </pre>
                        {part.error ? (
                          <p className="text-[var(--err)]">{part.error}</p>
                        ) : part.columns.length > 0 ? (
                          <div className="max-h-36 overflow-auto">
                            <table className="w-full border-collapse text-left">
                              <thead>
                                <tr className="border-b text-faint">
                                  {part.columns.map((c) => (
                                    <th key={c} className="px-1.5 py-1 font-medium">{c}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {part.rows.map((r, ri) => (
                                  <tr key={ri} className="border-b last:border-b-0">
                                    {r.map((cell, ci) => (
                                      <td key={ci} className="px-1.5 py-1 font-mono">{cell == null ? 'NULL' : String(cell)}</td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        ) : null}
                      </div>
                    </details>
                  )
                }
                return (
                  <section
                    key={part.id}
                    className="rounded-lg border bg-bg"
                    data-entity-assistant-proposal=""
                  >
                    <div className="border-b px-2.5 py-1.5 text-2xs font-medium text-fg">
                      实体修改提案 · {part.changes.length} 项
                    </div>
                    <ul className="divide-y">
                      {part.changes.map((c) => {
                        const state = changeStates[c.id] ?? 'pending'
                        const kept = state === 'pending'
                        return (
                          <li
                            key={c.id}
                            className={clsx('flex items-start justify-between gap-2 px-2.5 py-2 text-2xs', !kept && 'opacity-50')}
                            data-entity-proposal-item={c.id}
                            data-proposal-state={state}
                          >
                            <div className="min-w-0 space-y-0.5">
                              <div className="font-medium text-fg">{c.summary}</div>
                              {c.reason && <div className="text-faint">依据：{c.reason}</div>}
                            </div>
                            <button
                              type="button"
                              className="btn btn-xs shrink-0"
                              onClick={() => toggleProposalItem(c.id)}
                              data-entity-proposal-toggle=""
                            >
                              {kept ? '移出' : '保留'}
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  </section>
                )
              })}

              {turn.state === 'streaming' && (
                <div className="flex items-center gap-1.5 text-2xs text-faint">
                  <Spinner size={11} /> 正在思考与核对…
                </div>
              )}
              {turn.state === 'error' && turn.error && (
                <Notice tone="err" attr={{ 'data-entity-assistant-error': '' }}>
                  {turn.error}
                </Notice>
              )}
              {turn.state === 'stopped' && (
                <p className="text-2xs text-faint">已停止本轮回答</p>
              )}
            </div>
          ),
        )}
      </div>

      {/* 保存反馈 */}
      {saveError && (
        <div className="border-t px-3 py-2">
          <Notice tone="err" attr={{ 'data-entity-assistant-save-error': '' }}>{saveError}</Notice>
        </div>
      )}
      {savedNotice && (
        <div className="border-t px-3 py-2">
          <Notice tone="info" attr={{ 'data-entity-assistant-saved': '' }}>{savedNotice}</Notice>
        </div>
      )}

      {/* 待保存提案汇总条 */}
      {pendingItems.length > 0 && (
        <section className="space-y-2 border-t bg-elev px-3 py-2.5" data-entity-assistant-pending={pendingItems.length}>
          <div className="flex items-center justify-between gap-2 text-2xs">
            <span className="font-medium text-fg">待保存修改 · {pendingItems.length} 项</span>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                disabled={saving}
                onClick={clearPending}
                data-entity-assistant-clear=""
              >
                清空
              </button>
              <button
                type="button"
                className="btn btn-xs btn-primary"
                disabled={saving || streaming}
                onClick={() => void savePending()}
                data-entity-assistant-save=""
              >
                <Check size={11} aria-hidden />
                {saving ? '正在保存…' : '保存修改'}
              </button>
            </div>
          </div>
        </section>
      )}

      {/* 输入框 */}
      <div className="border-t p-2.5">
        <div className="flex items-end gap-1.5">
          <textarea
            className="field min-h-[58px] flex-1 resize-none text-xs"
            rows={2}
            maxLength={4000}
            placeholder="例如：根据刚上传的对照表，把 MES 和 ERP 的产线编码关联到生产产线实体…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !isComposing(e)) {
                e.preventDefault()
                sendMessage()
              }
            }}
            data-entity-assistant-input=""
          />
          {streaming ? (
            <button
              type="button"
              className="btn btn-sm"
              onClick={stopStream}
              title="停止回答"
              data-entity-assistant-stop=""
            >
              <Square size={12} aria-hidden /> 停止
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!input.trim()}
              onClick={() => sendMessage()}
              data-entity-assistant-send=""
            >
              <Send size={12} aria-hidden /> 发送
            </button>
          )}
        </div>
      </div>
    </aside>
  )
}

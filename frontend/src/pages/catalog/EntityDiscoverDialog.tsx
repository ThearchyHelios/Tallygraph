import { useEffect, useRef, useState } from 'react'
import { FileText, Sparkles, Upload, X } from 'lucide-react'
import { api } from '../../api/client'
import type { DataSourceName, KbDocument } from '../../types'
import type { EntityAttachmentOut, EntityDiscoveryJob } from '../../typesLedger'
import { ErrorState, Modal, Notice, Spinner, toast } from '../../components/ui'

const DEFAULT_ACCEPT = '.md,.markdown,.txt,.pdf,.docx,.pptx,.html,.htm,.xlsx,.csv,.tsv,.json,.yaml,.yml'

export function EntityDiscoverDialog({
  open,
  onClose,
  sources,
  defaultSourceId,
  onStarted,
}: {
  open: boolean
  onClose: () => void
  sources: DataSourceName[]
  defaultSourceId?: string
  onStarted: (job: EntityDiscoveryJob) => void
}) {
  const enabledSources = sources.filter((s) => s.enabled !== false)
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const [kbDocs, setKbDocs] = useState<KbDocument[]>([])
  const [collections, setCollections] = useState<string[]>(['default'])
  const [acceptExt, setAcceptExt] = useState(DEFAULT_ACCEPT)
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([])
  const [attachments, setAttachments] = useState<EntityAttachmentOut[]>([])
  const [saveToKb, setSaveToKb] = useState(false)
  const [collection, setCollection] = useState('default')
  const [uploading, setUploading] = useState(false)
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    const initialSources =
      defaultSourceId && enabledSources.some((s) => s.id === defaultSourceId)
        ? [defaultSourceId]
        : enabledSources.map((s) => s.id)
    setSelectedSources(initialSources)
    setSelectedDocIds([])
    setAttachments([])
    setNote('')
    setError(null)
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
        const names = Array.from(new Set(['default', ...colsRes.value.map((c) => c.collection).filter(Boolean)]))
        setCollections(names)
      }
      if (fmtRes.status === 'fulfilled') {
        const exts = Array.from(new Set([...(fmtRes.value.extensions ?? []), ...(fmtRes.value.tabular ?? [])]))
        if (exts.length > 0) setAcceptExt(exts.join(','))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, sources, defaultSourceId])

  const toggleSource = (id: string) => {
    setSelectedSources((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const toggleKbDoc = (id: string) => {
    setSelectedDocIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const handleFileChange = async (file: File | null) => {
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const uploaded = await api.ontology.uploadAttachment(file, {
        saveToKb,
        collection,
      })
      setAttachments((prev) => [...prev, uploaded])
      if (uploaded.saved_to_kb && uploaded.document_id) {
        toast.ok(`已解析《${uploaded.title}》并同步存入知识库`)
        const docs = await api.kb.documents().catch(() => null)
        if (docs) setKbDocs(docs.filter((d) => d.status !== 'failed'))
      } else {
        toast.ok(`已解析文档《${uploaded.title}》`)
      }
    } catch (err) {
      setError(err)
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const submit = async () => {
    if (selectedSources.length === 0 && selectedDocIds.length === 0 && attachments.length === 0) return
    setSubmitting(true)
    setError(null)
    try {
      const job = await api.ontology.createDiscoveryJob({
        source_ids: selectedSources,
        document_ids: selectedDocIds,
        attachment_ids: attachments.map((a) => a.attachment_id),
        note: note.trim() || undefined,
      })
      toast.ok('已启动候选业务实体扫描任务')
      onStarted(job)
      onClose()
    } catch (err) {
      setError(err)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="扫描发现候选业务实体"
      width={620}
      footer={(
        <div className="flex items-center justify-end gap-2">
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={
              submitting
              || uploading
              || (selectedSources.length === 0 && selectedDocIds.length === 0 && attachments.length === 0)
            }
            onClick={() => void submit()}
            data-discover-submit=""
          >
            <Sparkles size={13} aria-hidden />
            {submitting ? '正在提交…' : '开始扫描发现'}
          </button>
        </div>
      )}
    >
      <div className="space-y-4 text-xs" data-entity-discover-dialog="">
        <Notice tone="info">
          扫描结果中所有新增的表列绑定与口径参数映射均标记为「待确认」，不会直接影响线上问数穿透；扫描完成后可在实体列表逐项确认生效。
        </Notice>

        {error != null && <ErrorState error={error} compact />}

        {/* 1. 参与扫描的数据源 */}
        <section className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-medium text-fg">参与扫描的数据源（{selectedSources.length}/{enabledSources.length}）</span>
            {enabledSources.length > 0 && (
              <div className="flex items-center gap-1">
                {defaultSourceId && enabledSources.some((s) => s.id === defaultSourceId) && (
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost"
                    onClick={() => setSelectedSources([defaultSourceId])}
                  >
                    仅当前源
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-xs btn-ghost"
                  onClick={() =>
                    setSelectedSources(
                      selectedSources.length === enabledSources.length ? [] : enabledSources.map((s) => s.id),
                    )
                  }
                >
                  {selectedSources.length === enabledSources.length ? '清空选择' : '全选启用源'}
                </button>
              </div>
            )}
          </div>
          {enabledSources.length === 0 ? (
            <p className="text-2xs text-faint">暂无已启用的数据源，可上传业务文档或对照表提取实体定义。</p>
          ) : (
            <div className="grid max-h-32 gap-1.5 overflow-y-auto rounded border p-2 sm:grid-cols-2">
              {enabledSources.map((src) => (
                <label key={src.id} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-elev">
                  <input
                    type="checkbox"
                    checked={selectedSources.includes(src.id)}
                    onChange={() => toggleSource(src.id)}
                    data-discover-source={src.id}
                  />
                  <span className="truncate font-mono text-fg">{src.name}</span>
                  <span className="text-2xs text-faint">（{src.kind}）</span>
                </label>
              ))}
            </div>
          )}
        </section>

        {/* 2. 上传业务文档或编码对照表 */}
        <section className="space-y-2 rounded border p-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="font-medium text-fg">上传业务文档或主数据对照表（可选）</div>
              <p className="text-2xs text-faint">支持 Markdown、Word、PDF 及 Excel / CSV 跨系统编码对照表。</p>
            </div>
            <div>
              <input
                ref={fileInputRef}
                type="file"
                accept={acceptExt}
                className="hidden"
                onChange={(e) => void handleFileChange(e.target.files?.[0] ?? null)}
                data-discover-doc-input=""
              />
              <button
                type="button"
                className="btn btn-xs"
                disabled={uploading || submitting}
                onClick={() => fileInputRef.current?.click()}
                data-discover-doc-upload=""
              >
                {uploading ? <Spinner size={12} /> : <Upload size={12} aria-hidden />}
                {uploading ? '正在解析…' : '选择文件'}
              </button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 pt-0.5">
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-2xs text-dim">
              <input
                type="checkbox"
                checked={saveToKb}
                onChange={(e) => setSaveToKb(e.target.checked)}
                data-discover-doc-save-kb=""
              />
              <span>同时存入知识库供后续复用</span>
            </label>
            {saveToKb && (
              <label className="inline-flex items-center gap-1.5 text-2xs text-dim">
                <span>存入集合：</span>
                <select
                  className="field py-0.5 text-2xs"
                  value={collection}
                  onChange={(e) => setCollection(e.target.value)}
                  data-discover-doc-collection=""
                >
                  {collections.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {attachments.length > 0 && (
            <ul className="space-y-1 pt-1">
              {attachments.map((att) => (
                <li
                  key={att.attachment_id}
                  className="flex items-center justify-between gap-2 rounded border bg-bg px-2 py-1 text-2xs"
                  data-discover-attachment={att.attachment_id}
                >
                  <div className="flex min-w-0 items-center gap-1.5">
                    <FileText size={12} className="shrink-0 text-faint" aria-hidden />
                    <span className="truncate text-fg">{att.title}</span>
                    <span className="text-faint">（{att.chunk_count} 段）</span>
                    {att.saved_to_kb && (
                      <span className="rounded border border-[var(--ok)]/30 px-1 py-0.2 text-[var(--ok)]">
                        已存入知识库
                      </span>
                    )}
                  </div>
                  <button
                    type="button"
                    className="btn btn-xs btn-ghost text-faint"
                    onClick={() => setAttachments((prev) => prev.filter((x) => x.attachment_id !== att.attachment_id))}
                    aria-label={`移除附件 ${att.title}`}
                  >
                    <X size={11} aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* 3. 关联已有知识库文档 */}
        {kbDocs.length > 0 && (
          <section className="space-y-1.5">
            <div className="font-medium text-fg">关联已有知识库文档（已选 {selectedDocIds.length} 份）</div>
            <div className="max-h-28 space-y-1 overflow-y-auto rounded border p-2">
              {kbDocs.map((doc) => (
                <label key={doc.id} className="flex cursor-pointer items-center justify-between gap-2 rounded px-1.5 py-1 hover:bg-elev">
                  <span className="flex min-w-0 items-center gap-2">
                    <input
                      type="checkbox"
                      checked={selectedDocIds.includes(doc.id)}
                      onChange={() => toggleKbDoc(doc.id)}
                      data-discover-kb-doc={doc.id}
                    />
                    <span className="truncate text-fg">{doc.title}</span>
                  </span>
                  <span className="shrink-0 text-2xs text-faint">{doc.collection}</span>
                </label>
              ))}
            </div>
          </section>
        )}

        {/* 4. 扫描重点补充说明 */}
        <label className="block space-y-1">
          <span className="font-medium text-fg">扫描重点说明（可选）</span>
          <textarea
            className="field w-full"
            rows={2}
            maxLength={1000}
            placeholder="例如：重点对齐 MES 与 ERP 的产线、车间和不良原因编码"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            data-discover-note=""
          />
        </label>
      </div>
    </Modal>
  )
}

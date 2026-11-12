import type {
  CanonicalEntity,
  EntityApplyPatchOut,
  EntityAssistantEvent,
  EntityAssistantRequest,
  EntityAttachmentOut,
  EntityDiscoveryActiveOut,
  EntityDiscoveryJob,
  OntologyGraph,
  OntologyStatus,
  WorkflowOntology,
} from '../typesLedger'
import {
  actorHeader,
  BASE,
  del,
  failure,
  get,
  NETWORK_MESSAGE,
  parseBody,
  patch,
  post,
  qs,
  report,
  request,
  upload,
  type RequestOptions,
  type UploadOptions,
} from './http'

export interface EntityReviewPayload {
  target_kind: 'binding' | 'caliber_param'
  item_id: string
  status: OntologyStatus
  reason?: string
  version?: number
}

export interface EntityAssistantEnd {
  error?: string
  status?: number
  code?: string
  aborted?: boolean
}

export function streamEntityAssistant(
  body: EntityAssistantRequest,
  onEvent: (event: EntityAssistantEvent) => void,
  onEnd: (end: EntityAssistantEnd) => void,
): () => void {
  const controller = new AbortController()
  const path = '/ontology/entities/assistant'
  const emit = (line: string) => {
    const text = line.replace(/\r$/, '')
    if (!text.startsWith('data:')) return
    const payload = text.slice(5).trim()
    if (!payload) return
    try {
      const event = parseBody(payload) as Record<string, unknown> | null
      if (event && typeof event === 'object' && typeof event.type === 'string') {
        onEvent(event as unknown as EntityAssistantEvent)
      }
    } catch {
      /* 忽略非事件行 */
    }
  }
  void (async () => {
    try {
      const res = await fetch(BASE + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...actorHeader() },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        const err = failure(res.status, res.statusText, await res.text().catch(() => ''), path)
        onEnd(err.kind === 'network' ? { error: NETWORK_MESSAGE } : { error: err.message, status: res.status, code: err.code })
        return
      }
      report(true)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buffer.indexOf('\n')) >= 0) {
          emit(buffer.slice(0, idx))
          buffer = buffer.slice(idx + 1)
        }
      }
      buffer += decoder.decode()
      if (buffer.trim()) emit(buffer)
      onEnd({})
    } catch (err) {
      if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
        onEnd({ aborted: true })
        return
      }
      onEnd({ error: NETWORK_MESSAGE })
    }
  })()
  return () => controller.abort()
}

export const ontology = {
  listEntities: (opts?: RequestOptions & { source_name?: string; only_active?: boolean }) =>
    get<{ items: CanonicalEntity[] }>(
      `/ontology/entities${qs({
        source_name: opts?.source_name,
        only_active: opts?.only_active ? 'true' : undefined,
      })}`,
      opts,
    ),
  getEntity: (id: string, opts?: RequestOptions) =>
    get<CanonicalEntity>(`/ontology/entities/${encodeURIComponent(id)}`, opts),
  createEntity: (body: Partial<CanonicalEntity> & { key: string; label: string }) =>
    post<CanonicalEntity>('/ontology/entities', body),
  updateEntity: (id: string, body: Partial<CanonicalEntity> & { version?: number }) =>
    patch<CanonicalEntity>(`/ontology/entities/${encodeURIComponent(id)}`, body),
  reviewEntity: (id: string, body: EntityReviewPayload) =>
    post<CanonicalEntity>(`/ontology/entities/${encodeURIComponent(id)}/review`, body),
  deleteEntity: (id: string) =>
    del(`/ontology/entities/${encodeURIComponent(id)}`),
  uploadAttachment: (
    file: File,
    opts?: UploadOptions & { saveToKb?: boolean; collection?: string },
  ) => {
    const form = new FormData()
    form.append('file', file)
    form.append('save_to_kb', opts?.saveToKb ? 'true' : 'false')
    form.append('collection', opts?.collection || 'default')
    const path = '/ontology/entities/attachments'
    return opts?.onProgress
      ? upload<EntityAttachmentOut>(path, form, opts)
      : request<EntityAttachmentOut>(path, { method: 'POST', body: form, signal: opts?.signal })
  },
  applyPatch: (body: { operations: Record<string, unknown>[]; expected_versions?: Record<string, number> }) =>
    post<EntityApplyPatchOut>('/ontology/entities/apply-patch', body),
  streamAssistant: streamEntityAssistant,
  createDiscoveryJob: (body: {
    source_ids?: string[]
    document_ids?: string[]
    attachment_ids?: string[]
    note?: string
  }) => post<EntityDiscoveryJob>('/ontology/discovery-jobs', body),
  activeDiscoveryJob: (opts?: RequestOptions) =>
    get<EntityDiscoveryActiveOut>('/ontology/discovery-jobs/active', opts),
  getDiscoveryJob: (jobId: string, opts?: RequestOptions) =>
    get<EntityDiscoveryJob>(`/ontology/discovery-jobs/${encodeURIComponent(jobId)}`, opts),
  cancelDiscoveryJob: (jobId: string) =>
    post<EntityDiscoveryJob>(`/ontology/discovery-jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  graph: (opts?: RequestOptions & { include_proposed?: boolean }) =>
    get<OntologyGraph>(
      `/ontology/graph${qs({
        include_proposed: opts?.include_proposed === false ? 'false' : undefined,
      })}`,
      opts,
    ),
  workflow: (workflowId: string, opts?: RequestOptions & { version?: number }) =>
    get<WorkflowOntology>(
      `/ontology/workflows/${encodeURIComponent(workflowId)}${qs({ version: opts?.version })}`,
      opts,
    ),
}

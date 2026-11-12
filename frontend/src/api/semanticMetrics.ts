/**
 * 独立语义指标库（SemanticMetric）与后台文档一键拆解工作流 REST 客户端（服务端 backend/app/api/semantic_metrics.py）。
 *
 * 与工作流受管口径（caliberLedger）互补：
 * - 单表模式（expression）：绑定「数据源 + 物理表 + 单表聚合 SQL 表达式」；
 * - 多表 / 参数化模式（sql_template）：支持跨表 JOIN、WITH CTE 预聚合与 {{select_dims}}、{{and_where}}、{{period_days}} 等安全槽位；
 * - 后台一键拆解任务（decomposeJobs）：喂入业务文档后在后台分阶段拆解「字段目录补丁 + 单表/多表指标口径 + 标准业务实体 + 知识库归档」。
 */
import { del, get, post, put, qs, upload, type RequestOptions, type UploadOptions } from './http'

export type MetricCalcMode = 'expression' | 'sql_template'

export interface SemanticMetricDimension {
  key: string
  label: string
  column: string
  entity_key?: string
}

export interface SemanticMetric {
  id: string
  key: string
  name: string
  aliases: string[]
  description: string
  unit: string
  source_id: string
  source_name?: string
  calc_mode?: MetricCalcMode | string
  table_name: string
  sql_expression: string
  sql_template?: string
  aggregation_type: string
  date_column: string
  filter_sql: string
  dimensions: SemanticMetricDimension[]
  default_time_grain: string
  status: 'enabled' | 'disabled' | 'draft' | string
  source: 'human' | 'ai' | 'doc' | string
  version: number
  created_by?: string | null
  updated_by?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface SemanticMetricInput {
  key: string
  name: string
  aliases?: string[]
  description?: string
  unit?: string
  source_id: string
  calc_mode?: MetricCalcMode | string
  table_name?: string
  sql_expression?: string
  sql_template?: string
  date_column?: string
  filter_sql?: string
  dimensions?: SemanticMetricDimension[]
  default_time_grain?: string
  status?: string
  source?: string
  expected_version?: number
  exists_in_db?: boolean
}

export interface SemanticMetricPreviewIn {
  metric_id?: string
  definition?: Record<string, unknown>
  date_from?: string | null
  date_to?: string | null
  group_by_date?: boolean
  time_grain?: string | null
  group_by?: string[]
  filters?: Record<string, unknown>
  limit?: number
}

export interface SemanticMetricPreviewOut {
  sql: string
  source_name: string
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  entity_alignments: Record<string, unknown>[]
  latest_available_date?: string | null
}

export interface SemanticMetricSuggestOut {
  source_id: string
  source_name: string
  model_id?: string | null
  documents_used: string[]
  candidates: SemanticMetricInput[]
  skipped: { key: string; reason: string }[]
}

export interface DecomposeAttachmentOut {
  attachment_id: string
  title: string
  mime: string
  char_count: number
  chunk_count: number
  saved_to_kb: boolean
  document_id?: string | null
  collection?: string | null
}

export interface SemanticDecomposeStep {
  key: string
  label: string
  status: 'pending' | 'running' | 'succeeded' | 'skipped' | 'failed' | string
  detail: string
  count: number
  elapsed_ms: number
}

export interface SemanticCatalogChangeItem {
  id: string
  table: string
  path: string
  slot_label: string
  before: unknown
  after: unknown
  value: unknown
  reason: string
  state: string
}

export interface SemanticCatalogPatchTable {
  table: string
  if_version: number
  changes: SemanticCatalogChangeItem[]
}

export interface SemanticEntityChange {
  id: string
  op: string
  entity_key: string
  entity_label: string
  summary: string
  before: unknown
  after: unknown
  reason: string
  op_payload: Record<string, unknown>
}

export interface SemanticGuardrailNote {
  title: string
  rule: string
  category: string
}

export interface SemanticDecomposeAppliedRecord {
  applied_at: string
  applied_by?: string | null
  catalog_applied_count: number
  catalog_errors: string[]
  metrics_created_count: number
  metrics_updated_count: number
  metric_errors: { key: string; reason: string }[]
  entities_applied_count: number
  entity_errors: string[]
}

export interface SemanticDecomposeResult {
  model_id?: string | null
  kb_documents?: { id: string; title: string; created_now: boolean }[]
  guardrail_notes?: SemanticGuardrailNote[]
  catalog_patches?: SemanticCatalogPatchTable[]
  catalog_skipped?: string[]
  metric_candidates?: SemanticMetricInput[]
  metric_skipped?: { key: string; reason: string }[]
  entity_changes?: SemanticEntityChange[]
  entity_skipped?: string[]
  applied?: SemanticDecomposeAppliedRecord | null
}

export interface SemanticDecomposeJob {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string
  source_id: string
  source_name: string
  scopes: string[]
  document_ids: string[]
  attachment_ids: string[]
  doc_title: string
  has_doc_text: boolean
  save_to_kb: boolean
  note: string
  phase: string
  phase_label: string
  progress: number
  steps: SemanticDecomposeStep[]
  summary: Record<string, unknown>
  result?: SemanticDecomposeResult
  error?: string | null
  started_by?: string | null
  started_at?: string | null
  finished_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface SemanticDecomposeCreateIn {
  source_id: string
  scopes?: string[]
  table_names?: string[]
  document_ids?: string[]
  attachment_ids?: string[]
  doc_title?: string
  doc_text?: string
  save_to_kb?: boolean
  note?: string
}

export interface SemanticDecomposeApplyIn {
  catalog_patches?: { table: string; changes: { path: string; value: unknown; reason: string }[] }[]
  metrics?: SemanticMetricInput[]
  entity_operations?: Record<string, unknown>[]
  overwrite_existing_metrics?: boolean
}

export const semanticMetrics = {
  list: (params?: { source_id?: string; status?: string; q?: string }, opts?: RequestOptions) =>
    get<{ items: SemanticMetric[]; total: number }>(
      `/semantic-metrics${qs({
        source_id: params?.source_id,
        status: params?.status,
        q: params?.q,
      })}`,
      opts,
    ),
  get: (id: string, opts?: RequestOptions) =>
    get<{ metric: SemanticMetric }>(`/semantic-metrics/${encodeURIComponent(id)}`, opts),
  create: (body: SemanticMetricInput) =>
    post<{ metric: SemanticMetric }>('/semantic-metrics', body),
  batchCreate: (items: SemanticMetricInput[]) =>
    post<{ created: SemanticMetric[]; skipped: { key: string; reason: string }[] }>('/semantic-metrics/batch', { items }),
  update: (id: string, body: Partial<SemanticMetricInput>) =>
    put<{ metric: SemanticMetric }>(`/semantic-metrics/${encodeURIComponent(id)}`, body),
  remove: (id: string) =>
    del(`/semantic-metrics/${encodeURIComponent(id)}`),
  preview: (body: SemanticMetricPreviewIn) =>
    post<SemanticMetricPreviewOut>('/semantic-metrics/preview', body),
  suggest: (body: { source_id: string; table_names?: string[]; document_ids?: string[]; note?: string }) =>
    post<SemanticMetricSuggestOut>('/semantic-metrics/suggest', body),
  uploadAttachment: (file: File, opts?: { save_to_kb?: boolean; collection?: string } & UploadOptions) => {
    const form = new FormData()
    form.append('file', file)
    if (opts?.save_to_kb !== undefined) form.append('save_to_kb', String(opts.save_to_kb))
    if (opts?.collection) form.append('collection', opts.collection)
    return upload<DecomposeAttachmentOut>('/semantic-metrics/attachments', form, opts)
  },
  decomposeJobs: {
    active: (sourceId?: string, opts?: RequestOptions) =>
      get<{ active: SemanticDecomposeJob | null; latest: SemanticDecomposeJob | null }>(
        `/semantic-metrics/decompose-jobs/active${qs({ source_id: sourceId })}`,
        opts,
      ),
    list: (sourceId?: string, limit = 10, opts?: RequestOptions) =>
      get<{ items: SemanticDecomposeJob[] }>(
        `/semantic-metrics/decompose-jobs${qs({ source_id: sourceId, limit })}`,
        opts,
      ),
    create: (body: SemanticDecomposeCreateIn) =>
      post<{ job: SemanticDecomposeJob }>('/semantic-metrics/decompose-jobs', body),
    get: (jobId: string, opts?: RequestOptions) =>
      get<{ job: SemanticDecomposeJob }>(`/semantic-metrics/decompose-jobs/${encodeURIComponent(jobId)}`, opts),
    cancel: (jobId: string) =>
      post<{ job: SemanticDecomposeJob }>(`/semantic-metrics/decompose-jobs/${encodeURIComponent(jobId)}/cancel`, {}),
    apply: (jobId: string, body: SemanticDecomposeApplyIn) =>
      post<{
        job_id: string
        applied: SemanticDecomposeAppliedRecord
        created_metrics: SemanticMetric[]
        updated_metrics: SemanticMetric[]
      }>(`/semantic-metrics/decompose-jobs/${encodeURIComponent(jobId)}/apply`, body),
  },
}

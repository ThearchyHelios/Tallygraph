/**
 * 口径账本的接口（docs/design/next-round/02-口径账本.md），挂在 api.caliberLedger 上。形状见 typesLedger.ts。
 *
 * - run：GET /api/runs/{id}/ledger，一次运行里每个数的口径标签、整题判断、能做的动作、出具过的正式运行、对过的账；
 * - issue：按口径正式出具（参数只由服务端从比对工件里取，这里只能选「按哪份口径」）；
 * - reconcile：口径对账（点开才跑一次确定性派生的只读图）；feedback：对账结果上的「说明有用 / 看不懂」（记成对账运行上的
 *   封存后事件，统计 E2 数它）；
 * - certifyQueue / dismiss：可沉淀的问题、「不是口径」（搭建者及以上）；
 * - freeze：固化成口径草稿（/copilot/from-run 的 freeze 选项，报告引用的查询变成固定 SQL 的取数节点）。
 *
 * 应用平台的「账本」（版本被哪些应用固定、生效记录）在 api.ledger（api/apps.ts），和这里不是一回事。
 * 请求函数从 ./http 导入，不从 ./client 导入（client.ts 要导入这个文件，反过来就成了循环）。
 */
import type {
  CaliberDescriptor, CaliberDraft, CaliberDraftAttachOut, CaliberDraftSummary, CaliberTrialOut, CertifyQueue, DraftExpectIn,
  DraftImportAcceptOut, DraftImportRun, DraftMetric, DraftOperation, DraftTrialOut, DraftValidationsOut, FreezeOut,
  LedgerFeedbackOut, LedgerFeedbackVerdict, LedgerIssueOut, LedgerReconcileIn, LedgerReconcileOut, NlChangeOut,
  NlChangePreviewOut, RunLedger, DraftValidationRecord,
} from '../typesLedger'
import { get, patch, post, type RequestOptions } from './http'

const at = (runId: string) => `/ledger/answers/${encodeURIComponent(runId)}`
const draftAt = (draftId: string) => `/ledger/drafts/${encodeURIComponent(draftId)}`

export const caliberLedger = {
  run: (runId: string, opts?: RequestOptions) => get<RunLedger>(`/runs/${encodeURIComponent(runId)}/ledger`, opts),
  issue: (runId: string, body: { workflow_id: string; version: number }) => post<LedgerIssueOut>(`${at(runId)}/issue`, body),
  reconcile: (runId: string, body: LedgerReconcileIn) => post<LedgerReconcileOut>(`${at(runId)}/reconcile`, body),
  feedback: (runId: string, reconcileId: string, verdict: LedgerFeedbackVerdict) =>
    post<LedgerFeedbackOut>(`${at(runId)}/reconcile/${encodeURIComponent(reconcileId)}/feedback`, { verdict }),
  certifyQueue: (opts?: RequestOptions) => get<CertifyQueue>('/ledger/certify-queue', opts),
  dismiss: (body: { item_id: string; kind?: string; note: string }) =>
    post<{ ok: boolean; item_id: string }>('/ledger/certify-queue/dismiss', body),
  /**
   * 固化成口径草稿：代表运行里报告引用了的查询变成固定 SQL 的取数节点，peers（同一个 SQL 形状的别的运行）用来认哪些字面量
   * 跨轮变了。落成新的草稿工作流，人在画布上审改、按受管级别发布
   */
  freeze: (runId: string, opts: { peers?: string[]; conversation_id?: string | null; turn_id?: string | null } = {}) =>
    post<FreezeOut>('/copilot/from-run', {
      run_id: runId, freeze: true, peers: opts.peers ?? [],
      ...(opts.conversation_id ? { conversation_id: opts.conversation_id } : {}),
      ...(opts.turn_id ? { turn_id: opts.turn_id } : {}),
    }),
  /** 口径目录：每份已发布口径卡的入口 Schema、依赖、能否组合/出具（不返回 SQL、参数值） */
  catalog: (opts?: RequestOptions) => get<{ calibers: CaliberDescriptor[] }>('/ledger/calibers', opts),
  /** 按固定版本试跑：只读探索运行，返回指标值和 run_id；不是正式出具 */
  trial: (workflowId: string, version: number, nodeId: string, inputs: Record<string, unknown>) =>
    post<CaliberTrialOut>(
      `/ledger/calibers/${encodeURIComponent(workflowId)}/${version}/${encodeURIComponent(nodeId)}/trial`,
      { inputs },
    ),
  /**
   * 从业务文档起草口径（app/ledger/docdraft.py，搭建者）：发起后立刻返回 drafting，模型在后台读文档、对照数据目录
   * 写定义和取数，落成草稿图并自动试运行；结果用 draft() 反复取，直到状态离开 drafting
   */
  drafts: (status?: string, opts?: RequestOptions) =>
    get<{ drafts: CaliberDraftSummary[] }>(`/ledger/drafts${status ? `?status=${encodeURIComponent(status)}` : ''}`, opts),
  /** 一份草稿的完整内容：payload 摊平在上层（metrics / entry_fields / dropped / open_questions / alignment / trial） */
  draft: (draftId: string, opts?: RequestOptions) => get<CaliberDraft>(draftAt(draftId), opts),
  /** 起草后自动发起的那次只读试运行：状态和每个指标的值，running 时轮询到终态 */
  retryDraft: (draftId: string) => post<{ draft_id: string; status: string }>(`${draftAt(draftId)}/retry`, {}),
  stopDraft: (draftId: string) => post<CaliberDraft>(`${draftAt(draftId)}/stop`, {}),
  draftTrial: (draftId: string, opts?: RequestOptions) => get<DraftTrialOut>(`${draftAt(draftId)}/trial`, opts),
  /**
   * 按填好的入口字段再试运行一次（POST 同一个路径）：参数按入口字段校验，缺必填回 422；
   * 返回 {run_id, status, metrics, error}，status 是 running / succeeded / failed / needs_input。
   *
   * 带 expect（K05 的固定期望）时，期望里的指标必须是这张卡上的、要**逐条给全**（不合规回 422，不发起运行）；
   * 跑完把结果追加成一条验证记录。不带 expect 也能跑，但那样只记 observed——没有独立期望就不能算验证通过。
   */
  runDraftTrial: (draftId: string, inputs: Record<string, unknown>, expect?: DraftExpectIn) =>
    post<DraftTrialOut>(`${draftAt(draftId)}/trial`, expect ? { inputs, expect } : { inputs }),
  /** 这份草稿的验证记录（新的在前）+ 总情况 + 字段基线状态：K06 读的那份契约 */
  draftValidations: (draftId: string, opts?: RequestOptions) =>
    get<DraftValidationsOut>(`${draftAt(draftId)}/validations`, opts),
  /**
   * 对一条验证记录做一次审阅（builder 及以上）：confirmed / rejected，只追加。请求体里**没有 actor**，
   * 审批人取服务端身份（关登录时是自报署名）；没有能记下来的主体时回 409 review_actor_required。
   * 返回里的 record 带这次决定和 covers_current（所审记录还算不算覆盖当前定义/绑定）
   */
  reviewValidation: (draftId: string, recordId: string, body: { decision: 'confirmed' | 'rejected'; note?: string;
    metrics?: string[] }) =>
    post<{ ok: boolean; draft_id: string; record: DraftValidationRecord }>(
      `${draftAt(draftId)}/validations/${encodeURIComponent(recordId)}/review`, body),
  /** 选文档发起起草：documents 是要用的知识库文档 id，guidance 是给模型看的附加要求，
   *  fixed_filters 是每次跑都不变的条件（工厂、产线、属性组），服务端要求写死在 SQL 里、不做成入口字段 */
  extractDraft: (body: { documents: string[]; collection?: string; datasource_id?: string; guidance?: string;
    fixed_filters?: string }) =>
    post<{ draft_id: string; status: string }>('/ledger/drafts/extract', body),
  importDefinitions: (body: { documents: string[]; datasource_id?: string; guidance?: string; fixed_filters?: string }) =>
    post<{ draft_id: string; status: string }>('/ledger/drafts/definitions', body),
  implementDefinitions: (draftId: string, body: { selected?: string[]; datasource_id: string }) =>
    post<{ draft_id: string; status: string }>(`${draftAt(draftId)}/implement`, body),
  continueDefinitions: (draftId: string, body: {
    expected_revision: number; guidance: string; selected?: string[]; datasource_id?: string
  }) =>
    post<{ draft_id: string; status: string; original_draft_id: string; merge_required: boolean }>(`${draftAt(draftId)}/continue`, body),
  mergeContinuation: (draftId: string, body: { expected_revision: number; candidate_revision: number }) =>
    post<CaliberDraft & { merged_candidate_id: string }>(`${draftAt(draftId)}/merge`, body),
  migration: (draftId: string) => get<{
    revision: number; metrics_total: number; metrics_needing_work: number; can_rebuild: boolean; note: string
    metrics: { card_id: string; metric_id: string; name: string; query_count: number; guidance: string
      findings: { code: string; message: string }[] }[]
  }>(`${draftAt(draftId)}/migration`),
  prepareMigration: (draftId: string, expected_revision: number) =>
    post<{ draft_id: string; status: string }>(`${draftAt(draftId)}/migration`, { expected_revision }),
  /**
   * 改标题、整体换掉指标、或把答清的问题消掉（resolved 是问题原文）。服务端据此重算状态：还有问题 → questions_open，
   * 否则 ready；已经接上或驳回的不改状态。
   *
   * 已接管的草稿（有取数图）是 K02 的并发保护对象：expected_revision 是读草稿时给的 revision（必带，缺了 409
   * revision_required），target.card_id 在多张可写的卡时必须给（否则 409 card_required），request_id 是幂等键——
   * 同一个键带着同样的内容重发不会多存一版。基准版本过期回 409 stale_revision（另带 current_version），
   * 这时保留页面上的编辑、刷新详情后再改。
   *
   * 走 operations 时回执里另带 change 一段（变更分类、影响指标、定义指纹前后值、失效说明、绑定状态）；同键同内容的
   * 重放交回的是当前视图（带 replayed），不再重算一份回执
   */
  patchDraft: (draftId: string, body: {
    title?: string; metrics?: DraftMetric[]; resolved?: string[]
    /** K03 的字段操作：按稳定 metric_id + 字段路径改一处；和 metrics / resolved 互斥（422 change_conflict） */
    operations?: DraftOperation[]
    expected_revision?: number; request_id?: string
    target?: { workflow_id?: string; card_id?: string }
  }) => patch<CaliberDraft>(draftAt(draftId), body),
  /** 接上一次真实运行补取数路径（app/ledger/docattach.py）：由它建工作流和版本，定义来自草稿 */
  attachDraft: (draftId: string, runId: string) => post<CaliberDraftAttachOut>(`${draftAt(draftId)}/attach`, { run_id: runId }),
  /** 「不是口径」：置 rejected，写一条审计，以后不再出现在列表的待办里 */
  dismissDraft: (draftId: string, note?: string) =>
    post<{ ok: boolean; draft_id: string; status: string }>(`${draftAt(draftId)}/dismiss`, { note: note ?? null }),

  // ---- K11 增量重导入（app/ledger/reimport.py、api/caliber_import.py；搭建者）----
  /** 这份草稿的导入记录（新的在前），每条都带工作项和逐字段三方 */
  imports: (draftId: string, opts?: RequestOptions) =>
    get<{ runs: DraftImportRun[] }>(`${draftAt(draftId)}/imports`, opts),
  /**
   * 按这份草稿的来源文档重新抽一次候选 R（后台跑）。**不改 L、不改 B**：真正落到图上的只有 accept。
   * 带 request_id 的：同一份草稿 + 同一个键已经建过就交回那一次，不重复抽、不重复建工作项
   */
  createImport: (draftId: string, requestId?: string) =>
    post<DraftImportRun>(`${draftAt(draftId)}/imports`, requestId ? { request_id: requestId } : {}),
  /** 一次导入的现状：工作项、逐字段三方、冲突；queued / running 时轮询它 */
  importRun: (runId: string, opts?: RequestOptions) =>
    get<DraftImportRun>(`/ledger/imports/${encodeURIComponent(runId)}`, opts),
  /**
   * 按**现在的** L / B 重算这次导入还没决定的三方，并刷新每项的比较锚点（人处置过的决定不动）。
   * 这是 409 import_stale_l 之后的恢复动作：候选算出来之后 L 被人改过，不能只把 expected_revision 更新到最新一版
   * 就覆盖上去，要按现在的口径重新算、人重新看过再接受
   */
  refreshImport: (runId: string) =>
    post<DraftImportRun>(`/ledger/imports/${encodeURIComponent(runId)}/refresh`, {}),
  /** 人工把一条工作项对到图上某条指标（空串 = 取消对齐）：baseline_unknown 和同名不同范围都走这里 */
  alignImportItem: (runId: string, itemId: string, metricId: string) =>
    post<DraftImportRun>(`/ledger/imports/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}/align`,
      { metric_id: metricId }),
  /**
   * 接受选定字段：**一次性**落到 L（新增一版图）并按被接受的那几格推进 B。要带读到的基准版本
   * （expected_revision，缺了 409 revision_required、对不上 409 stale_revision）和幂等键。
   * items 一律按**工作项身份**（item_id）指认：同一条指标上可能有多条工作项（重复的候选、同一位被两项候选各带一份
   * 说法），按 metric_id 指会指错（服务端只在旧调用只给 metric_id 时才要求它在这条导入里唯一对得上一条）；
   * fields 要**明确列出来**（页面列了几格就只接受几格，不靠 auto 猜）。服务端发现要接受的格子「当前内容和算候选时的
   * 那一版不一样」时回 409 import_stale_l（另带 fields 指出是哪几格），这时不能只更新 expected_revision 覆盖上去，
   * 要先 refreshImport 按现在的口径重算三方再让人确认
   */
  acceptImport: (runId: string, body: {
    items: { item_id: string; metric_id?: string; fields: string[] }[]
    expected_revision?: number; request_id?: string
  }) => post<DraftImportAcceptOut>(`/ledger/imports/${encodeURIComponent(runId)}/accept`, body),
  /** 拒绝选定字段：保持 L 不动，也不推进 B。同样按工作项身份（item_id）指认；老调用可以用 seq */
  rejectImport: (runId: string, body: {
    items: { item_id: string; metric_id?: string; seq?: number; fields: string[] }[]; note?: string
  }) => post<DraftImportRun>(`/ledger/imports/${encodeURIComponent(runId)}/reject`, body),
  /** 显式恢复：把这次导入重新排进后台，续做还没完成的工作项（已接受 / 拒绝的不动，不重复建版） */
  resumeImport: (runId: string) =>
    post<DraftImportRun>(`/ledger/imports/${encodeURIComponent(runId)}/resume`, {}),

  // ---- K12 受限的自然语言修改（app/ledger/nlchange.py；搭建者）----
  /** 一句话 → 受限修改提案：**只读**，不落库、不新增版本；说不清就只回澄清项 */
  changeProposal: (draftId: string, body: { instruction: string; metrics?: string[]
    target?: { workflow_id?: string; card_id?: string } }) =>
    post<NlChangePreviewOut>(`${draftAt(draftId)}/change-proposal`, body),
  /**
   * 一句话改口径：默认只预览（confirm=false）；**confirm=true 才按同一批操作落一版**（内部翻成一次
   * DraftPatchIn 交给现有变更服务：expected_revision / request_id / CAS / 绑定同步一条不少）。
   * 说不清时回 409 并带澄清项，一条操作都不落
   */
  nlChange: (draftId: string, body: { instruction: string; confirm: boolean; expected_revision?: number
    request_id?: string; metrics?: string[]; target?: { workflow_id?: string; card_id?: string } }) =>
    post<NlChangeOut>(`${draftAt(draftId)}/nl-change`, body),
}

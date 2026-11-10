/**
 * 口径账本的界面文案（docs/design/next-round/02-口径账本.md 做法 3–6、8 的界面部分；总方案 X10）：证据正文里数字上的
 * 小标记、证据面板的「口径」一步、问数据轮次底部的摘要和动作、口径对账、可沉淀的问题、设置里的口径账本一组。
 *
 * 叫法：来自口径（受管 / 已发布）· 等价已证明 / 助手选用、相关口径、这次的推理、口径对账、可沉淀的问题；出具档位沿用
 * 「完整出具 / 降档出具 / 不予出具」（服务端给 tier_text）；版本写「固定版本」。标签四种、认法两种、级别两种的叫法在
 * terms.ts 的 LEDGER_TEXT（时间线那一行同一套），这里不重写。规矩同 terms.ts 开头的术语表。
 */
import { formatNumber } from './format'

/** 口径的写法：「周销售」v3（受管） */
export const caliberName = (name: string | null | undefined, version: number | null | undefined, level?: string) =>
  `「${name || '未命名'}」${version != null ? `v${version}` : ''}${level ? `（${level}）` : ''}`

export const LEDGER_UI = {
  // ---- 证据正文：来自口径的数字上的小标记 ----
  mark: {
    /** 标记上的字就是级别（受管 / 已发布）；相关口径的数不加标记，只在面板里说 */
    title: (caliber: string, how: string) => `来自口径${caliber} · ${how}`,
    aria: (caliber: string, how: string) => `，来自口径${caliber}，${how}`,
  },

  // ---- 证据面板最上面的「口径」一步 ----
  step: {
    title: '口径',
    pending: '正在比对口径…',
    pendingSub: '运行封存后在后台和已发布口径比对，比完这里会自动更新',
    timeout: '比对口径还没有结果：可能在排队，或比对超过了时限',
    retry: '再取一次',
    failed: '无法读取口径比对',
    unreadable: (reason: string) => `口径比对记录无法读取${reason ? `：${reason}` : ''}`,
    params: '参数',
    noParams: '没有参数',
    masked: '已遮罩',
    column: (c: string) => `对应口径里的列「${c}」`,
    how: {
      independent: '这次 Agent 独立写的 SQL 被证明和这份口径的取数等价',
      composed: '助手按问法选用了这份口径，取数照那一版生成；这份口径答的是不是这个问题，要由人确认',
    },
    relatedSub: '这个数是这次的推理，下面这份已发布口径和它相关',
    where: '差在哪',
    noDiff: '没有归纳出具体差异',
    fallback: '两条 SQL 的结构对不上，未能归纳成具体差异，请对照原文',
    notComparable: '这份口径不能对账',
    reasoningSub: '没有相关的已发布口径',
    unknownSub: '没有和已发布口径比对',
  },

  // ---- 按口径正式出具（整题等价时） ----
  issue: {
    button: '按口径正式出具',
    busy: '正在发起…',
    /** 服务端判了能自动出具：比对完就自动发起，这里不另给按钮 */
    auto: '整题等价于这份口径，已在按口径自动正式出具',
    autoWaiting: '正在按口径自动出具…',
    autoTimeout: '自动出具的正式运行还没有出现，可以手动发起',
    launched: (caliber: string) => `已按口径${caliber}发起正式运行`,
    again: (caliber: string) => `已经按口径${caliber}出具过同样参数的这一次`,
    issuedRun: '已出具',
    readOnly: '对话在回收站中，恢复后才能出具',
  },
  /** 出具过的正式运行，挂在轮次下面 */
  issued: {
    title: (auto: boolean) => (auto ? '按口径自动出具' : '按口径正式出具'),
    running: '正式运行进行中',
    rejected: '被驳回',
    failed: '正式运行没有完成',
    cancelled: '已取消',
    open: '打开运行',
    /** 使用者读不了正式运行的原始记录（记录页只给搭建者） */
    userNote: '正式运行的记录由搭建者在记录页查看',
  },

  // ---- 口径对账 ----
  reconcile: {
    button: '看差多少',
    turnButton: (caliber: string) => `对账：${caliber}`,
    title: (caliber: string) => `口径对账：${caliber}`,
    intro: '按你的问法和按口径，在同一份数据上各算一次，差值在对账卡里算。只读，不写任何数据',
    where: '差在哪',
    params: '核对参数',
    paramsHint: '参数按这次的 SQL 推断，请核对',
    paramsFixed: '参数取自比对时绑定出来的值',
    editParams: '修改参数',
    required: '必填',
    defaulted: '留空用口径的缺省值',
    maskedPlaceholder: '已遮罩，请重新填写',
    pairs: '选择要对照的指标',
    pairsHint: '认不出这次的哪一列对应口径的哪个指标，请为要对照的列选一个口径卡上的指标',
    /** 有预选的：按逐格比对证明了的那几列预选上，人可以改 */
    pairsSuggested: '已按逐格比对的结论预选了能证明对应的列，请核对，可以改选',
    pairSuggestedTag: '预选',
    pairNone: '不对照',
    pairEmpty: '至少选一组对照',
    launch: '发起对账',
    busy: '正在发起…',
    cancel: '取消',
    launched: '已发起对账，结果在下面',
    readOnly: '对话在回收站中，恢复后才能对账',
    /**
     * 这一项当前这个身份**发不了**（服务端没给这一项身份，只给了说明）：不摆按钮，只写清是哪份口径、为什么。
     * 允许的理由只有授权这一条（应用授权里给到「使用」），不替服务端编别的原因。
     */
    unavailable: (caliber: string) => `对账：${caliber}（当前这个身份没有发起对账的授权）`,
    unavailableHint: '要对这份口径对账，需要有它的使用授权；这里只写清差在哪，发不出请求',
    notAvailable: '这一项没有可发起对账的授权（服务端没给身份）：这里只写差在哪',
  },
  /** 对账的结果卡片 */
  result: {
    title: (caliber: string) => `口径对账 · ${caliber}`,
    running: '对账进行中…',
    failed: '对账没有完成',
    cancelled: '对账已取消',
    inputs: { binding: '按比对时推断的参数', checked: '按核对过的参数' } as Record<string, string>,
    ours: '按你的问法',
    caliber: '按口径',
    diff: '差值',
    rel: '相对差',
    loading: '正在取值…',
    noValue: '取值失败',
    open: (what: string, label: string) => `${label} · ${what}：点开看出处`,
    more: (n: number) => `展开其余 ${formatNumber(n)} 组`,
    less: '收起',
    original: (text: string) => `原答案里写的是 ${text}；对账时按同样的 SQL 重新取数，数据有变化时两者可能不同`,
    openRun: '打开对账运行',
    /** 「差在哪」的说明有没有帮人看懂（统计 E2 数它，衡量差异说明写得好不好） */
    feedback: {
      ask: '「差在哪」的说明有帮助吗？',
      useful: '说明有用',
      unclear: '看不懂',
      mine: (v: 'useful' | 'unclear') => `你选了「${v === 'useful' ? '说明有用' : '看不懂'}」，可以改`,
      counts: (useful: number, unclear: number) => [useful ? `${formatNumber(useful)} 人觉得有用` : '',
        unclear ? `${formatNumber(unclear)} 人看不懂` : ''].filter(Boolean).join(' · '),
      saved: '已记下你的反馈',
      readOnly: '对话在回收站中，恢复后才能反馈',
    },
  },

  // ---- 答案身上的召回标记（问数据的轮次、证据面板「口径」一步） ----
  recall: {
    /** 口径升版后在同一份数据上重算过、数没变 */
    checked: (caliber: string) => `已按口径${caliber} 核对，不影响`,
    /** 部分指标行序不定：那几项没能核对，照实列出 */
    checkedRowOrder: (names: string[]) => `其中${names.map((x) => `「${x}」`).join('、')}行序不定，没能核对`,
    /** 依据有变：目录里影响数字的项被驳回或修改、出现新码值、处理变化时改了目录（目录类），表或列被删、类型变了（表结构） */
    basis: (what: 'catalog' | 'structure' | 'other', items: number) =>
      `${what === 'structure' ? '依据的表结构有变' : what === 'catalog' ? '依据的数据目录有变' : '答案的依据有变'}`
      + `${items ? `（${formatNumber(items)} 项）` : ''}`,
    newErrors: (n: number) => `按新目录检查新出现 ${formatNumber(n)} 条错误级问题`,
    basisHint: '答案本身没有改：它依据的数据目录或表结构在答案之后有变化，请核对是否还能用',
    /** 看的这个人手上说的是这个答案的 recall 待办：口径升版后数变了的、依据有变检查出新问题的 */
    taskCaliber: (version: number | null | undefined) => `口径升到 ${version != null ? `v${version}` : '新版本'}，这个答案用到的数变了`,
    taskBasis: '依据有变，按新目录检查出了新的问题',
    taskOther: '有一条召回待办',
    task: '去待办处理',
  },

  // ---- 问数据：轮次底部的摘要 ----
  turn: {
    pending: '正在比对口径…',
    timeout: '比对口径还没有结果',
    /** 一行摘要：几个数来自哪份口径、几个是这次的推理、几个没有比对 */
    summary: (parts: { caliber: number; calibers: string[]; reasoning: number; related: number; unknown: number;
      external?: number; derived?: number; assessed?: number }) => {
      const out: string[] = []
      if (parts.caliber) {
        out.push(parts.calibers.length === 1 ? `${formatNumber(parts.caliber)} 个数来自口径${parts.calibers[0]}`
          : `${formatNumber(parts.caliber)} 个数来自 ${formatNumber(parts.calibers.length)} 份口径`)
      }
      if (parts.external) out.push(`${formatNumber(parts.external)} 个数是外部常量`)
      if (parts.derived) out.push(`${formatNumber(parts.derived)} 个数是规则派生`)
      if (parts.assessed) out.push(`${formatNumber(parts.assessed)} 个数是评估值`)
      const reasoning = parts.reasoning + parts.related
      if (reasoning) {
        out.push(`${formatNumber(reasoning)} 个数是这次的推理${parts.related ? `（其中 ${formatNumber(parts.related)} 个有相关口径）` : ''}`)
      }
      if (parts.unknown) out.push(`${formatNumber(parts.unknown)} 个数没有比对`)
      return out.join('，')
    },
    whole: (caliber: string, how: string) => `整题等价于口径${caliber} · ${how}`,
    label: '口径',
  },

  // ---- 工作流列表页：可沉淀的问题 ----
  certify: {
    open: '可沉淀的问题',
    openHint: '被反复问到、反复写出同样查询的问题：固化成口径草稿，人审后发布，以后再问就认得出等价',
    back: '返回工作流列表',
    title: '可沉淀的问题',
    intro: '三类来源分开列出。固化成口径草稿会新建一张草稿工作流（报告引用的查询变成固定 SQL 的取数节点），在画布上审改、按受管级别发布后才生效',
    kinds: {
      shape: '确定性重复：同样形状的 SQL',
      cluster: '问题聚类：问法相近的问题',
      reused: '被反复用的答案',
    } as Record<string, string>,
    kindHint: {
      shape: '问数据里执行过、被报告引用、又没有对上任何口径的查询，按数据源和规范化后的 SQL 分组：出现在多次运行、多段对话里',
      cluster: '只按问题原文聚类（不含对话历史），问法相近不等于口径相同，固化前请确认是同一个问题',
      reused: '导出过多次、做成过应用、在画布里打开或提取过的答案',
    } as Record<string, string>,
    empty: '暂时没有可沉淀的问题',
    emptyBody: (runs: number) => `看了最近 ${formatNumber(runs)} 次问数据的运行，没有达到门槛的重复`,
    threshold: (runs: number, conversations: number) => `门槛：同一形状出现在 ${formatNumber(runs)} 次以上运行、来自 ${formatNumber(conversations)} 段以上对话`,
    shapeStats: (runs: number, conversations: number) => `${formatNumber(runs)} 次运行 · ${formatNumber(conversations)} 段对话`,
    params: (p: number, c: number) => `${formatNumber(p)} 个参数候选 · ${formatNumber(c)} 个口径常量`,
    clusterStats: (n: number) => `${formatNumber(n)} 个相近的问题`,
    uses: (u: { exports: number; app: boolean; extracted: boolean; canvas: boolean }) => [
      u.exports ? `导出 ${formatNumber(u.exports)} 次` : '', u.app ? '做成过应用' : '', u.extracted ? '提取过模板' : '',
      u.canvas ? '在画布里打开过' : '',
    ].filter(Boolean).join(' · '),
    source: (s: string) => `数据源 ${s}`,
    shape: 'SQL 形状（取值已换成「?」）',
    related: '已有相关口径',
    frozen: '已固化成',
    turns: '来自这些轮次',
    openTurn: '打开对话',
    openRun: '打开运行',
    freeze: '固化成口径草稿',
    freezeTurn: '按这一轮固化',
    freezeBusy: '正在固化…',
    frozenOk: (name: string) => `已固化成草稿「${name}」，在画布里审改后发布`,
    dismiss: '不是口径',
    dismissTitle: '这不是口径',
    dismissLabel: '理由',
    dismissPlaceholder: '例如：每次问的是不同门店的临时核对，不需要固定下来',
    dismissNeed: '请写明理由',
    dismissConfirm: '确认不是口径',
    dismissed: '已记下，这一项不再出现',
    noRun: '这一项没有可固化的运行',
    loadFailed: '无法读取可沉淀的问题',
  },

  // ---- 从业务文档起草口径（/ledger/drafts；app/ledger/docdraft.py、docattach.py） ----
  drafts: {
    entry: '新增口径',
    title: '从业务文档起草口径',
    subtitle: '文档整理成定义和取数，自动试运行一次；去画布审改后由人签字发布',
    back: '返回口径目录',
    refresh: '刷新',
    /** 列表按阶段筛：全部、起草中、待审、已丢弃、失败。ready / questions_open / path_pending / attached 都归「待审」 */
    filter: {
      all: '全部',
      drafting: '起草中',
      review: '待审',
      discarded: '已丢弃',
      failed: '起草失败',
    } as Record<string, string>,
    stage: {
      drafting: '起草中',
      review: '待审',
      discarded: '已丢弃',
      failed: '起草失败',
    } as Record<string, string>,
    status: {
      drafting: '起草中',
      ready: '待签字发布',
      questions_open: '待确认问题',
      path_pending: '待接数据路径',
      attached: '已接上',
      rejected: '已驳回',
      failed: '起草失败',
    } as Record<string, string>,
    /** 细状态里值得在阶段旁边多说一句的：没有图、接了运行 */
    stageNote: (status: string) => status === 'path_pending' ? '还没有取数图'
      : status === 'attached' ? '已接上一次运行' : '',
    filterLabel: '按阶段筛',
    summary: (metrics: number, questions: number) => `${formatNumber(metrics)} 个指标 · ${
      questions ? `${formatNumber(questions)} 个未决问题` : '没有未决问题'}`,
    loadFailed: '无法读取口径草稿',
    empty: '还没有口径草稿',
    emptyBody: '选一份业务文档，把里面的指标整理成定义和取数草稿',
    emptyFiltered: '这个阶段没有草稿，换个阶段看看',

    // ---- 发起：选文档、可选数据源和起草要求 ----
    start: '起草一份新口径',
    documents: '文档',
    documentsHint: '从知识库里选；模型只读这些文档的段落',
    noDocuments: '知识库里还没有已完成的文档',
    noDocumentsBody: '先在知识页上传业务文档，切块完成后再来起草',
    documentMeta: (collection: string, chunks: number) => `${collection} · ${formatNumber(chunks)} 段`,
    datasource: '数据源（必选）',
    datasourceHint: '取数 SQL 对照这个库的真实表和列写，先探查过表结构才能起草',
    guidance: '起草要求（可选）',
    guidancePlaceholder: '例如：只整理和月度结算有关的指标',
    fixedFilters: '固定条件（可选）',
    fixedFiltersHint: '每次运行都不变的条件写死在 SQL 里，不变成入口字段：工厂、产线、属性组这类编码都放这里。'
      + '入口只留时间范围，最多再一个业务维度',
    fixedFiltersPlaceholder: "例如：line_code = 'FOAM01'、attribute_group = 'OEE'",
    submit: '开始起草',
    submitting: '正在发起…',
    needDoc: '请至少选一份文档',
    started: '已开始起草，完成并试运行后指标和数会出现在下面',

    // ---- 抽取中 ----
    drafting: '正在抽取指标…',
    draftingHint: '模型在服务端读文档、对照数据目录写定义和取数；指标多时会分几轮接着起草，离开页面不影响结果',
    /** 第 N 轮 · 已覆盖 M 条 · 还有 K 条 */
    progress: (round: number, covered: number, uncovered: number) =>
      `第 ${formatNumber(round)} 轮 · 已覆盖 ${formatNumber(covered)} 条 · 还有 ${formatNumber(uncovered)} 条`,
    draftingTimeout: '还没起草完，稍后刷新看结果',
    draftingFailed: '起草没有完成',

    // ---- 自动试运行 ----
    trial: '自动试运行',
    trialHint: '发起时按草稿图只读运行一次，只用来看数和定义对不对得上',
    trialRunning: '正在试运行…',
    trialSucceeded: '试运行已完成',
    trialFailed: '试运行没有成功',
    trialTimeout: '试运行超过一分钟还没有结果：稍后刷新再看',
    trialNoMetrics: '这次试运行没有产出指标值',
    trialUnavailable: '暂时无法读取试运行状态',
    trialRun: '看这次运行',
    /** 试运行读到的表：草稿页和口径目录共用，只读，不是正式出具 */
    trialResult: '只读试运行结果，不是正式出具',
    trialRows: (total: number) => `共 ${formatNumber(total)} 行`,
    trialTruncated: (shown: number) => `只显示前 ${formatNumber(shown)} 行`,
    trialNoRows: '这次试运行没有读到数据',
    /** 必填入口字段没有值：状态停在 needs_input，不拿系统编的值去跑 */
    trialNeedsInput: '等补入口字段',
    trialNeedsInputHint: '填好入口字段再试运行',
    trialStale: '需要重新试运行',
    trialStaleHint: '口径已修改，之前的试运行结果不再代表当前定义。',
    trialRunAction: '试运行',
    trialRunStarting: '正在发起…',

    // ---- 详情 ----
    documentsUsed: (n: number) => `${formatNumber(n)} 份文档`,
    docTruncated: '文档太长，后面的段落没有参与起草',
    catalogTruncated: '数据目录太大，只展开了文档点到的几张表',
    blocks: (n: number) => `${formatNumber(n)} 个取数节点`,
    /** 取数 SQL：草稿图里的模板，入口字段在运行时渲染；页面只读展示，不出现执行值 */
    sqlTitle: '取数 SQL',
    sqlHint: '取数模板，入口字段在运行时渲染',
    /** 逐组取数：第几组、这条 SQL 出哪几个指标 */
    sqlGroup: (n: number, metrics: number) => `第 ${formatNumber(n)} 组 · ${formatNumber(metrics)} 个指标`,
    metricsCount: (n: number) => `指标（${formatNumber(n)} 条）`,
    /** 指标卡默认收起：标题行看名字、单位、值和未决问题数，展开看定义、表达式和引用 */
    metricSummaryHint: '展开看定义、表达式和引用原文',
    metricQuestions: (n: number) => `${formatNumber(n)} 个未决问题`,
    noMetrics: '没有整理出指标',
    noMetricsBody: '文档里没有能逐字核对上引用的指标定义，可以换一份文档或补充起草要求后重来',
    synonyms: (list: string) => `又称：${list}`,
    /** 指标业务写法那一行：标签 + 说法 */
    businessValue: (value: string) => `：${value}`,
    unit: '单位',
    decimals: '小数位',
    expression: '表达式',
    definition: '定义',
    grain: '粒度',
    timeField: '时间字段',
    dimensions: '维度',
    filters: '过滤',
    numerator: '分子',
    denominator: '分母',
    scope: '范围',
    exclusions: '不包含',
    owner: '负责人',
    effectiveFrom: '生效日期',
    citations: '引用原文',
    paragraph: (n: number) => `第 ${n} 段`,
    citationMissing: '原文段落无法读取',
    questions: '未决问题',
    questionsOther: '跨指标的问题',
    resolve: '回答并移除',
    resolveHint: '在业务上问清之后勾选对应的问题，移除后状态会跟着变',
    resolveNeed: '请先勾选已经问清的问题',
    resolved: (n: number) => `已移除 ${formatNumber(n)} 个问题`,
    /** 图上有多张可写的口径卡、又认不出唯一目标：要人去画布上确认改哪一张（K02） */
    cardAmbiguous: '这份草稿的图上有好几张可写的口径卡：请先到画布上确认要改哪一张',
    cardMissing: '这份草稿的图上看不到可写的口径卡：请到画布上核对这张图',
    /** 写入被拒（有人先改过）：勾选还在、也不会自动重发，人确认后再点一次（K02） */
    writeStale: (current: number | null, base: number | null) => current == null
      ? `这条口径已经被别处改过（你基于 v${base ?? '?'}）：勾选的问题还在，确认后再点一次`
      : `这条口径已经被别处改过（你基于 v${base ?? '?'}，服务端现在是 v${current}）：勾选的问题还在，确认后再点一次`,
    dropped: '被丢弃的条目',
    droppedHint: '带不了能对照数据目录的取数，或引用无法逐字核对上的指标不留下',
    uncovered: (n: number) => `还没起草出来的指标（${formatNumber(n)} 条）`,
    uncoveredHint: '这轮起草结束后还是没能起草出来的指标：可以再起一份草稿，或在起草要求里写清它们的取数口径',
    alignment: '和已发布口径对齐',
    alignmentNew: '新的',
    alignmentDuplicate: '已有出处',
    alignmentConflict: '定义不同',
    model: (id: string) => `起草模型：${id}`,
    createdBy: (who: string) => `起草人：${who}`,

    // ---- 入口字段与发布 ----
    entryFields: '入口字段',
    entryFieldsHint: '试运行和发布后都按这些字段取值；带缺省值的可以不改',
    entryFieldNone: '这份草稿的图没有入口字段',
    entryPrefilled: '已预填，可改',
    required: '必填',
    optional: '可选',
    defaultValue: (value: string) => `缺省 ${value}`,
    publish: '去画布审改并发布',
    publishHint: '在画布上核对图、改完再发布；发布这一步由人签字',
    /** 发布前检查：服务端从图和试跑状态算，只读、不拦人 */
    checks: '发布前检查',
    checksHint: '只读检查，不拦人；未通过的项照实列出',
    checkOk: '通过',
    checkFail: '待处理',
    checksSummary: (passed: number, total: number) => `${formatNumber(passed)}/${formatNumber(total)} 项通过`,
    workflowVersion: (version: number) => `工作流 v${version}`,

    // ---- 接上一次运行（辅助入口） ----
    attach: '已经有答案？接上一次运行',
    attachHint: '已经有一次运行得出的答案，让它认这份定义：取数照抄那次运行执行过的查询，不重新生成；接上后产出可试运行的工作流版本，不发布',
    runId: '运行 id',
    runIdPlaceholder: '粘贴运行记录里的 id',
    recentRuns: '最近成功的运行',
    recentRunsNone: '没有可选的运行',
    recentRunsPick: '选一次运行填入左边',
    attachAction: '接上',
    attaching: '正在接入…',
    attachNeedRun: '请先填运行 id',
    attached: (version: number) => `已接上，工作流产出版本 v${version}`,
    attachWarnings: '这些指标没有对上已有口径卡，需要人决定新建还是忽略：',
    openWorkflow: '打开工作流',

    // ---- 不是口径 ----
    dismiss: '不是口径',
    dismissTitle: '这份草稿不是口径',
    dismissNote: '理由（可选）',
    dismissPlaceholder: '例如：一次性的核对，不需要固定下来',
    dismissConfirm: '确认不是口径',
    dismissed: '已驳回，这份草稿不再出现在待办里',
  },

  // ---- 工作台：业务规则（K04 的能力描述 + K06 的改法；只在服务端说得出能改什么时才开表单）----
  rules: {
    title: '业务规则',
    hint: '这两项是业务定义的写法。保存时服务端在同一个版本里把取数一起改过来；改不了的原样保留，并写清原因',
    fieldHelp: (help: string) => help,
    /** 这一项现在取值不一致（同一张卡上的指标写法不同）：不替人选一个默认值 */
    mixed: '现在写法不一致',
    mixedHint: '保存后这些指标会统一成选中的写法',
    keep: '保持各自现在的写法',
    /** 这一项这张卡上没人声明：改不了（没有落点的取数不会被硬改） */
    fieldNone: '这张卡上的指标没有声明这一项',
    /** 保存后会变哪几条指标：指标名 + 这一项现在 → 保存后 */
    previewTitle: '保存后会变成',
    before: '现在',
    after: '保存后',
    unaffected: (n: number) => `另有 ${formatNumber(n)} 条指标没有声明这一项，这次不动它们`,
    unaffectedHint: '要改它们得先由技术侧在指标上声明这两项、并给取数加上受控绑定',
    affected: (n: number) => `会改到 ${formatNumber(n)} 条指标`,
    noChange: '还没有改动：选一个和现在不一样的写法，这里就会列出会改到哪些指标',
    save: '保存这次修改',
    saving: '正在保存…',
    saveNeedChange: '先选一个和现在不一样的写法',
    /** 保存回执 */
    saved: (version: number | null) => version == null ? '已保存' : `已保存成 v${formatNumber(version)}`,
    savedAligned: '定义和取数已经在同一个版本里改过来了',
    savedNotSynced: '定义已经存下来了，取数一个字没动',
    savedNoChange: '这次操作没有改动任何内容：图没有换版本',
    invalidated: '这张卡的内容变了：旧的试运行和验证不再算覆盖这一版，请重新试运行',
    nextStep: '下一步：按新定义重新试运行，并对一次独立样例',
    replayed: '这一次和上一次是同一个请求，服务端认得出来，没有多存一版',
    /** 基准版本过期：保留页面上的选择，不自动重发 */
    stale: (current: number | null, base: number | null) => current == null
      ? `这条口径已经被别处改过（你基于 v${base ?? '？'}）：你选好的写法还在，确认后再点一次「保存这次修改」`
      : `这条口径已经被别处改过（你基于 v${base ?? '？'}，服务端现在是 v${current}）：你选好的写法还在，确认后再点一次`,
    /** 撤销：用保存前的值构造逆操作，再存一版，不顺着一版整份覆盖 */
    undo: (n: number) => `撤销这次修改（回到 ${formatNumber(n)} 处改动之前的值）`,
    undoing: '正在撤销…',
    undone: (version: number | null) => version == null ? '已撤销这次修改' : `已撤销这次修改，现在这一版是 v${formatNumber(version)}`,
    undoHint: '撤销也是新增一版（按当前版本和新的请求键提交逆操作），不改写已经存下的历史版本',
    /** 不能改的资产：照实说为什么，并给技术待办 */
    readOnly: '这份草稿现在不能在这里改',
    unbound: '这张卡上的指标没有声明统计时间口径和退款处理：按旧资产处理，不提供修改',
    noCard: '图上看不到能改的口径卡：先在画布上确认要改哪一张',
    noRevision: '这份草稿还没有版本号：刷新页面后再改',
    noWorkflow: '这份草稿还没有取数图：起草完成或接上一次运行之后才能改业务规则',
    /** 绑定的同步状态（同步只说这两边对得上，不等于验证通过） */
    status: {
      aligned: '定义和取数已经同步',
      needs_update: '定义和取数还没同步',
      unsupported: '这条取数同步不了',
      unbound: '这张卡没有声明业务字段',
    } as Record<string, string>,
    alignedNote: '同步只说业务定义和这张取数对得上，不等于业务验证通过',
    reason: '这条为什么改不了',
    fixLabel: '技术待办',
    fix: {
      none: '不用处理',
      sync: '可以按受控模板确定性同步：保存时会把取数一起改过来',
      author: '要作者侧补或改受控绑定：这条取数里没有这个业务字段的落点，或者映射里没有这个取值',
      manual: '先由人拿主意：同一条取数上的指标说法不一致，或者取数被手工改过',
    } as Record<string, string>,
    fixNote: '这一条不受工作台影响，复杂或改不了的取数不会被字符串替换硬改',
  },

  // ---- 工作台：验证与审阅（K05 的记录，K06 的读法）----
  validation: {
    title: '验证与审阅',
    records: '验证记录',
    empty: '还没有验证记录',
    emptyBody: '按入口字段试运行一次就会留下一条记录；带独立期望时才能判「对得上」',
    counts: (total: number, covering: number) =>
      `${formatNumber(total)} 条记录 · 覆盖当前版本 ${formatNumber(covering)} 条`,
    method: (version: number) => `比较方法 v${formatNumber(version)}`,
    /** 结论：只有「有独立期望对得上」并且「有覆盖它的认证审阅」才是验证通过 */
    verdict: {
      none: '还没有验证记录',
      stale: '现有记录不覆盖这一版',
      observed: '只记了试运行结果',
      mismatched: '和独立期望对不上',
      failed: '试运行没有成功',
      unverified: '还不能算验证通过',
      verified: '验证通过',
      rejected: '被审阅驳回',
    } as Record<string, string>,
    /** 一条记录的状态 */
    status: {
      matched: '和独立期望对得上',
      mismatched: '和独立期望对不上',
      observed: '只记了试运行结果',
      unverified: '证据不足',
      failed: '试运行没有成功',
    } as Record<string, string>,
    covers: '覆盖当前版本',
    notCovers: '不覆盖当前版本',
    /** 只有服务端明确说覆盖才算覆盖：判不了就照实说判不了，不默认成覆盖 */
    coversUnknown: '覆盖判断未知',
    coversHint: '定义、取数或绑定改过之后，旧记录留着可追，但不算覆盖这一版',
    /** 结论依据的是哪一条记录：说出来，别让人把别的记录当成这一条的结论 */
    basis: (when: string, status: string) => `结论按 ${when} 那条记录（${status}）`,
    basisNone: '还没有一条记录可以下结论',
    semanticGaps: (n: number) => `其中 ${formatNumber(n)} 条指标的语义说明仍待人工确认`,
    expected: '独立期望',
    expectedNone: '这条没有独立期望：只记试运行结果，不能算验证通过',
    expectedSource: '期望来自',
    sources: {
      human_confirmed: '人工确认',
      fixture: '固定夹具',
      independent_computation: '独立计算',
    } as Record<string, string>,
    expectedHint: '期望要独立于这次试运行：人工确认、固定夹具或独立算出来的标准答案，不能抄这次的结果当答案',
    actual: '实际结果',
    ok: '对得上',
    notOk: '对不上',
    unknown: '判不了',
    noValue: '没有取到值',
    rowsHint: (rows: number | null) => rows == null ? '' : `${formatNumber(rows)} 行`,
    masked: '已遮罩',
    problems: '这一条记下的问题',
    review: '审阅',
    noReview: '还没有审阅决定',
    reviewConfirmed: '确认',
    reviewRejected: '驳回',
    reviewBy: (who: string, verified: boolean) => verified ? `由 ${who}` : `由 ${who}（自报署名，未认证）`,
    reviewScope: '确认范围',
    /** 确认范围没有盖住这条记录的全部指标：不能当成整卡通过 */
    reviewPartial: '没有盖住这条记录的全部指标：不能当整卡通过',
    reviewCovers: '这次审阅覆盖当前版本',
    reviewNotCovering: '这条不覆盖当前版本：确认了也不算覆盖这一版',
    reviewCoversUnknown: '这次审阅的覆盖判断未知',
    reviewNote: '说明（可空）',
    reviewNotePlaceholder: '例如：对过业务口径和样例来源',
    confirm: '确认这条',
    reject: '驳回这条',
    reviewing: '正在提交审阅…',
    reviewDone: (decision: string) => `已记录一次审阅：${decision}`,
    reviewScopeHint: '审阅只认服务端身份，界面代写不了审批人；同一份记录可以再审，显示的是最新一条',
    baseline: '导入基线',
    baselineKnown: (revision: number | null, metrics: number, fields: number) =>
      `有可还原的导入基线${revision != null ? `（第 ${formatNumber(revision)} 版接受）` : ''}：`
      + `${formatNumber(metrics)} 条指标、${formatNumber(fields)} 个字段`,
    baselineUnknown: '这份草稿没有导入基线：当时被接受的导入值还原不出来，不能拿现在的内容当作它',
    baselineHint: '基线说的是「上次被接受的导入值」，人工改过这里的内容不会推进它',
    /** 确认范围没盖住整条记录时的说明：服务端按范围收回也好、页面自己按范围收回也好，都用同一句 */
    partialVerdict: (covered: string, missed: string, total: number) =>
      `这次确认盖住了 ${covered || '—'}，没有盖住 ${missed}（这条记录一共 ${formatNumber(total)} 条指标）：`
      + '不能算整卡验证通过',
    /** 试运行那一块里的「这次试运行的验证记录」：只说本次那一条，旧记录不冒充本次 */
    trialRecord: '这次试运行的验证记录',
    trialWaiting: '试运行还在进行，正在等本次的验证记录',
    trialNoRecord: '这次试运行还没有留下验证记录：缺证据不能算验证通过',
    /** 试运行那一条的紧凑写法：旁边就是状态和覆盖判断，不铺开整句 */
    noExpectation: '没有独立期望',
    /** 带独立期望试运行 */
    trialWithExpect: '带独立期望试运行',
    trialWithExpectHint: '先改上面那两项业务规则、或确认现在的写法就是要验的，再填标准答案试一次',
    expectFor: (name: string) => `${name} 的期望值`,
    expectNeedAll: '要逐条给全卡指标，不能只给其中几条',
    expectSourceLabel: '期望来自',
    expectSubmit: '带期望试运行',
    expectSubmitting: '正在发起…',
    runTrial: '不带期望试运行',
    openRun: '看这次运行',
    more: (n: number) => `展开其余 ${formatNumber(n)} 条记录`,
    fieldsHint: '这里只摆指标值和验证依据，不显示取数语句、内部编号和字段路径',
  },

  // ---- K11 增量重导入：B / L / R 三方、按字段接受 / 拒绝、人工对齐、显式恢复 ----
  imports: {
    title: '文档更新（重新读一遍文档）',
    hint: '用最新的文档重抽一遍候选，和现在的内容、上次被接受的导入值逐字段比。'
      + '候选只是候选：你不点接受，图上和字段基线一个字节都不动',
    create: '按文档重新读一遍',
    creating: '正在重新读取…',
    started: '已开始重新读取：抽完候选会出现在下面',
    createHint: '在服务端重读这份草稿的来源文档、抽候选，再逐字段比对；不改现在的内容',
    runs: '重导入记录',
    runsEmpty: '还没有重导入记录',
    /** 一条记录：什么时候读的、已经处置了几条 */
    runLabel: (when: string, decided: number, total: number) =>
      `${when} · 已处置 ${formatNumber(decided)}/${formatNumber(total)} 条`,
    open: '看这一次',
    createdAt: (when: string) => `读过一次：${when}`,
    itemsTotal: (n: number) => `这次抽出 ${formatNumber(n)} 条候选`,
    /** run 的状态 */
    status: {
      queued: '排队中',
      running: '正在重新读取',
      succeeded: '读完了',
      partial: '读完了（有没抽全的）',
      failed: '没有读完',
      interrupted: '中断了，可以恢复',
      cancelled: '已停止',
    } as Record<string, string>,
    /** 逐格的三方状态说法 */
    state: {
      update: '无冲突更新',
      conflict: '人工修改冲突',
      new: '新增候选',
      missing: '这次没提取到',
      manual: '需要人工对齐',
      unchanged: '不变',
    } as Record<string, string>,
    stateHint: {
      update: '上次接受之后没人动过这一格：接受就取文档里的新说法',
      conflict: '你人工改过这一格，文档又给了一个新说法：接受取文档的，拒绝就留你现在的内容',
      new: '现在的内容和基线都没有这一格：文档新提出来的，可以接受',
      missing: '这次没提取到这一格（漏提取不当删除）：现在的内容原样留着，不能接受',
      manual: '这一格没有可比的基线：要你人工对齐之后才能定',
      unchanged: '和现在的内容或者基线一致：不用接受',
    } as Record<string, string>,
    b: '上次被接受的导入值',
    l: '现在的内容',
    r: '这次文档里的说法',
    noCell: '没有这一格',
    /** 一格的值转成一行字：空值、空数组和结构化内容各有各的说法，不把 JSON 摆上来 */
    noValue: '没写',
    emptyValue: '（空）',
    objectValue: '（一段结构化内容）',
    citationCount: (n: number) => `${formatNumber(n)} 段引用原文`,
    /** 认不出的字段路径不摆原始路径，用一句笼统的说法 */
    otherField: '这一格',
    /** 工作项的状态（逐字段算出来的那条） */
    itemStatus: {
      pending: '还没处置',
      accepted: '已接受',
      rejected: '已拒绝',
      partial: '有漏提取的格子',
      failed: '这条没抽好',
    } as Record<string, string>,
    accept: '接受',
    reject: '拒绝',
    /** 拒绝的说明（可空）：记在这一次的决定上 */
    notePlaceholder: '拒绝的说明（可空）',
    acceptSelected: (n: number) => `接受选中的 ${formatNumber(n)} 格`,
    rejectSelected: (n: number) => `拒绝选中的 ${formatNumber(n)} 格`,
    accepting: '正在接受…',
    rejecting: '正在拒绝…',
    /** 一条上所有「无冲突更新 + 新增候选」一次收掉；也是一次显式的点击 */
    acceptClean: (n: number) => `接受这条上无冲突的 ${formatNumber(n)} 格`,
    nothingSelected: '先选要接受或者拒绝的格子',
    nothingClean: '这条上没有可以直接接受的格子',
    needRevision: '这份草稿还没有版本号：刷新页面之后再改',
    missingNoDelete: '漏提取不当删除',
    /** 人工对齐：没对上、或者同名不同范围时必须由人指定 */
    align: '对齐到哪条指标',
    alignAction: '对齐',
    aligning: '正在对齐…',
    alignHint: '名字像不像不参与对齐，只认稳定身份和来源证据；机器对不上就必须由人指定',
    alignNone: '不对齐（当作新指标）',
    alignNeed: '先选要对齐到哪条指标',
    reasonLabel: '为什么这么对',
    reason: {
      metric_id: '候选带着和图上一致的稳定身份',
      evidence: '按来源证据对上了（引的是同一段原文）',
      new: '没有对得上的指标：名字相不相似不参与自动合并',
      manual: '要你来定：稳定身份对不上来源证据，或者证据对上了不止一条',
    } as Record<string, string>,
    /** 显式恢复中断的那一次 */
    resume: '恢复这次导入',
    resuming: '正在恢复…',
    resumed: '已排进后台，正在接着读',
    resumeHint: '续做还没完成的工作项；已经接受或者拒绝的不动，不会重复建版',
    unfinished: (n: number) => `还有 ${formatNumber(n)} 条没做完`,
    /** 基准版本过期：选择保留，不自动重发 */
    stale: (current: number | null, base: number | null) => current == null
      ? `这条口径已经被别处改过（你基于 v${base ?? '？'}）：你选好的格子还在，刷新后再点一次接受`
      : `这条口径已经被别处改过（你基于 v${base ?? '？'}，服务端现在是 v${current}）：你选好的格子还在，刷新后再点一次接受`,
    /** 服务端说这次导入比对的 L 已经过期（打开之后图被别处改过）：按现在的 L / B 重算三方，不能拿旧 L 直接覆盖 */
    staleL: (names: string) => names
      ? `这几格的当前内容和这次导入算候选时的那一版不一样了：${names}。`
        + '已经按现在的口径把这次导入的三方重算了一遍，选好的格子还在，核对之后再点一次接受'
      : '这次导入比对的还是旧的内容（你打开之后这张卡被别处改过）：已经按现在的口径把这次导入的三方重算了一遍，'
        + '选好的格子还在，核对之后再点一次接受',
    accepted: (revision: number | null) => revision == null ? '已接受选中的格子' : `已接受选中的格子，落到 v${formatNumber(revision)}`,
    acceptedHint: '这次图和字段基线是在同一次提交里推进的：没有接受的格子不会推进基线',
    rejected: '已拒绝选中的格子：现在的内容和字段基线都没动',
    replayed: '这一次和上一次是同一个请求，服务端认得出来：没有多存一版，基线也读得回来',
    /** 字段基线覆盖到哪一步：有基线不等于整张都有 */
    coverageLabel: '字段基线的覆盖',
    coverageComplete: '这张卡的每一格都有基线',
    coveragePartial: (fields: number, metrics: number) =>
      `还没盖满这张卡：还有 ${formatNumber(fields)} 格没有基线`
      + (metrics > 0 ? `、${formatNumber(metrics)} 条指标整条没有基线` : '')
      + '。只有被接受的格子才往前走，没覆盖到的格子下次会按「新增候选 / 需要人工对齐」处理',
    coverageUnknown: '这一份基线读不出来（不在、或者内容与哈希不符）：不能拿现在的内容当作它',
    coverageHint: '基线说的是「上次被接受的导入值」，人工改过现在的内容不会推进它',
    conflicts: (n: number) => `这次有 ${formatNumber(n)} 格要人处置（冲突、要人工对齐、漏提取）`,
    noItems: '这次没有抽出候选：可以再读一次，或者换一份文档重起草稿',
    /** 没接管到图上：导入落不到图上 */
    readOnly: '这份草稿还没有取数图：重新导入落不到图上，先起草完成或者接上一次运行',
    itemUnaligned: '这条还没对上图上的指标',
    /** 一条指标在页面上找不到名字：不摆内部编号 */
    unknownMetric: '（这条指标在页面上找不到名字）',
    fieldCount: (n: number) => `${formatNumber(n)} 格`,
  },

  // ---- K12 受限的自然语言修改：只产出提案，确认后才落一版 ----
  nl: {
    title: '用一句话改口径',
    hint: '只认已经声明过的业务写法（统计时间口径、退款处理这类）。先看会影响什么，确认之后才落一版；'
      + '预览本身不写任何东西',
    placeholder: '例如：订单金额按支付时间算，排除全额退款',
    preview: '预览影响',
    previewing: '正在看影响…',
    needText: '先写一句要改什么',
    /** 认可的操作：一条一行「指标 · 字段：现在 → 保存后」 */
    changeCount: (n: number) => `这句话会改到 ${formatNumber(n)} 处`,
    noChange: '这句话没有能按字段改的地方',
    now: '现在',
    after: '保存后',
    removeValue: '删掉这一项',
    noValue: '没写',
    /** 含糊的条件先澄清：一条操作都不产出 */
    clarifyTitle: '这句还说不清',
    clarifyHint: '没有产出任何修改：说清之后再预览一次',
    clarifyNoMatch: '这句话里没有能按字段改的已声明写法',
    confirm: '确认并应用',
    confirming: '正在应用…',
    confirmHint: '确认之后才按这批操作落一版（带读到的版本和请求键）；预览不会写任何东西',
    needConfirm: '先预览，看清影响之后再确认',
    /** 预览的是哪一段原文：确认发的就是这一段，页面显示的（A）和实际落下的（B）不是两句话 */
    previewedAs: (text: string) => `这份影响是按这一段原文算的：「${text}」`,
    changedNotice: '输入改过了：上面还是上一次预览的结果，请重新预览之后再确认',
    needPreviewAgain: '这句话改过了：要先重新预览；确认的必须是预览里那一段原文',
    /** 服务端这次落下的操作和预览里的不是同一批：照实提示，让人到画布上核对 */
    mismatch: '服务端这次落下的操作和预览里的不是同一批：请到画布上核对这一版',
    applied: (revision: number | null) => revision == null ? '已应用这句话' : `已应用这句话，落到 v${formatNumber(revision)}`,
    invalidated: '这张卡的内容变了：旧的试运行和验证不再覆盖这一版，请重新试运行',
    failed: '这次没有改任何东西：',
    replayed: '这一次和上一次是同一个请求：没有多存一版',
    readOnly: '这份草稿还没有取数图：一句话改不了任何东西，先起草完成或者接上一次运行',
    noRevision: '这份草稿还没有版本号：刷新页面之后再改',
    /** 一句话点到一条页面上找不到的指标：不硬套，也不摆内部编号 */
    unknownMetric: '（这条指标在页面上找不到名字）',
    offerMissing: '这条指标在这张卡上找不到，没有落成修改',
  },

  // ---- 设置：口径账本一组 ----
  settings: {
    title: '口径账本',
    hint: '问数据的答案封存后在后台和已发布口径比对：按口径正式出具、口径对账、升版后的召回都按这里的设置',
    autoIssue: '自动按口径正式出具',
    autoIssueHint: '问数据的答案整题等价于受管口径（Agent 独立写的 SQL 被证明等价、图里没有人工节点和写入）时自动发起一次正式运行；不满足的只给一键出具',
    dailyLimit: '按口径正式出具每日上限',
    dailyLimitHint: '自动的、一键的合计；0 表示不再按口径出具。正式运行含报告撰写的模型费用',
    timeout: '口径比对时限（秒）',
    timeoutHint: '一次运行的口径比对最多算这么久，超时的照实记没比完',
    recallLimit: '口径升版后每次最多重算几组',
    recallLimitHint: '发布新版本后自动重算受影响的答案，按参数去重成组；超出的写明另有几组未重算；0 表示不自动重算',
    invalid: {
      limit: (max: number) => `请填写 0 到 ${formatNumber(max)} 之间的整数`,
      timeout: (lo: number, hi: number) => `请填写 ${lo} 到 ${hi} 之间的秒数`,
    },
    saveInvalid: '口径账本的设置中有无效值，请修正后再保存',
  },
}

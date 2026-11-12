/**
 * 独立语义指标工作台与口径文档一键拆解中心（SemanticMetricStudio）：
 *
 * 1. 即存即用指标卡片编辑器（MetricEditorModal）：同时支持「单表聚合表达式」与「多表 / 参数化 SQL 模板」两种模式；
 * 2. 后台文档一键拆解与智能提取工作台（MetricSuggestModal）：喂入业务口径文档（粘贴 Markdown、上传附件、选择知识库文档），
 *    在服务端后台分阶段拆解「字段目录补丁 + 单表与多表指标口径 + 标准业务实体映射 + 知识库归档」，支持实时进度、关闭弹窗后台继续执行、行内试算与一键应用；
 * 3. 语义指标卡片列表与在线试算面板（SemanticMetricCardsSection）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Check, CheckCircle2, ChevronDown, Clock, Code2, Database, Edit3,
  FileText, Layers, Play, Plus, Power, RotateCcw, ShieldAlert, Sparkles, Tag, Trash2, Upload, X, Zap,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type {
  DecomposeAttachmentOut,
  MetricCalcMode,
  SemanticDecomposeJob,
  SemanticMetric,
  SemanticMetricDimension,
  SemanticMetricInput,
  SemanticMetricPreviewOut,
} from '../../api/semanticMetrics'
import { confirmDialog, CopyButton, Field, Modal, Notice, Spinner, toast } from '../../components/ui'
import { useAtLeast } from '../../store/auth'
import { useDatasources } from '../../store/catalog'
import type { CanonicalEntity } from '../../typesLedger'
import type { KbDocument } from '../../types'

const TEMPLATE_SLOT_CHIPS: { token: string; tip: string }[] = [
  { token: '{{select_dims}}', tip: '动态展开分组列（放在 SELECT 紧后，无分组时自动省去）' },
  { token: '{{and_where}}', tip: '在 WHERE 1=1 后动态追加日期区间与维度过滤条件' },
  { token: '{{where}}', tip: '完整 WHERE 子句占位符（无过滤条件时自动省去 WHERE）' },
  { token: '{{group_by}}', tip: '按所选时间粒度与维度动态生成 GROUP BY 子句' },
  { token: '{{order_by}}', tip: '动态生成趋势升序或指标降序 ORDER BY 子句' },
  { token: '{{period_days}}', tip: '动态统计周期自然天数（如 TEEP 日历秒数 = {{period_days}} * 86400）' },
  { token: '{{date_from}}', tip: '查询起始日期字面量（未传时为 NULL）' },
  { token: '{{date_to}}', tip: '查询结束日期字面量（未传时为 NULL）' },
]

function splitAliases(raw: string): string[] {
  return raw
    .split(/[，,、;\n]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function formatCatalogValue(val: unknown): string {
  if (val === null || val === undefined || val === '') return '（未填写）'
  if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') return String(val)
  if (typeof val === 'object') {
    const obj = val as Record<string, unknown>
    if ('column' in obj) {
      const col = String(obj.column ?? '')
      const rule = String(obj.rule ?? '')
      return rule ? `${col}（${rule}）` : col
    }
    return Object.entries(obj)
      .map(([k, v]) => `${k}=${String(v ?? '')}`)
      .join('，')
  }
  return String(val)
}

function PreviewResultPanel({ preview }: { preview: SemanticMetricPreviewOut }) {
  return (
    <div className="mt-2 space-y-2 rounded-md border bg-bg p-2.5 text-xs" data-metric-preview-result="">
      <div className="flex flex-wrap items-center justify-between gap-2 text-2xs text-dim">
        <span>
          数据源：{preview.source_name} · 返回 {preview.row_count} 行 · 耗时 {preview.elapsed_ms} ms
        </span>
        <CopyButton text={preview.sql} />
      </div>
      <pre className="mono max-h-32 overflow-auto rounded border bg-panel p-2 text-[11px] leading-relaxed text-dim">
        {preview.sql}
      </pre>
      {preview.latest_available_date && (
        <Notice tone="info">
          当前筛选条件下结果为空；探查到该表最新可用业务日期为 {preview.latest_available_date}。
        </Notice>
      )}
      {preview.entity_alignments?.length > 0 && (
        <div className="text-2xs text-dim">
          标准实体编码自动转换：
          {preview.entity_alignments.map((a, idx) => (
            <span key={idx} className="ml-1 rounded border bg-panel px-1.5 py-0.5">
              {String(a.dimension ?? '')}：{String(a.input ?? '')} → {Array.isArray(a.translated) ? a.translated.join(' / ') : ''}
            </span>
          ))}
        </div>
      )}
      {preview.columns.length > 0 && preview.rows.length > 0 ? (
        <div className="max-h-48 overflow-auto rounded border bg-panel">
          <table className="w-full border-collapse text-left text-2xs">
            <thead>
              <tr className="border-b bg-elev text-dim">
                {preview.columns.map((c) => (
                  <th key={c} className="px-2 py-1.5 font-medium">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {preview.rows.slice(0, 30).map((r, ri) => (
                <tr key={ri} className="border-b border-hairline last:border-0">
                  {r.map((cell, ci) => (
                    <td key={ci} className="tnum px-2 py-1 text-fg">
                      {cell === null || cell === undefined ? <span className="text-faint">NULL</span> : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-2xs text-faint">查询执行成功，当前条件下暂无数据行。</div>
      )}
    </div>
  )
}

interface TableColumnMeta {
  name: string
  type: string
  label?: string
}

export function MetricEditorModal({
  open,
  metric,
  entities,
  onClose,
  onSaved,
}: {
  open: boolean
  metric: SemanticMetric | null
  entities: CanonicalEntity[]
  onClose: () => void
  onSaved: () => void
}) {
  const { list: allSources } = useDatasources()
  const sources = useMemo(() => allSources.filter((s) => s.enabled !== false), [allSources])

  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [unit, setUnit] = useState('')
  const [aliasesText, setAliasesText] = useState('')
  const [description, setDescription] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [calcMode, setCalcMode] = useState<MetricCalcMode>('expression')
  const [tableName, setTableName] = useState('')
  const [sqlExpression, setSqlExpression] = useState('')
  const [sqlTemplate, setSqlTemplate] = useState('')
  const [dateColumn, setDateColumn] = useState('')
  const [filterSql, setFilterSql] = useState('')
  const [defaultTimeGrain, setDefaultTimeGrain] = useState('day')
  const [dimensions, setDimensions] = useState<SemanticMetricDimension[]>([])

  const [tables, setTables] = useState<string[]>([])
  const [columns, setColumns] = useState<TableColumnMeta[]>([])
  const [catalogHint, setCatalogHint] = useState<string>('')
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [preview, setPreview] = useState<SemanticMetricPreviewOut | null>(null)
  const tplRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (!open) return
    setPreview(null)
    if (metric) {
      setName(metric.name)
      setKey(metric.key)
      setUnit(metric.unit || '')
      setAliasesText((metric.aliases ?? []).join('、'))
      setDescription(metric.description || '')
      setSourceId(metric.source_id)
      setCalcMode(metric.calc_mode === 'sql_template' ? 'sql_template' : 'expression')
      setTableName(metric.table_name || '')
      setSqlExpression(metric.calc_mode === 'sql_template' && metric.sql_expression?.startsWith('[多表模板]') ? '' : (metric.sql_expression || ''))
      setSqlTemplate(metric.sql_template || '')
      setDateColumn(metric.date_column || '')
      setFilterSql(metric.filter_sql || '')
      setDefaultTimeGrain(metric.default_time_grain || 'day')
      setDimensions(metric.dimensions ?? [])
    } else {
      setName('')
      setKey('')
      setUnit('')
      setAliasesText('')
      setDescription('')
      setSourceId(sources[0]?.id ?? '')
      setCalcMode('expression')
      setTableName('')
      setSqlExpression('')
      setSqlTemplate('')
      setDateColumn('')
      setFilterSql('')
      setDefaultTimeGrain('day')
      setDimensions([])
    }
  }, [open, metric, sources])

  useEffect(() => {
    if (!open || !sourceId) {
      setTables([])
      return
    }
    let alive = true
    api.datasources.schema(sourceId).then(
      (res) => {
        if (!alive) return
        const nextTables: string[] = Array.isArray(res?.tables) ? res.tables : []
        setTables(nextTables)
        if (!metric && nextTables.length > 0 && !tableName) {
          setTableName(nextTables[0])
        }
      },
      () => { if (alive) setTables([]) },
    )
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sourceId])

  useEffect(() => {
    if (!open || !sourceId || !tableName) {
      setColumns([])
      setCatalogHint('')
      return
    }
    let alive = true
    api.dataCatalog.get(sourceId, tableName).then(
      (detail: any) => {
        if (!alive) return
        const rawCols = Array.isArray(detail?.structure?.columns) ? detail.structure.columns : []
        const colNotes = detail?.notes?.columns ?? {}
        const mapped: TableColumnMeta[] = rawCols.map((c: any) => ({
          name: String(c.name ?? ''),
          type: String(c.type ?? ''),
          label: typeof colNotes?.[c.name]?.label?.value === 'string' ? colNotes[c.name].label.value : undefined,
        })).filter((c: TableColumnMeta) => c.name)
        setColumns(mapped)
        const bDateRaw = detail?.notes?.business_date?.value
        const bDate = typeof bDateRaw === 'string'
          ? bDateRaw
          : (bDateRaw && typeof bDateRaw === 'object' && typeof bDateRaw.column === 'string' ? bDateRaw.column : '')
        const vFilter = typeof detail?.notes?.valid_filter?.value === 'string' ? detail.notes.valid_filter.value : ''
        const tLabel = typeof detail?.notes?.label?.value === 'string' ? detail.notes.label.value : ''
        const hints = [
          tLabel ? `目录名：${tLabel}` : '',
          bDate ? `推荐业务时间列：${bDate}` : '',
          vFilter ? `推荐有效过滤：${vFilter}` : '',
        ].filter(Boolean)
        setCatalogHint(hints.join(' · '))
        if (!metric && calcMode === 'expression') {
          if (bDate) setDateColumn((prev) => prev || bDate)
          if (vFilter) setFilterSql((prev) => prev || vFilter)
        }
      },
      () => {
        if (alive) {
          setColumns([])
          setCatalogHint('')
        }
      },
    )
    return () => { alive = false }
  }, [open, sourceId, tableName, metric, calcMode])

  const isTemplate = calcMode === 'sql_template'

  const insertSlotToken = (token: string) => {
    const el = tplRef.current
    if (!el) {
      setSqlTemplate((prev) => (prev ? `${prev} ${token}` : token))
      return
    }
    const start = el.selectionStart ?? sqlTemplate.length
    const end = el.selectionEnd ?? sqlTemplate.length
    const next = `${sqlTemplate.slice(0, start)}${token}${sqlTemplate.slice(end)}`
    setSqlTemplate(next)
    requestAnimationFrame(() => {
      el.focus()
      const pos = start + token.length
      el.setSelectionRange(pos, pos)
    })
  }

  const fillTemplateSkeleton = () => {
    const anchor = tableName || tables[0] || 'fact_table'
    const metricAlias = key.trim() || 'metric_value'
    const sampleDate = columns[0]?.name || 'stat_date'
    setSqlTemplate(
      `WITH aux AS (\n  SELECT id, AVG(coeff) AS coeff\n  FROM ${tables[1] || anchor}\n  GROUP BY id\n)\nSELECT\n  {{select_dims}}\n  ROUND(SUM(s.${columns[1]?.name || 'amount'}) * 1.0, 2) AS ${metricAlias}\nFROM ${anchor} s\nWHERE 1=1 {{and_where}}\n{{group_by}}\n{{order_by}}`,
    )
    if (!dateColumn) setDateColumn(`s.${sampleDate}`)
  }

  const buildPayload = (): SemanticMetricInput => ({
    key: key.trim(),
    name: name.trim(),
    unit: unit.trim(),
    aliases: splitAliases(aliasesText),
    description: description.trim(),
    source_id: sourceId,
    calc_mode: calcMode,
    table_name: tableName.trim(),
    sql_expression: isTemplate ? sqlExpression.trim() : sqlExpression.trim(),
    sql_template: isTemplate ? sqlTemplate.trim() : '',
    date_column: dateColumn.trim(),
    filter_sql: filterSql.trim(),
    default_time_grain: defaultTimeGrain,
    dimensions: dimensions
      .filter((d) => d.column.trim())
      .map((d) => ({
        key: (d.key || d.column.split('.').pop() || d.column).trim(),
        label: (d.label || d.key || d.column).trim(),
        column: d.column.trim(),
        entity_key: (d.entity_key || '').trim(),
      })),
    ...(metric ? { expected_version: metric.version } : {}),
  })

  const canRunOrSave = Boolean(
    sourceId && (isTemplate ? sqlTemplate.trim() : (tableName.trim() && sqlExpression.trim())),
  )

  const runPreview = async () => {
    setPreviewing(true)
    try {
      const payload = buildPayload()
      if (!payload.key) payload.key = 'draft_metric'
      if (!payload.name) payload.name = '草稿试算指标'
      const res = await api.semanticMetrics.preview({
        definition: payload as unknown as Record<string, unknown>,
        limit: 20,
      })
      setPreview(res)
      toast.ok('在线试算成功')
    } catch (e) {
      toast.error(e)
    } finally {
      setPreviewing(false)
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      const payload = buildPayload()
      if (metric) {
        await api.semanticMetrics.update(metric.id, payload)
        toast.ok(`已更新指标「${payload.name}」`)
      } else {
        await api.semanticMetrics.create(payload)
        toast.ok(`已创建并启用指标「${payload.name}」`)
      }
      onSaved()
      onClose()
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={760}
      title={metric ? `编辑语义指标 · ${metric.name}` : '新建语义指标卡片'}
      footer={
        <div className="flex w-full items-center justify-between gap-2">
          <button type="button" className="btn btn-sm" disabled={previewing || !canRunOrSave}
                  onClick={() => void runPreview()} data-metric-editor-preview="">
            {previewing ? <Spinner size={12} /> : <Play size={12} aria-hidden />} 在线试算预览
          </button>
          <div className="flex items-center gap-2">
            <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>取消</button>
            <button type="button" className="btn btn-sm btn-primary"
                    disabled={saving || !name.trim() || !key.trim() || !canRunOrSave}
                    onClick={() => void save()} data-metric-editor-save="">
              {saving ? <Spinner size={12} /> : <Check size={12} aria-hidden />} 保存并启用
            </button>
          </div>
        </div>
      }
    >
      <div className="max-h-[72vh] space-y-3 overflow-y-auto pr-1">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-elev px-3 py-2">
          <span className="text-xs font-medium text-dim">指标计算模式</span>
          <div role="group" aria-label="计算模式" className="inline-flex rounded-md border bg-panel p-0.5 text-xs">
            <button
              type="button"
              aria-pressed={!isTemplate}
              data-calc-mode-expression=""
              className={clsx(
                'rounded px-2.5 py-1 transition-colors',
                !isTemplate ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
              )}
              onClick={() => setCalcMode('expression')}
            >
              单表聚合表达式
            </button>
            <button
              type="button"
              aria-pressed={isTemplate}
              data-calc-mode-template=""
              className={clsx(
                'inline-flex items-center gap-1 rounded px-2.5 py-1 transition-colors',
                isTemplate ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
              )}
              onClick={() => setCalcMode('sql_template')}
            >
              <Code2 size={12} aria-hidden /> 多表 / 参数化 SQL 模板
            </button>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="指标中文名称" required>
            <input className="input w-full" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="例如：设备综合效率 OEE（跨表精算）" />
          </Field>
          <Field label="唯一英文标识" required hint="小写字母、数字与下划线，供问数工具调用">
            <input className="input mono w-full" value={key} onChange={(e) => setKey(e.target.value)}
                   placeholder="例如：oee_multi_table" />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="所属数据源" required>
            <select className="input w-full" value={sourceId} onChange={(e) => { setSourceId(e.target.value); setTableName('') }}>
              <option value="">请选择数据源</option>
              {sources.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </Field>
          <Field label={isTemplate ? '主事实表（可选锚点）' : '物理表名'} required={!isTemplate}
                 hint={isTemplate ? '留空时自动从模板首张物理表识别' : undefined}>
            {tables.length > 0 ? (
              <select className="input mono w-full" value={tableName} onChange={(e) => setTableName(e.target.value)}>
                <option value="">{isTemplate ? '（自动识别主物理表）' : '请选择物理表'}</option>
                {tables.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            ) : (
              <input className="input mono w-full" value={tableName} onChange={(e) => setTableName(e.target.value)}
                     placeholder="物理表名" />
            )}
          </Field>
          <Field label="单位">
            <input className="input w-full" value={unit} onChange={(e) => setUnit(e.target.value)}
                   placeholder="例如：% / 秒 / 人次 / 元" />
          </Field>
        </div>

        {catalogHint && (
          <div className="rounded border bg-elev px-2.5 py-1.5 text-2xs text-dim">
            数据目录提示：{catalogHint}
          </div>
        )}

        {!isTemplate ? (
          <Field label="聚合 SQL 表达式" required hint="单表聚合表达式（禁止嵌套查询），点击下方列名可快速填入 SUM(列名)">
            <input className="input mono w-full" value={sqlExpression} onChange={(e) => setSqlExpression(e.target.value)}
                   placeholder="例如：SUM(paid_amount) 或 ROUND(SUM(good_qty) * 100.0 / NULLIF(SUM(total_qty), 0), 2)" />
            {columns.length > 0 && (
              <div className="mt-1.5 flex max-h-20 flex-wrap gap-1 overflow-y-auto">
                {columns.map((c) => (
                  <button key={c.name} type="button"
                          className="rounded border bg-panel px-1.5 py-0.5 text-[11px] text-dim hover:border-[var(--accent)] hover:text-fg"
                          title={`${c.name}（${c.type}）— 点击填入 SUM(${c.name})`}
                          onClick={() => setSqlExpression((prev) => (prev ? `${prev}` : `SUM(${c.name})`))}>
                    <span className="mono">{c.name}</span>
                    {c.label && <span className="ml-1 text-faint">{c.label}</span>}
                  </button>
                ))}
              </div>
            )}
          </Field>
        ) : (
          <Field
            label="多表 / 参数化只读 SQL 模板"
            required
            hint="支持跨表 JOIN、WITH CTE 预聚合、COALESCE 兜底系数与动态周期天数槽位；保存时自动执行双形态语法与表列安全校验"
          >
            <div className="mb-1.5 flex flex-wrap items-center justify-between gap-1.5">
              <div className="flex flex-wrap gap-1">
                {TEMPLATE_SLOT_CHIPS.map((slot) => (
                  <button
                    key={slot.token}
                    type="button"
                    title={slot.tip}
                    onClick={() => insertSlotToken(slot.token)}
                    className="mono rounded border bg-panel px-1.5 py-0.5 text-[11px] text-[var(--accent)] hover:border-[var(--accent)]"
                  >
                    {slot.token}
                  </button>
                ))}
              </div>
              <button type="button" className="btn btn-xs btn-ghost" onClick={fillTemplateSkeleton}>
                填入跨表模板骨架
              </button>
            </div>
            <textarea
              ref={tplRef}
              rows={8}
              data-metric-sql-template=""
              className="input mono w-full resize-y py-2 text-xs leading-relaxed"
              value={sqlTemplate}
              onChange={(e) => setSqlTemplate(e.target.value)}
              placeholder={'WITH cf AS (\n  SELECT prod_code, AVG(conv_factor) AS conv_factor FROM dim_carrier_factor GROUP BY prod_code\n)\nSELECT\n  {{select_dims}}\n  ROUND(SUM(s.good_qty * COALESCE(cf.conv_factor, 0.5)) * 100.0 / NULLIF(SUM(s.total_qty), 0), 2) AS oee_val\nFROM mes_shift_daily s\nLEFT JOIN cf ON cf.prod_code = s.prod_code\nWHERE 1=1 {{and_where}}\n{{group_by}}\n{{order_by}}'}
            />
          </Field>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="业务时间列" hint={isTemplate ? '多表请带主表别名前缀，如 s.stat_date' : '用于按日期筛选与趋势分组'}>
            {!isTemplate && columns.length > 0 ? (
              <select className="input mono w-full" value={dateColumn} onChange={(e) => setDateColumn(e.target.value)}>
                <option value="">（无时间列）</option>
                {columns.map((c) => (
                  <option key={c.name} value={c.name}>{c.name}{c.label ? `（${c.label}）` : ''}</option>
                ))}
              </select>
            ) : (
              <input className="input mono w-full" value={dateColumn} onChange={(e) => setDateColumn(e.target.value)}
                     placeholder={isTemplate ? '例如：s.stat_date' : '例如：stat_date'} />
            )}
          </Field>
          <Field label="默认有效过滤条件" hint="自动拼入 WHERE 子句">
            <input className="input mono w-full" value={filterSql} onChange={(e) => setFilterSql(e.target.value)}
                   placeholder={isTemplate ? '例如：s.is_deleted = 0' : '例如：status = 1 AND is_deleted = 0'} />
          </Field>
          <Field label="默认时间粒度">
            <select className="input w-full" value={defaultTimeGrain} onChange={(e) => setDefaultTimeGrain(e.target.value)}>
              <option value="day">按日（day）</option>
              <option value="month">按月（month）</option>
              <option value="hour">按小时（hour）</option>
            </select>
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="口语别名（顿号或逗号分隔）" hint="帮助问数快速直查毫秒级命中，如：OEE、综合效率">
            <input className="input w-full" value={aliasesText} onChange={(e) => setAliasesText(e.target.value)}
                   placeholder="例如：OEE、设备综合效率、跨表OEE" />
          </Field>
          <Field label="业务口径说明与判异提示" hint="统计范围、兜底系数、物理红线等说明">
            <input className="input w-full" value={description} onChange={(e) => setDescription(e.target.value)}
                   placeholder="例如：无模具系数时按 0.5 兜底；超过 200% 视为异常数据" />
          </Field>
        </div>

        <div className="space-y-1.5 rounded-md border bg-elev p-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-dim">
              可下钻与筛选维度（{isTemplate ? '多表模式请带主表别名前缀，如 s.line_code' : '可选绑定标准业务实体自动转码'}）
            </span>
            <button type="button" className="btn btn-xs"
                    onClick={() => setDimensions((d) => [
                      ...d,
                      { key: '', label: '', column: isTemplate ? '' : (columns[0]?.name ?? ''), entity_key: '' },
                    ])}>
              <Plus size={11} aria-hidden /> 添加维度
            </button>
          </div>
          {dimensions.length === 0 ? (
            <p className="text-2xs text-faint">未配置维度时，仍支持按时间区间汇总或按日、按月查看趋势。</p>
          ) : (
            <div className="space-y-1.5">
              {dimensions.map((dim, idx) => (
                <div key={idx} className="grid grid-cols-12 items-center gap-1.5">
                  <input className="input mono col-span-3 text-xs" placeholder="维度键（如 line_code）" value={dim.key}
                         onChange={(e) => setDimensions((ds) => ds.map((x, i) => (i === idx ? { ...x, key: e.target.value } : x)))} />
                  <input className="input col-span-3 text-xs" placeholder="中文名（如 产线）" value={dim.label}
                         onChange={(e) => setDimensions((ds) => ds.map((x, i) => (i === idx ? { ...x, label: e.target.value } : x)))} />
                  {!isTemplate && columns.length > 0 ? (
                    <select className="input mono col-span-3 text-xs" value={dim.column}
                            onChange={(e) => setDimensions((ds) => ds.map((x, i) => (i === idx ? { ...x, column: e.target.value } : x)))}>
                      <option value="">选择物理列</option>
                      {columns.map((c) => (
                        <option key={c.name} value={c.name}>{c.name}</option>
                      ))}
                    </select>
                  ) : (
                    <input className="input mono col-span-3 text-xs" placeholder={isTemplate ? 's.line_code' : '物理列名'} value={dim.column}
                           onChange={(e) => setDimensions((ds) => ds.map((x, i) => (i === idx ? { ...x, column: e.target.value } : x)))} />
                  )}
                  <select className="input col-span-2 text-xs" value={dim.entity_key ?? ''}
                          title="关联标准业务实体后，按名称筛选将自动转换为该数据源物理编码"
                          onChange={(e) => setDimensions((ds) => ds.map((x, i) => (i === idx ? { ...x, entity_key: e.target.value } : x)))}>
                    <option value="">无关联实体</option>
                    {entities.map((ent) => (
                      <option key={ent.id} value={ent.key}>{ent.label}（{ent.key}）</option>
                    ))}
                  </select>
                  <button type="button" className="btn btn-xs btn-ghost col-span-1 justify-center text-faint hover:text-[var(--err)]"
                          aria-label="移除维度"
                          onClick={() => setDimensions((ds) => ds.filter((_, i) => i !== idx))}>
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {preview && <PreviewResultPanel preview={preview} />}
      </div>
    </Modal>
  )
}

type DocInputMode = 'paste' | 'upload' | 'kb' | 'catalog_only'
type ReviewTab = 'metrics' | 'catalog' | 'entities' | 'guardrails'

export function MetricSuggestModal({
  open,
  initialJob,
  onClose,
  onSaved,
  onJobUpdated,
}: {
  open: boolean
  initialJob?: SemanticDecomposeJob | null
  onClose: () => void
  onSaved: () => void
  onJobUpdated?: (job: SemanticDecomposeJob | null) => void
}) {
  const { list: allSources } = useDatasources()
  const sources = useMemo(() => allSources.filter((s) => s.enabled !== false), [allSources])

  const [sourceId, setSourceId] = useState('')
  const [docMode, setDocMode] = useState<DocInputMode>('paste')
  const [docTitle, setDocTitle] = useState('')
  const [docText, setDocText] = useState('')
  const [uploadedFiles, setUploadedFiles] = useState<DecomposeAttachmentOut[]>([])
  const [uploading, setUploading] = useState(false)
  const [docs, setDocs] = useState<KbDocument[]>([])
  const [pickedDocs, setPickedDocs] = useState<string[]>([])
  const [scopes, setScopes] = useState<string[]>(['catalog', 'metrics', 'entities'])
  const [saveToKb, setSaveToKb] = useState(true)
  const [note, setNote] = useState('')

  const [job, setJob] = useState<SemanticDecomposeJob | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [applying, setApplying] = useState(false)
  const [reviewTab, setReviewTab] = useState<ReviewTab>('metrics')

  // 审阅勾选态
  const [selectedMetricKeys, setSelectedMetricKeys] = useState<Set<string>>(new Set())
  const [selectedCatalogIds, setSelectedCatalogIds] = useState<Set<string>>(new Set())
  const [selectedEntityIds, setSelectedEntityIds] = useState<Set<string>>(new Set())
  const [trialBusyKey, setTrialBusyKey] = useState<string | null>(null)
  const [trialResults, setTrialResults] = useState<Record<string, SemanticMetricPreviewOut>>({})

  const syncSelectionsFromJob = (nextJob: SemanticDecomposeJob) => {
    const res = nextJob.result
    if (!res) return
    const mKeys = new Set((res.metric_candidates ?? []).map((m) => m.key))
    const cIds = new Set(
      (res.catalog_patches ?? []).flatMap((t) => (t.changes ?? []).map((c) => c.id)),
    )
    const eIds = new Set((res.entity_changes ?? []).map((e) => e.id))
    setSelectedMetricKeys(mKeys)
    setSelectedCatalogIds(cIds)
    setSelectedEntityIds(eIds)
    if ((res.metric_candidates?.length ?? 0) > 0) setReviewTab('metrics')
    else if (cIds.size > 0) setReviewTab('catalog')
    else if (eIds.size > 0) setReviewTab('entities')
    else setReviewTab('guardrails')
  }

  useEffect(() => {
    if (!open) return
    setSourceId((prev) => prev || sources[0]?.id || '')
    api.kb.documents().then(setDocs, () => setDocs([]))
    if (initialJob) {
      setJob(initialJob)
      if (initialJob.status === 'succeeded' && initialJob.result) {
        syncSelectionsFromJob(initialJob)
      } else if (initialJob.id) {
        api.semanticMetrics.decomposeJobs.get(initialJob.id).then(
          ({ job: fresh }) => {
            setJob(fresh)
            if (fresh.status === 'succeeded') syncSelectionsFromJob(fresh)
          },
          () => {},
        )
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialJob?.id, sources])

  // 弹窗打开期间，若存在排队或执行中的后台任务，每 1.2s 拉取实时阶段进度
  useEffect(() => {
    if (!open || !job || (job.status !== 'queued' && job.status !== 'running')) return
    let alive = true
    const timer = window.setInterval(() => {
      api.semanticMetrics.decomposeJobs.get(job.id).then(
        ({ job: fresh }) => {
          if (!alive) return
          setJob(fresh)
          onJobUpdated?.(fresh)
          if (fresh.status === 'succeeded') {
            syncSelectionsFromJob(fresh)
            toast.ok('后台口径拆解已完成，请审阅并一键应用')
          } else if (fresh.status === 'failed') {
            toast.error(fresh.error || '口径拆解任务执行失败')
          }
        },
        () => {},
      )
    }, 1200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, job?.id, job?.status])

  const toggleScope = (s: string) => {
    setScopes((prev) => {
      const has = prev.includes(s)
      const next = has ? prev.filter((x) => x !== s) : [...prev, s]
      return next.length > 0 ? next : ['metrics']
    })
  }

  const handleFileUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploading(true)
    try {
      for (const file of Array.from(files)) {
        const out = await api.semanticMetrics.uploadAttachment(file, { save_to_kb: false })
        setUploadedFiles((prev) => [...prev, out])
      }
      toast.ok('文档附件解析完成')
    } catch (e) {
      toast.error(e)
    } finally {
      setUploading(false)
    }
  }

  const startJob = async () => {
    if (!sourceId) return
    setStarting(true)
    setTrialResults({})
    try {
      const effectiveScopes = docMode === 'catalog_only' ? ['metrics'] : scopes
      const { job: created } = await api.semanticMetrics.decomposeJobs.create({
        source_id: sourceId,
        scopes: effectiveScopes,
        document_ids: docMode === 'kb' ? pickedDocs : [],
        attachment_ids: docMode === 'upload' ? uploadedFiles.map((f) => f.attachment_id) : [],
        doc_title: docMode === 'paste' ? docTitle.trim() : '',
        doc_text: docMode === 'paste' ? docText.trim() : '',
        save_to_kb: docMode === 'paste' || docMode === 'upload' ? saveToKb : false,
        note: note.trim(),
      })
      setJob(created)
      onJobUpdated?.(created)
      toast.info('已转入服务端后台拆解，可随时关闭窗口切换页面')
    } catch (e) {
      toast.error(e)
    } finally {
      setStarting(false)
    }
  }

  const cancelCurrentJob = async () => {
    if (!job) return
    setCancelling(true)
    try {
      const { job: cancelled } = await api.semanticMetrics.decomposeJobs.cancel(job.id)
      setJob(cancelled)
      onJobUpdated?.(cancelled)
      toast.info('已取消后台拆解任务')
    } catch (e) {
      toast.error(e)
    } finally {
      setCancelling(false)
    }
  }

  const runCandidateTrial = async (cand: SemanticMetricInput) => {
    setTrialBusyKey(cand.key)
    try {
      const out = await api.semanticMetrics.preview({
        definition: { ...cand, source_id: job?.source_id || sourceId } as Record<string, unknown>,
        limit: 15,
      })
      setTrialResults((prev) => ({ ...prev, [cand.key]: out }))
    } catch (e) {
      toast.error(e)
    } finally {
      setTrialBusyKey(null)
    }
  }

  const applySelected = async () => {
    if (!job || !job.result) return
    setApplying(true)
    try {
      const res = job.result
      const chosenMetrics = (res.metric_candidates ?? []).filter((m) => selectedMetricKeys.has(m.key))
      const chosenCatalog = (res.catalog_patches ?? [])
        .map((tbl) => ({
          table: tbl.table,
          changes: (tbl.changes ?? [])
            .filter((c) => selectedCatalogIds.has(c.id))
            .map((c) => ({ path: c.path, value: c.value, reason: c.reason })),
        }))
        .filter((tbl) => tbl.changes.length > 0)
      const chosenEntities = (res.entity_changes ?? [])
        .filter((e) => selectedEntityIds.has(e.id))
        .map((e) => e.op_payload)

      const appliedOut = await api.semanticMetrics.decomposeJobs.apply(job.id, {
        catalog_patches: chosenCatalog,
        metrics: chosenMetrics,
        entity_operations: chosenEntities,
        overwrite_existing_metrics: true,
      })
      const parts: string[] = []
      const totalMet = appliedOut.applied.metrics_created_count + appliedOut.applied.metrics_updated_count
      if (totalMet > 0) parts.push(`${totalMet} 个语义指标`)
      if (appliedOut.applied.catalog_applied_count > 0) parts.push(`${appliedOut.applied.catalog_applied_count} 项字段目录补丁`)
      if (appliedOut.applied.entities_applied_count > 0) parts.push(`${appliedOut.applied.entities_applied_count} 项标准实体映射`)
      toast.ok(`一键应用完成：${parts.join('、') || '已保存审阅结果'}`)

      const { job: refreshed } = await api.semanticMetrics.decomposeJobs.get(job.id)
      setJob(refreshed)
      onJobUpdated?.(refreshed)
      onSaved()
      onClose()
    } catch (e) {
      toast.error(e)
    } finally {
      setApplying(false)
    }
  }

  const isRunning = job?.status === 'queued' || job?.status === 'running'
  const isReview = job?.status === 'succeeded' && Boolean(job.result)
  const res = job?.result
  const totalSelectedCount = selectedMetricKeys.size + selectedCatalogIds.size + selectedEntityIds.size

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={820}
      title="口径文档一键拆解与智能指标提取"
      footer={
        <div className="flex w-full items-center justify-between gap-2">
          {isRunning ? (
            <>
              <button type="button" className="btn btn-sm text-[var(--err)]" disabled={cancelling}
                      onClick={() => void cancelCurrentJob()} data-decompose-cancel="">
                {cancelling ? <Spinner size={12} /> : <X size={12} aria-hidden />} 取消后台任务
              </button>
              <button type="button" className="btn btn-sm btn-primary" onClick={onClose} data-decompose-minimize="">
                转入后台继续（可关闭窗口）
              </button>
            </>
          ) : isReview ? (
            <>
              <button type="button" className="btn btn-sm btn-ghost"
                      onClick={() => setJob(null)} data-decompose-reset="">
                <RotateCcw size={12} aria-hidden /> 拆解新文档
              </button>
              <div className="flex items-center gap-2">
                <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>关闭</button>
                <button type="button" className="btn btn-sm btn-primary"
                        disabled={applying || totalSelectedCount === 0}
                        onClick={() => void applySelected()} data-metric-suggest-save="">
                  {applying ? <Spinner size={12} /> : <Check size={12} aria-hidden />}
                  一键应用已选（{totalSelectedCount}）
                </button>
              </div>
            </>
          ) : (
            <>
              <span className="text-2xs text-faint">提交后在服务端后台执行，关闭窗口不影响进度</span>
              <div className="flex items-center gap-2">
                <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>取消</button>
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  disabled={
                    starting
                    || !sourceId
                    || (docMode === 'paste' && !docText.trim())
                    || (docMode === 'upload' && uploadedFiles.length === 0)
                  }
                  onClick={() => void startJob()}
                  data-metric-suggest-run=""
                >
                  {starting ? <Spinner size={12} /> : <Sparkles size={12} aria-hidden />}
                  启动后台一键拆解
                </button>
              </div>
            </>
          )}
        </div>
      }
    >
      <div className="max-h-[74vh] space-y-3 overflow-y-auto pr-1">
        {/* 视图 1：后台任务实时分步进度 */}
        {isRunning && job && (
          <div className="space-y-3 rounded-lg border bg-elev p-4" data-decompose-progress-view="">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Spinner size={15} />
                <span className="text-sm font-medium text-fg">{job.phase_label || '正在后台拆解口径文档…'}</span>
              </div>
              <span className="tnum mono text-xs font-semibold text-[var(--accent)]">{job.progress}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-panel">
              <div
                className="h-full rounded-full bg-[var(--accent)] transition-all duration-300"
                style={{ width: `${Math.max(4, Math.min(100, job.progress))}%` }}
              />
            </div>
            <div className="space-y-2 pt-1">
              {(job.steps ?? []).map((st) => (
                <div key={st.key} className="flex items-start justify-between gap-3 rounded-md border bg-panel px-3 py-2 text-xs">
                  <div className="flex items-start gap-2">
                    {st.status === 'succeeded' ? (
                      <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-[var(--ok)]" aria-hidden />
                    ) : st.status === 'running' ? (
                      <span className="mt-0.5 shrink-0"><Spinner size={13} /></span>
                    ) : (
                      <Clock size={14} className="mt-0.5 shrink-0 text-faint" aria-hidden />
                    )}
                    <div>
                      <div className={clsx('font-medium', st.status === 'pending' ? 'text-dim' : 'text-fg')}>
                        {st.label}
                      </div>
                      <div className="text-2xs text-faint">{st.detail}</div>
                    </div>
                  </div>
                  {st.elapsed_ms > 0 && (
                    <span className="tnum shrink-0 text-2xs text-faint">
                      {(st.elapsed_ms / 1000).toFixed(1)} s
                    </span>
                  )}
                </div>
              ))}
            </div>
            <Notice tone="info">
              任务正在服务端后台独立执行。您可以点击右下角「转入后台继续」先去处理其他工作，口径页顶部会实时展示进度并在完成后提醒审阅。
            </Notice>
          </div>
        )}

        {/* 视图 2：已完成任务的四层审阅与一键应用 */}
        {isReview && job && res && (
          <div className="space-y-3" data-decompose-review-view="">
            {res.applied && (
              <Notice tone="info">
                该拆解结果已于 {res.applied.applied_at.slice(0, 19).replace('T', ' ')} 应用入库；您仍可调整勾选并再次应用更新。
              </Notice>
            )}

            <div role="tablist" aria-label="拆解产物分层审阅" className="flex flex-wrap gap-1 rounded-md border bg-elev p-1 text-xs">
              <button
                type="button"
                role="tab"
                aria-selected={reviewTab === 'metrics'}
                className={clsx(
                  'rounded px-2.5 py-1.5 transition-colors',
                  reviewTab === 'metrics' ? 'bg-panel font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
                )}
                onClick={() => setReviewTab('metrics')}
              >
                语义指标口径（{selectedMetricKeys.size}/{(res.metric_candidates ?? []).length}）
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={reviewTab === 'catalog'}
                className={clsx(
                  'rounded px-2.5 py-1.5 transition-colors',
                  reviewTab === 'catalog' ? 'bg-panel font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
                )}
                onClick={() => setReviewTab('catalog')}
              >
                字段目录补丁（{selectedCatalogIds.size}/{(res.catalog_patches ?? []).reduce((n, t) => n + t.changes.length, 0)}）
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={reviewTab === 'entities'}
                className={clsx(
                  'rounded px-2.5 py-1.5 transition-colors',
                  reviewTab === 'entities' ? 'bg-panel font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
                )}
                onClick={() => setReviewTab('entities')}
              >
                标准业务实体（{selectedEntityIds.size}/{(res.entity_changes ?? []).length}）
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={reviewTab === 'guardrails'}
                className={clsx(
                  'rounded px-2.5 py-1.5 transition-colors',
                  reviewTab === 'guardrails' ? 'bg-panel font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
                )}
                onClick={() => setReviewTab('guardrails')}
              >
                判异红线与知识归档（{(res.guardrail_notes ?? []).length}）
              </button>
            </div>

            {reviewTab === 'metrics' && (
              <div className="space-y-2">
                {(res.metric_candidates ?? []).length === 0 ? (
                  <div className="rounded border bg-panel p-4 text-center text-xs text-faint">
                    本次未提取到新的候选指标口径。
                  </div>
                ) : (
                  (res.metric_candidates ?? []).map((c) => {
                    const checked = selectedMetricKeys.has(c.key)
                    const isTpl = c.calc_mode === 'sql_template'
                    const trial = trialResults[c.key]
                    return (
                      <div
                        key={c.key}
                        className={clsx(
                          'rounded-lg border p-2.5 text-xs transition-colors',
                          checked ? 'border-[var(--accent)] bg-accent-soft/30' : 'bg-panel',
                        )}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5">
                            <input
                              type="checkbox"
                              className="mt-0.5"
                              checked={checked}
                              onChange={() => setSelectedMetricKeys((prev) => {
                                const next = new Set(prev)
                                if (checked) next.delete(c.key)
                                else next.add(c.key)
                                return next
                              })}
                            />
                            <div className="min-w-0 flex-1 space-y-1">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="font-medium text-fg">{c.name}</span>
                                <span className="mono text-2xs text-faint">{c.key}</span>
                                {c.unit && <span className="rounded border px-1 py-0.5 text-[10px] text-dim">{c.unit}</span>}
                                <span className={clsx(
                                  'rounded border px-1.5 py-0.5 text-[10px]',
                                  isTpl ? 'border-[var(--accent)]/50 text-[var(--accent)]' : 'text-dim',
                                )}>
                                  {isTpl ? '多表 SQL 模板' : '单表聚合'}
                                </span>
                                {c.exists_in_db && (
                                  <span className="rounded border px-1.5 py-0.5 text-[10px] text-[var(--warn)]">覆盖已有同名指标</span>
                                )}
                              </div>
                              {!isTpl ? (
                                <div className="mono text-2xs text-[var(--accent)]">
                                  {c.table_name} · {c.sql_expression}
                                  {c.date_column ? ` · 时间列 ${c.date_column}` : ''}
                                </div>
                              ) : (
                                <pre className="mono max-h-28 overflow-auto rounded border bg-bg p-2 text-[11px] leading-relaxed text-dim">
                                  {c.sql_template}
                                </pre>
                              )}
                              {c.description && <p className="text-2xs text-dim">{c.description}</p>}
                            </div>
                          </label>
                          <button
                            type="button"
                            className="btn btn-xs shrink-0"
                            disabled={trialBusyKey === c.key}
                            onClick={() => void runCandidateTrial(c)}
                          >
                            {trialBusyKey === c.key ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 试算
                          </button>
                        </div>
                        {trial && <PreviewResultPanel preview={trial} />}
                      </div>
                    )
                  })
                )}
                {(res.metric_skipped?.length ?? 0) > 0 && (
                  <details className="rounded border bg-elev p-2 text-2xs text-faint">
                    <summary className="cursor-pointer">
                      已自动拦截 {res.metric_skipped?.length} 条未通过表列或语法核验的候选项（点击查看原因）
                    </summary>
                    <ul className="mt-1.5 space-y-1 pl-3">
                      {(res.metric_skipped ?? []).map((s, idx) => (
                        <li key={idx}><span className="mono">{s.key}</span>：{s.reason}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}

            {reviewTab === 'catalog' && (
              <div className="space-y-2.5">
                {(res.catalog_patches ?? []).length === 0 ? (
                  <div className="rounded border bg-panel p-4 text-center text-xs text-faint">
                    没有需要新增或更新的字段目录补丁。
                  </div>
                ) : (
                  (res.catalog_patches ?? []).map((tbl) => (
                    <div key={tbl.table} className="space-y-1.5 rounded-lg border bg-panel p-2.5">
                      <div className="flex items-center gap-1.5 text-xs font-semibold text-fg">
                        <Database size={12} className="text-[var(--accent)]" aria-hidden />
                        <span className="mono">{tbl.table}</span>
                        <span className="text-2xs font-normal text-faint">（共 {tbl.changes.length} 项）</span>
                      </div>
                      <div className="space-y-1">
                        {tbl.changes.map((ch) => {
                          const checked = selectedCatalogIds.has(ch.id)
                          return (
                            <label key={ch.id} className="flex cursor-pointer items-start gap-2 rounded border border-hairline bg-elev px-2.5 py-1.5 text-xs">
                              <input
                                type="checkbox"
                                className="mt-0.5"
                                checked={checked}
                                onChange={() => setSelectedCatalogIds((prev) => {
                                  const next = new Set(prev)
                                  if (checked) next.delete(ch.id)
                                  else next.add(ch.id)
                                  return next
                                })}
                              />
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-baseline gap-2">
                                  <span className="font-medium text-fg">{ch.slot_label}</span>
                                  <span className="mono text-2xs text-faint">{ch.path}</span>
                                </div>
                                <div className="mt-0.5 text-2xs">
                                  <span className="text-faint">{formatCatalogValue(ch.before)}</span>
                                  <span className="mx-1.5 text-dim">→</span>
                                  <span className="font-medium text-[var(--accent)]">{formatCatalogValue(ch.after)}</span>
                                </div>
                                {ch.reason && <div className="mt-0.5 text-2xs text-faint">依据：{ch.reason}</div>}
                              </div>
                            </label>
                          )
                        })}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}

            {reviewTab === 'entities' && (
              <div className="space-y-2">
                {(res.entity_changes ?? []).length === 0 ? (
                  <div className="rounded border bg-panel p-4 text-center text-xs text-faint">
                    本次未识别到新增的标准业务实体或物理列绑定。
                  </div>
                ) : (
                  (res.entity_changes ?? []).map((ec) => {
                    const checked = selectedEntityIds.has(ec.id)
                    return (
                      <label key={ec.id} className="flex cursor-pointer items-start gap-2.5 rounded-lg border bg-panel p-2.5 text-xs">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={checked}
                          onChange={() => setSelectedEntityIds((prev) => {
                            const next = new Set(prev)
                            if (checked) next.delete(ec.id)
                            else next.add(ec.id)
                            return next
                          })}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-fg">{ec.summary}</span>
                            <span className="mono text-2xs text-faint">{ec.entity_key}</span>
                          </div>
                          {ec.reason && <div className="mt-0.5 text-2xs text-dim">{ec.reason}</div>}
                        </div>
                      </label>
                    )
                  })
                )}
              </div>
            )}

            {reviewTab === 'guardrails' && (
              <div className="space-y-2.5">
                {(res.kb_documents ?? []).length > 0 && (
                  <div className="rounded-lg border bg-panel p-2.5 text-xs">
                    <div className="font-medium text-fg">知识库归档状态</div>
                    <div className="mt-1 space-y-1 text-2xs text-dim">
                      {(res.kb_documents ?? []).map((d) => (
                        <div key={d.id} className="flex items-center gap-1.5">
                          <FileText size={12} className="text-[var(--accent)]" aria-hidden />
                          <span>《{d.title}》</span>
                          <span className="text-faint">
                            {d.created_now ? '— 已自动归档至知识库并建立向量索引' : '— 已关联既有知识库文档'}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {(res.guardrail_notes ?? []).length === 0 ? (
                  <div className="rounded border bg-panel p-4 text-center text-xs text-faint">
                    文档中未检出额外的判异红线或缺源警示。
                  </div>
                ) : (
                  (res.guardrail_notes ?? []).map((g, idx) => (
                    <div key={idx} className="flex items-start gap-2.5 rounded-lg border bg-panel p-2.5 text-xs">
                      <ShieldAlert size={14} className="mt-0.5 shrink-0 text-[var(--warn)]" aria-hidden />
                      <div className="space-y-0.5">
                        <div className="font-medium text-fg">{g.title}</div>
                        <div className="text-2xs leading-relaxed text-dim">{g.rule}</div>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        )}

        {/* 视图 3：新建拆解任务配置表单 */}
        {!isRunning && !isReview && (
          <>
            {job?.status === 'failed' && job.error && (
              <Notice tone="err">{job.error}</Notice>
            )}
            <p className="text-xs leading-relaxed text-dim">
              喂入一份业务口径文档（如《指标公式总表》），服务端将在后台分阶段自动拆解：先识别盲列符号（如 EXT_VAL_01）与存储陷阱生成「字段目录补丁」，再联动生成「单表聚合与多表参数化 SQL 模板指标」及「标准业务实体绑定」，全程支持后台执行与一键应用。
            </p>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="目标数据源" required>
                <select className="input w-full" value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
                  <option value="">请选择数据源</option>
                  {sources.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              </Field>
              <Field label="补充拆解要求（可选）">
                <input className="input w-full" value={note} onChange={(e) => setNote(e.target.value)}
                       placeholder="例如：重点拆解 OEE 与 TEEP 跨表精算公式及产线维度" />
              </Field>
            </div>

            <div className="space-y-2 rounded-md border bg-elev p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-medium text-dim">文档输入方式</span>
                <div role="group" aria-label="文档输入方式" className="inline-flex flex-wrap rounded-md border bg-panel p-0.5 text-xs">
                  {([
                    ['paste', '粘贴 Markdown 文档'],
                    ['upload', '上传文档或表格'],
                    ['kb', '选择知识库文档'],
                    ['catalog_only', '仅按数据目录提取'],
                  ] as [DocInputMode, string][]).map(([mKey, mLabel]) => (
                    <button
                      key={mKey}
                      type="button"
                      aria-pressed={docMode === mKey}
                      className={clsx(
                        'rounded px-2.5 py-1 transition-colors',
                        docMode === mKey ? 'bg-accent-soft font-medium text-[var(--accent)]' : 'text-dim hover:text-fg',
                      )}
                      onClick={() => setDocMode(mKey)}
                    >
                      {mLabel}
                    </button>
                  ))}
                </div>
              </div>

              {docMode === 'paste' && (
                <div className="space-y-2 pt-1">
                  <input
                    className="input w-full text-xs"
                    value={docTitle}
                    onChange={(e) => setDocTitle(e.target.value)}
                    placeholder="文档标题（可选，留空时自动取首行标题，如：TEEP / OEE 及相关指标公式总表）"
                  />
                  <textarea
                    rows={7}
                    data-decompose-paste-input=""
                    className="input mono w-full resize-y py-2 text-xs leading-relaxed"
                    value={docText}
                    onChange={(e) => setDocText(e.target.value)}
                    placeholder="在此直接粘贴业务指标口径文档（支持 Markdown 表格、字段符号对照、跨表公式与判异红线说明）…"
                  />
                </div>
              )}

              {docMode === 'upload' && (
                <div className="space-y-2 pt-1">
                  <label className="flex cursor-pointer flex-col items-center justify-center gap-1 rounded-md border border-dashed bg-panel px-4 py-4 text-xs text-dim hover:border-[var(--accent)] hover:text-fg">
                    {uploading ? <Spinner size={16} /> : <Upload size={16} aria-hidden />}
                    <span>点击选择业务口径文档或主数据对照表（支持 .md / .txt / .pdf / .docx / .xlsx / .csv）</span>
                    <input
                      type="file"
                      multiple
                      accept=".md,.txt,.pdf,.docx,.pptx,.html,.xlsx,.csv,.tsv"
                      className="hidden"
                      onChange={(e) => void handleFileUpload(e.target.files)}
                    />
                  </label>
                  {uploadedFiles.length > 0 && (
                    <div className="space-y-1">
                      {uploadedFiles.map((f) => (
                        <div key={f.attachment_id} className="flex items-center justify-between rounded border bg-panel px-2.5 py-1 text-xs">
                          <span className="truncate">{f.title}（{f.char_count} 字）</span>
                          <button
                            type="button"
                            className="text-faint hover:text-[var(--err)]"
                            onClick={() => setUploadedFiles((prev) => prev.filter((x) => x.attachment_id !== f.attachment_id))}
                          >
                            <X size={12} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {docMode === 'kb' && (
                <div className="max-h-36 space-y-1 overflow-y-auto rounded border bg-panel p-2">
                  {docs.length === 0 ? (
                    <div className="py-2 text-center text-2xs text-faint">知识库暂无文档，可切换至「粘贴 Markdown 文档」或「上传文档」。</div>
                  ) : (
                    docs.map((doc) => {
                      const checked = pickedDocs.includes(doc.id)
                      return (
                        <label key={doc.id} className="flex cursor-pointer items-center gap-2 text-xs hover:text-fg">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => setPickedDocs((prev) => (checked ? prev.filter((x) => x !== doc.id) : [...prev, doc.id]))}
                          />
                          <FileText size={12} className="shrink-0 text-faint" aria-hidden />
                          <span className="truncate">{doc.title || doc.source || doc.id}</span>
                        </label>
                      )
                    })
                  )}
                </div>
              )}

              {docMode === 'catalog_only' && (
                <p className="pt-1 text-2xs text-faint">
                  不挂载外部文档，直接依据该数据源已探查的物理表结构、数据目录与标准实体绑定提取核心语义指标卡片。
                </p>
              )}
            </div>

            {docMode !== 'catalog_only' && (
              <div className="space-y-2 rounded-md border bg-elev p-3 text-xs">
                <div className="font-medium text-dim">一键协同拆解范围</div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <label className="flex cursor-pointer items-start gap-2 rounded border bg-panel p-2">
                    <input type="checkbox" className="mt-0.5" checked={scopes.includes('catalog')} onChange={() => toggleScope('catalog')} />
                    <div>
                      <div className="font-medium text-fg">1. 字段目录与盲列符号补丁</div>
                      <div className="text-2xs text-faint">提取列中文名、单位、流量或存量角色与存储陷阱，并自动供给下一步指标生成</div>
                    </div>
                  </label>
                  <label className="flex cursor-pointer items-start gap-2 rounded border bg-panel p-2">
                    <input type="checkbox" className="mt-0.5" checked={scopes.includes('metrics')} onChange={() => toggleScope('metrics')} />
                    <div>
                      <div className="font-medium text-fg">2. 单表与多表 SQL 模板指标口径</div>
                      <div className="text-2xs text-faint">生成通过表列与只读守卫预编译校验的指标卡片，支持跨表 JOIN 与周期参数</div>
                    </div>
                  </label>
                  <label className="flex cursor-pointer items-start gap-2 rounded border bg-panel p-2">
                    <input type="checkbox" className="mt-0.5" checked={scopes.includes('entities')} onChange={() => toggleScope('entities')} />
                    <div>
                      <div className="font-medium text-fg">3. 标准业务实体与编码映射</div>
                      <div className="text-2xs text-faint">识别产线、车间、区域等共用维度实体及物理列绑定</div>
                    </div>
                  </label>
                  {(docMode === 'paste' || docMode === 'upload') && (
                    <label className="flex cursor-pointer items-start gap-2 rounded border bg-panel p-2">
                      <input type="checkbox" className="mt-0.5" checked={saveToKb} onChange={(e) => setSaveToKb(e.target.checked)} />
                      <div>
                        <div className="font-medium text-fg">4. 同步归档至知识库</div>
                        <div className="text-2xs text-faint">保留文档中的判异阈值、物理红线与缺源说明，供问数分析时检索引用</div>
                      </div>
                    </label>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

export function DecomposeJobBanner({
  job,
  onOpenModal,
  onCancel,
  onDismiss,
}: {
  job: SemanticDecomposeJob
  onOpenModal: (job: SemanticDecomposeJob) => void
  onCancel: (jobId: string) => void
  onDismiss: () => void
}) {
  const running = job.status === 'queued' || job.status === 'running'
  const succeeded = job.status === 'succeeded'
  const failed = job.status === 'failed'
  const applied = Boolean(job.result?.applied)

  if (!running && !(succeeded && !applied) && !failed) return null

  return (
    <div
      data-decompose-banner={job.status}
      className={clsx(
        'flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5 text-xs',
        running ? 'border-[var(--accent)]/50 bg-accent-soft/40' : '',
        succeeded ? 'border-[var(--ok)]/50 bg-panel' : '',
        failed ? 'border-[var(--err)]/50 bg-panel' : '',
      )}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {running ? (
          <Spinner size={14} />
        ) : succeeded ? (
          <Layers size={15} className="shrink-0 text-[var(--ok)]" aria-hidden />
        ) : (
          <AlertTriangle size={15} className="shrink-0 text-[var(--err)]" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-fg">
              {running
                ? `后台正在拆解「${job.source_name || job.source_id}」：${job.phase_label}`
                : succeeded
                  ? `「${job.source_name || job.source_id}」口径文档拆解已完成：${job.phase_label}`
                  : `「${job.source_name || job.source_id}」口径拆解未完成：${job.error || job.phase_label}`}
            </span>
            {running && (
              <span className="tnum mono rounded border bg-panel px-1.5 py-0.5 text-[11px] font-semibold text-[var(--accent)]">
                {job.progress}%
              </span>
            )}
          </div>
          {running && (
            <div className="mt-1.5 h-1.5 max-w-md overflow-hidden rounded-full bg-panel">
              <div
                className="h-full rounded-full bg-[var(--accent)] transition-all duration-300"
                style={{ width: `${Math.max(4, Math.min(100, job.progress))}%` }}
              />
            </div>
          )}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        {running ? (
          <>
            <button type="button" className="btn btn-xs" onClick={() => onOpenModal(job)}>
              查看实时进度
            </button>
            <button type="button" className="btn btn-xs btn-ghost text-[var(--err)]" onClick={() => onCancel(job.id)}>
              取消
            </button>
          </>
        ) : succeeded ? (
          <>
            <button type="button" className="btn btn-xs btn-primary" onClick={() => onOpenModal(job)} data-decompose-open-review="">
              审阅并一键应用
            </button>
            <button type="button" className="btn btn-xs btn-ghost" onClick={onDismiss} aria-label="收起提示">
              <X size={12} />
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-xs" onClick={() => onOpenModal(job)}>
              重新发起
            </button>
            <button type="button" className="btn btn-xs btn-ghost" onClick={onDismiss} aria-label="收起提示">
              <X size={12} />
            </button>
          </>
        )}
      </div>
    </div>
  )
}

export function SemanticMetricCardsSection({
  metrics,
  entities,
  query,
  onReload,
  onEdit,
}: {
  metrics: SemanticMetric[]
  entities: CanonicalEntity[]
  query: string
  onReload: () => void
  onEdit: (m: SemanticMetric) => void
}) {
  const builder = useAtLeast('builder')
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [groupByDate, setGroupByDate] = useState(false)
  const [pickedDims, setPickedDims] = useState<string[]>([])
  const [busyId, setBusyId] = useState<string | null>(null)
  const [previews, setPreviews] = useState<Record<string, SemanticMetricPreviewOut>>({})

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return metrics
    return metrics.filter((m) => [
      m.name, m.key, m.table_name, m.source_name, m.description, ...(m.aliases ?? []),
    ].some((s) => String(s ?? '').toLowerCase().includes(q)))
  }, [metrics, query])

  if (!shown.length) return null

  const toggleExpand = (m: SemanticMetric) => {
    if (openKey === m.id) {
      setOpenKey(null)
      return
    }
    setOpenKey(m.id)
    setPickedDims([])
    setGroupByDate(false)
  }

  const runInlinePreview = async (m: SemanticMetric) => {
    setBusyId(m.id)
    try {
      const out = await api.semanticMetrics.preview({
        metric_id: m.id,
        date_from: dateFrom.trim() || undefined,
        date_to: dateTo.trim() || undefined,
        group_by_date: groupByDate,
        group_by: pickedDims,
        limit: 30,
      })
      setPreviews((p) => ({ ...p, [m.id]: out }))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusyId(null)
    }
  }

  const toggleStatus = async (m: SemanticMetric) => {
    const next = m.status === 'enabled' ? 'disabled' : 'enabled'
    try {
      await api.semanticMetrics.update(m.id, { status: next, expected_version: m.version })
      toast.ok(next === 'enabled' ? `已启用指标「${m.name}」` : `已停用指标「${m.name}」`)
      onReload()
    } catch (e) {
      toast.error(e)
    }
  }

  const handleDelete = async (m: SemanticMetric) => {
    const ok = await confirmDialog({
      title: `删除语义指标「${m.name}」？`,
      body: '删除后，问数快速直查与指标查询工具将不再自动匹配该指标卡片。',
      confirmLabel: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await api.semanticMetrics.remove(m.id)
      toast.ok(`已删除指标「${m.name}」`)
      onReload()
    } catch (e) {
      toast.error(e)
    }
  }

  const entityMap = new Map(entities.map((e) => [e.key, e.label]))

  return (
    <div className="space-y-2" data-semantic-metrics-section="">
      <div className="flex items-center justify-between px-0.5 text-xs font-semibold text-dim">
        <span className="inline-flex items-center gap-1.5">
          <Zap size={13} className="text-[var(--accent)]" aria-hidden />
          独立语义指标卡片（即存即生效 · 共 {shown.length} 项）
        </span>
        <span className="text-2xs font-normal text-faint">支持单表聚合与多表参数化 SQL 模板，自动接入问数快速直查</span>
      </div>
      {shown.map((m) => {
        const expanded = openKey === m.id
        const preview = previews[m.id]
        const enabled = m.status === 'enabled'
        const isTemplate = m.calc_mode === 'sql_template'
        return (
          <section key={m.id} className="overflow-hidden rounded-lg border bg-panel" data-semantic-metric={m.key}>
            <div className="flex items-start gap-3 px-3 py-2.5 hover:bg-hover">
              <button type="button" className="min-w-0 flex-1 text-left" onClick={() => toggleExpand(m)} aria-expanded={expanded}>
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="text-sm font-medium text-fg">{m.name}</span>
                  <span className="mono text-2xs text-faint">{m.key}</span>
                  {m.unit && <span className="rounded border px-1.5 py-0.5 text-[10px] text-dim">{m.unit}</span>}
                  {isTemplate && (
                    <span className="inline-flex items-center gap-0.5 rounded border border-[var(--accent)]/40 px-1.5 py-0.5 text-[10px] text-[var(--accent)]">
                      <Code2 size={10} aria-hidden /> 多表模板
                    </span>
                  )}
                  <span className={clsx(
                    'rounded border px-1.5 py-0.5 text-[10px]',
                    enabled ? 'border-[var(--accent)]/40 bg-accent-soft text-[var(--accent)]' : 'text-faint',
                  )}>
                    {enabled ? '即存即用' : '已停用'}
                  </span>
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
                  <span className="inline-flex items-center gap-1">
                    <Database size={10} aria-hidden />
                    {m.source_name || m.source_id}.{m.table_name}
                  </span>
                  <span className="mono text-dim">· {m.sql_expression}</span>
                  {m.date_column && <span>· 时间列 {m.date_column}</span>}
                  {m.aliases?.length > 0 && <span>· 别名：{m.aliases.join('、')}</span>}
                </div>
              </button>
              <div className="flex shrink-0 items-center gap-1">
                {builder && (
                  <>
                    <button type="button" className="btn btn-xs btn-ghost"
                            title={enabled ? '停用该指标' : '启用该指标'}
                            onClick={() => void toggleStatus(m)}>
                      <Power size={11} aria-hidden /> {enabled ? '停用' : '启用'}
                    </button>
                    <button type="button" className="btn btn-xs btn-ghost" onClick={() => onEdit(m)}>
                      <Edit3 size={11} aria-hidden /> 编辑
                    </button>
                    <button type="button" className="btn btn-xs btn-ghost text-faint hover:text-[var(--err)]"
                            onClick={() => void handleDelete(m)} aria-label={`删除 ${m.name}`}>
                      <Trash2 size={11} aria-hidden />
                    </button>
                  </>
                )}
                <button type="button" className="btn btn-xs btn-ghost" onClick={() => toggleExpand(m)} aria-label="展开试算">
                  <ChevronDown size={14} className={clsx('transition-transform', expanded && 'rotate-180')} aria-hidden />
                </button>
              </div>
            </div>

            {expanded && (
              <div className="space-y-2.5 border-t px-3 py-3 text-xs">
                {m.description && <p className="text-2xs leading-relaxed text-dim">{m.description}</p>}
                {isTemplate && m.sql_template && (
                  <div className="space-y-1">
                    <div className="flex items-center justify-between text-2xs text-faint">
                      <span>参数化 SQL 模板定义</span>
                      <CopyButton text={m.sql_template} />
                    </div>
                    <pre className="mono max-h-36 overflow-auto rounded border bg-bg p-2 text-[11px] leading-relaxed text-dim">
                      {m.sql_template}
                    </pre>
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-1.5 text-2xs">
                  {m.filter_sql && (
                    <span className="mono rounded border bg-elev px-2 py-0.5 text-dim">默认过滤：{m.filter_sql}</span>
                  )}
                  {(m.dimensions ?? []).map((d) => (
                    <span key={d.key || d.column} className="inline-flex items-center gap-1 rounded border bg-elev px-2 py-0.5 text-dim">
                      <Tag size={10} aria-hidden />
                      维度：{d.label || d.key}（<span className="mono">{d.column}</span>
                      {d.entity_key ? ` → 实体 ${entityMap.get(d.entity_key) ?? d.entity_key}` : ''}）
                    </span>
                  ))}
                </div>

                <div className="flex flex-wrap items-end gap-2 rounded-md border bg-elev p-2.5">
                  <div>
                    <label className="mb-0.5 block text-[11px] text-faint">起始日期（可选）</label>
                    <input type="date" className="input text-xs" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
                  </div>
                  <div>
                    <label className="mb-0.5 block text-[11px] text-faint">结束日期（可选）</label>
                    <input type="date" className="input text-xs" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
                  </div>
                  {m.date_column && (
                    <label className="flex cursor-pointer items-center gap-1.5 pb-1.5 text-xs text-dim">
                      <input type="checkbox" checked={groupByDate} onChange={(e) => setGroupByDate(e.target.checked)} />
                      按日期趋势分组
                    </label>
                  )}
                  {(m.dimensions ?? []).map((d) => {
                    const dk = d.key || d.column
                    const on = pickedDims.includes(dk)
                    return (
                      <label key={dk} className="flex cursor-pointer items-center gap-1.5 pb-1.5 text-xs text-dim">
                        <input type="checkbox" checked={on}
                               onChange={() => setPickedDims((prev) => (on ? prev.filter((x) => x !== dk) : [...prev, dk]))} />
                        按「{d.label || dk}」分组
                      </label>
                    )
                  })}
                  <button type="button" className="btn btn-xs btn-primary" disabled={busyId === m.id}
                          onClick={() => void runInlinePreview(m)} data-metric-inline-preview="">
                    {busyId === m.id ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 在线试算
                  </button>
                </div>

                {preview && <PreviewResultPanel preview={preview} />}
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}

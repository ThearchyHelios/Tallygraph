/**
 * 只读试运行的结果表（草稿页和口径目录共用）：一个取数节点一张表，列头 + 前几行 + 总行数。
 *
 * 试运行只是看数和定义对不对得上，不是正式出具，所以表上方写明这一点；页面最多画前二十行，再多只给总行数。
 * 遮罩过的格子在服务端已经是「已遮罩」这样的字样，这里照原样显示，不再按列名猜。
 * 旧服务端和旧草稿没有 queries，整块不画。
 */
import { LEDGER_UI } from '../../lib/termsLedger'
import type { DraftTrialQuery, QualityReport } from '../../typesLedger'

const D = LEDGER_UI.drafts

/** 每个查询最多画几行：再多给总行数，页面上不铺开整张表 */
const MAX_ROWS = 20

/** 格子转成文字：字符串照写，空值用破折号，对象转成 JSON 文本 */
function cellText(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  const text = JSON.stringify(value)
  return text ?? String(value)
}

export function TrialResult({ queries }: { queries?: DraftTrialQuery[] | null }) {
  const list = (queries ?? []).filter((q) => q && ((q.columns?.length ?? 0) > 0 || (q.rows?.length ?? 0) > 0))
  if (!list.length) return null
  return (
    <div className="mt-1.5 space-y-2" data-trial-result="">
      <p className="text-2xs leading-relaxed text-faint">{D.trialResult}</p>
      {list.map((q, i) => {
        const columns = q.columns ?? []
        const rows = q.rows ?? []
        const shown = rows.slice(0, MAX_ROWS)
        const truncated = !!q.truncated || rows.length > MAX_ROWS
        return (
          <div key={`${q.node_id}:${i}`} className="overflow-hidden rounded border" data-trial-query={q.node_id}>
            <div className="flex flex-wrap items-baseline gap-x-2 border-b bg-elev px-2 py-1 text-2xs">
              <span className="font-medium text-dim">{q.node_id}</span>
              {q.tool && <span className="text-faint">{q.tool}</span>}
              {q.source && <span className="text-faint">· {q.source}</span>}
              <span className="tnum ml-auto text-faint">{q.source_truncated ? `已取回 ${q.total_rows} 行，查询结果已截断` : D.trialRows(q.total_rows)}</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-2xs">
                <thead>
                  <tr className="border-b text-left text-faint">
                    {columns.map((column, j) => (
                      <th key={`${column}:${j}`} className="whitespace-nowrap px-2 py-1 font-normal">{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((row, r) => (
                    <tr key={r} className="border-b last:border-0" data-trial-row={r}>
                      {columns.map((column, j) => (
                        <td key={`${column}:${j}`} className="mono max-w-[240px] truncate px-2 py-1 text-dim"
                            title={cellText(row[j])}>
                          {cellText(row[j])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {shown.length === 0 && <p className="px-2 py-1 text-2xs text-faint">{D.trialNoRows}</p>}
            {truncated && <p className="border-t px-2 py-1 text-2xs text-faint">{D.trialTruncated(MAX_ROWS)}</p>}
          </div>
        )
      })}
    </div>
  )
}

/** 执行完成后仍单独展示验证结论；旧结果没有报告时明确保留未知。 */
export function TrialValidation({ report }: { report?: QualityReport | null }) {
  const status = report?.status ?? 'unknown'
  const labels = { pass: '质量检查通过', fail: '质量检查未通过', unknown: '尚未完成质量验证' }
  return (
    <div className="mt-2 rounded border px-2 py-1 text-2xs" data-trial-validation={status}
         style={{ color: status === 'pass' ? 'var(--ok)' : 'var(--warn)' }}>
      <p>{labels[status]}</p>
      {report?.checks?.length ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          {report.checks.map((check, index) => (
            <li key={`${check.code}:${check.metric_id ?? ''}:${index}`}>
              {check.name ? `${check.name}：` : ''}{check.detail}
            </li>
          ))}
        </ul>
      ) : status === 'unknown' ? <p>这份结果缺少验证记录，请重新试运行。</p> : null}
    </div>
  )
}

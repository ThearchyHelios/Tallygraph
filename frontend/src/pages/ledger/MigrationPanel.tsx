import { useState } from 'react'
import { api } from '../../api/client'
import { ErrorState, Notice } from '../../components/ui'
import type { CaliberDraft } from '../../typesLedger'

type Audit = Awaited<ReturnType<typeof api.caliberLedger.migration>>

export function MigrationPanel({ draft, onPrepared }: { draft: CaliberDraft; onPrepared: (id: string) => void }) {
  const [audit, setAudit] = useState<Audit | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  if (!draft.workflow_id) return null
  const check = async () => {
    setBusy(true); setError(null)
    try { setAudit(await api.caliberLedger.migration(draft.id)) }
    catch (e) { setError(e) } finally { setBusy(false) }
  }
  const prepare = async () => {
    if (!audit) return
    setBusy(true); setError(null)
    try { onPrepared((await api.caliberLedger.prepareMigration(draft.id, audit.revision)).draft_id) }
    catch (e) { setError(e) } finally { setBusy(false) }
  }
  return <section className="space-y-2 rounded-lg border p-3" data-migration-panel="">
    <div className="flex items-center justify-between gap-2">
      <h3 className="text-xs font-semibold">旧口径迁移检查</h3>
      <button className="btn btn-sm" disabled={busy} onClick={() => void check()}>{busy ? '处理中…' : '检查问数与取数粒度'}</button>
    </div>
    <p className="text-xs text-dim">逐项检查明细误作标量、必填参数、对象与日期映射，以及本机数据源是否就绪。</p>
    {error != null && <ErrorState error={error} />}
    {audit && <>
      <Notice tone="info">{audit.note}</Notice>
      <p className="text-xs">共 {audit.metrics_total} 项，{audit.metrics_needing_work} 项需处理</p>
      {audit.metrics.map(m => <details key={`${m.card_id}:${m.metric_id}`} className="rounded border p-2 text-xs">
        <summary className="cursor-pointer">{m.name} · {m.findings.length ? `${m.findings.length} 项待处理` : '静态检查通过，待试运行'}</summary>
        <ul className="my-2 list-disc space-y-1 pl-4">{m.findings.map((f, i) => <li key={i}>{f.message}</li>)}</ul>
        <p className="text-dim">{m.guidance}</p>
      </details>)}
      {audit.can_rebuild && <button className="btn btn-sm" disabled={busy} onClick={() => void prepare()}>从原文准备独立迁移草稿</button>}
      <p className="text-xs text-dim">只缺问数定义时可在下方补齐；取数粒度不符时，从原文分批重建、验证。新草稿验证并发布后，再切换调用口径。</p>
    </>}
  </section>
}

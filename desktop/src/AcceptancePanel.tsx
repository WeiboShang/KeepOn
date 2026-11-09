import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { rpc } from './rpc'
import type { AcceptanceReport, AcceptanceResult, AcceptanceStatus } from './types'

export const acceptanceLabels: Record<AcceptanceStatus, string> = {
  running: '执行中', queued: '等待检查', checking: '检查中', awaiting_review: '待人工签收',
  needs_changes: '交付待修正', check_error: '检查异常', passed: '验收通过', not_evaluated: '未验收',
}

const resultLabels = { passed: '通过', failed: '未通过', pending_review: '待人工确认', error: '检查异常' }
const reasons: Record<string, string> = {
  file_exists: '检查时工作区内存在该文件。',
  file_missing: '未找到所需文件或交付物副本。',
  unsafe_or_nonregular_file: '该路径不指向允许检查的普通文件。',
  artifact_missing: '本次运行未发布符合条件的交付物。',
  artifact_ambiguous: '本次运行有多份同名交付物，请使用唯一名称。',
  artifact_published: '本次运行已发布文件，副本可读取。',
  url_registered: '本次运行已登记链接。链接可达性未检查。',
  artifact_integrity: '交付物内容与发布时的哈希不一致。',
  json_too_large: 'JSON 文件超过 2 MiB。',
  invalid_json: '文件无法解析为受支持的 UTF-8 JSON。',
  schema_mismatch: 'JSON 内容与规格不符。',
  schema_valid: '已发布的 JSON 副本符合规则。',
  manual_review: '由用户确认此项交付结果。',
  file_unreadable: '检查器无法读取该文件。',
  check_error: '检查器未能完成检查。',
}

function ResultEvidence({ result }: { result: AcceptanceResult }) {
  const evidence = result.evidence
  return <>
    <p>{reasons[result.code] || resultLabels[result.status]}
      {evidence?.field_path && <> <code>{evidence.field_path}</code> · {evidence.rule}</>}
    </p>
    <small>检查时间 {new Date(result.checked_at).toLocaleString()}</small>
    {evidence?.path && <small><code>{evidence.path}</code> · {evidence.size_bytes?.toLocaleString()} 字节</small>}
    {evidence?.artifact_id && <a href={`#run-artifact-${evidence.artifact_id}`}>查看交付物 · {evidence.identifier}</a>}
    {evidence?.sha256 && <details><summary>内容哈希</summary><code>{evidence.sha256}</code></details>}
  </>
}

export function useAcceptanceReport(runId: string | null | undefined) {
  return useQuery({
    queryKey: ['acceptance', runId],
    enabled: Boolean(runId),
    queryFn: () => rpc.call<{report:AcceptanceReport|null}>('acceptance.get', { run_id: runId }).then(result => result.report),
    refetchInterval: query => ['running', 'queued', 'checking'].includes(query.state.data?.status || '') ? 2500 : false,
  })
}

export default function AcceptancePanel({ report, onRepair }: {
  report: AcceptanceReport
  onRepair?: (taskId: string, prompt: string) => void
}) {
  const client = useQueryClient()
  const [notes, setNotes] = useState<Record<string,string>>({})
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['acceptance', report.run_id] })
    void client.invalidateQueries({ queryKey: ['tasks'] })
  }
  const write = useMutation({
    mutationFn: ({ criterionId, decision }: { criterionId?: string; decision?: 'pass'|'reject' }) => {
      const params = { run_id: report.run_id, expected_report_version: report.version }
      if (!criterionId) return rpc.acceptance('acceptance.retry', params)
      return rpc.acceptance('acceptance.review', { ...params, criterion_id: criterionId, decision, note: notes[criterionId] || '' })
    },
    onSettled: refresh,
  })
  const latest = report.attempts.at(-1)
  const canAct = report.current && report.open
  const repair = () => {
    const lines = report.checks.flatMap(check => {
      const result = latest?.results.find(item => item.criterion_id === check.id)
      const review = report.reviews[check.id]
      if (review?.decision === 'reject') return [`- ${check.label} · ${review.note}`]
      if (result?.status === 'failed') return [`- ${check.label} · ${reasons[result.code] || result.code}${result.evidence?.field_path ? `（${result.evidence.field_path}）` : ''}`]
      return []
    })
    onRepair?.(report.task_id, ['请修正上一轮交付验收发现的问题，并在本次运行发布当前规格要求的交付物。', ...lines].join('\n'))
  }
  return <div className="acceptance-report">
    <div className="acceptance-summary">
      <strong className={`status ${report.status === 'passed' ? 'completed' : report.status === 'needs_changes' ? 'failed' : 'pending'}`}>
        {acceptanceLabels[report.status]}
      </strong>
      <span>{report.task_title ? `${report.task_title} · ` : ''}任务规格 v{report.spec_revision}</span>
      {!report.current && <small>历史报告 · 当前规格 v{report.current_spec_revision}，请以最新提交为准。</small>}
    </div>
    {report.status === 'not_evaluated' && <p>本次运行未正常结束，交付验收尚未执行。</p>}
    <ol className="acceptance-items">
      {report.checks.map(check => {
        const result = latest?.results.find(item => item.criterion_id === check.id)
        const review = report.reviews[check.id]
        return <li key={check.id}>
          <div className="acceptance-summary"><strong>{check.label}</strong><span>{review ? review.decision === 'pass' ? '已签收' : '已退回' : result ? resultLabels[result.status] : '尚未检查'}</span></div>
          {result && <ResultEvidence result={result}/>}
          {review && <small>{new Date(review.reviewed_at).toLocaleString()}{review.note && ` · ${review.note}`}</small>}
          {canAct && report.status === 'awaiting_review' && check.kind === 'manual' && !review && <div className="acceptance-review">
            <label>签收备注或退回原因<input maxLength={1000} value={notes[check.id] || ''}
              onChange={event => setNotes(values => ({ ...values, [check.id]: event.target.value }))}/></label>
            <div className="row-actions">
              <button className="primary" disabled={write.isPending} onClick={() => write.mutate({ criterionId: check.id, decision: 'pass' })}>签收此项</button>
              <button disabled={write.isPending || !notes[check.id]?.trim()} onClick={() => write.mutate({ criterionId: check.id, decision: 'reject' })}>退回此项</button>
            </div>
          </div>}
        </li>
      })}
    </ol>
    {report.attempts.length > 1 && <details><summary>先前的检查记录</summary>
      {report.attempts.slice(0, -1).map((attempt, index) => <details key={index}>
        <summary>{new Date(attempt.checked_at).toLocaleString()}</summary>
        <ul>{attempt.results.map(result => <li key={result.criterion_id}>
          <strong>{report.checks.find(check => check.id === result.criterion_id)?.label} · {resultLabels[result.status]}</strong>
          <ResultEvidence result={result}/>
        </li>)}</ul>
      </details>)}
    </details>}
    {write.error && <p className="form-error" role="alert">{write.error.message}</p>}
    {canAct && report.status === 'check_error' && <button disabled={write.isPending} onClick={() => write.mutate({})}>重试检查</button>}
    {canAct && report.status === 'needs_changes' && onRepair && <button className="primary" onClick={repair}>继续修正</button>}
  </div>
}

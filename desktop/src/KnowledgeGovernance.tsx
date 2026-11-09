import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { rpc } from './rpc'
import type { AcceptanceCheck, Task } from './types'

const checkLabels = {
  relevance: '与当前项目或用户长期偏好相关',
  source: '来源可信，能回到原始任务或文件核对',
  applicability: '适用范围清楚，不会跨项目误用',
  sensitive_information: '不含密钥、口令或其他敏感信息',
  prompt_injection: '不含要求 Agent 改变权限或忽略指令的内容',
} as const
type CheckKey = keyof typeof checkLabels
type Checks = Record<CheckKey, boolean>
const emptyChecks = (): Checks => ({ relevance: false, source: false, applicability: false, sensitive_information: false, prompt_injection: false })

// 知识、记忆和 Skill 共用审核门槛：来源、适用范围及风险项都需要确认。
function Review({ item, kind, refresh }: { item: any; kind: 'wiki'|'memory'|'skill'; refresh(): unknown }) {
  const [checks, setChecks] = useState<Checks>(emptyChecks)
  const [reason, setReason] = useState('')
  const safe = !Object.values(item.risk_flags || {}).some(Boolean)
  const approve = useMutation({
    mutationFn: () => rpc.call(kind === 'skill' ? 'skill_candidate.review' : `${kind}.approve`, {
      [kind === 'skill' ? 'candidate_id' : kind === 'wiki' ? 'entry_id' : 'memory_id']: item.id, checks,
    }),
    onSuccess: refresh,
  })
  const quarantine = useMutation({
    mutationFn: () => rpc.call(kind === 'skill' ? 'skill_candidate.quarantine' : `${kind}.quarantine`, {
      [kind === 'skill' ? 'candidate_id' : kind === 'wiki' ? 'entry_id' : 'memory_id']: item.id, reason,
    }),
    onSuccess: refresh,
  })
  return <details className="knowledge-review"><summary>{item.title} · {item.status} · v{item.version || item.revision || 1}</summary>
    <p>{item.summary || item.description}</p>
    <small>来源：{item.source_type || (kind === 'skill' ? '成功任务' : '运行反思')} · {item.source_ref || item.example_run_ids?.join(', ') || '未标注'}<br/>适用范围：{item.applicability || '待审核'}</small>
    {item.source_verified === false && <p className="form-error">来源目前无法在本项目中核实，需先修订来源。</p>}
    <pre>{item.content || item.instructions}</pre>
    {Object.entries(item.risk_flags || {}).filter(([, value]) => value).map(([key]) => <p className="form-error" key={key}>自动检查提示：{key}</p>)}
    {item.status === 'candidate' || item.status === 'ready' ? <>
      <div className="knowledge-checks">{(Object.keys(checkLabels) as CheckKey[]).map(key => <label key={key}><input type="checkbox" checked={checks[key]} onChange={event => setChecks({ ...checks, [key]: event.target.checked })}/>{checkLabels[key]}</label>)}</div>
      <button className="primary" disabled={!safe || item.source_verified === false || !Object.values(checks).every(Boolean) || approve.isPending} onClick={() => approve.mutate()}>确认审核并启用</button>
    </> : null}
    {['candidate', 'observing', 'ready', 'active', 'approved', 'promoted'].includes(item.status) && <>
      <input value={reason} onChange={event => setReason(event.target.value)} placeholder="隔离原因" aria-label="隔离原因"/>
      <button disabled={!reason.trim() || quarantine.isPending} onClick={() => quarantine.mutate()}>隔离</button>
    </>}
    {(approve.error || quarantine.error) && <p className="form-error">{String(approve.error || quarantine.error)}</p>}
  </details>
}

function SkillVersions({ skill, refresh }: { skill: any; refresh(): unknown }) {
  const [open, setOpen] = useState(false)
  const history = useQuery({
    queryKey: ['skill-history', skill.scope, skill.name],
    queryFn: () => rpc.call<{versions: any[]}>('skill.history', { name: skill.name, scope: skill.scope }).then(value => value.versions),
    enabled: open,
  })
  const restore = useMutation({
    mutationFn: (version: number) => rpc.call('skill.restore', { name: skill.name, scope: skill.scope, version }),
    onSuccess: () => { void history.refetch(); refresh() },
  })
  return <details className="knowledge-review" onToggle={event => setOpen(event.currentTarget.open)}><summary>{skill.name} · 当前 v{skill.version || 1}</summary>
    {history.data?.map(version => <div key={version.version}><strong>v{version.version}</strong><small>{new Date(version.created_at).toLocaleString()}</small><pre>{version.instructions}</pre><button disabled={restore.isPending || version.version === skill.version} onClick={() => restore.mutate(version.version)}>恢复为新版本</button></div>)}
    {restore.error && <p className="form-error">{String(restore.error)}</p>}
  </details>
}

const checkKinds: Record<AcceptanceCheck['kind'], string> = {
  file_exists: '文件存在', json_schema: 'JSON 结构', artifact_published: '交付物已发布', manual: '人工确认',
}

interface CheckDraft {
  id: string
  label: string
  kind: AcceptanceCheck['kind']
  target: string
  artifactKind: 'file' | 'url'
  schema: string
}

function checkDraft(check?: AcceptanceCheck, label = ''): CheckDraft {
  return {
    id: check?.id || crypto.randomUUID(),
    label: check?.label || label,
    kind: check?.kind || 'manual',
    target: check?.kind === 'file_exists' ? check.config.path : check?.kind === 'json_schema' ? check.config.filename
      : check?.kind === 'artifact_published' ? check.config.identifier : '',
    artifactKind: check?.kind === 'artifact_published' ? check.config.kind : 'file',
    schema: JSON.stringify(check?.kind === 'json_schema' ? check.config.schema : { type: 'object' }, null, 2),
  }
}

function serializeCheck({ id, label, kind, target, artifactKind, schema }: CheckDraft): AcceptanceCheck {
  if (kind === 'file_exists') return { id, label, kind, config: { path: target.trim() } }
  if (kind === 'artifact_published') return { id, label, kind, config: { kind: artifactKind, identifier: target.trim() } }
  if (kind === 'json_schema') {
    try {
      return { id, label, kind, config: { filename: target.trim(), schema: JSON.parse(schema) } }
    } catch {
      throw new Error(`「${label || 'JSON 检查'}」的规则不是有效 JSON。`)
    }
  }
  return { id, label, kind, config: {} }
}

function TaskSpecEditor({ task, refresh }: { task: Task; refresh(): unknown }) {
  const [goal, setGoal] = useState(task.goal || '')
  const [constraints, setConstraints] = useState((task.constraints || []).join('\n'))
  const [criteria, setCriteria] = useState((task.success_criteria || []).join('\n'))
  const managed = Boolean(task.acceptance_checks?.length)
  const [enabled, setEnabled] = useState(managed)
  const [checks, setChecks] = useState(() => (task.acceptance_checks || []).map(check => checkDraft(check)))
  const updateCheck = (id: string, changes: Partial<CheckDraft>) => {
    setChecks(items => items.map(item => item.id === id ? { ...item, ...changes } : item))
  }
  const revise = useMutation({
    mutationFn: () => {
      const params = {
        task_id: task.id, goal, expected_spec_revision: task.spec_revision || 1,
        constraints: constraints.split('\n').map(value => value.trim()).filter(Boolean),
        success_criteria: criteria.split('\n').map(value => value.trim()).filter(Boolean),
      }
      return enabled
        ? rpc.acceptance('task.spec_revise', { ...params, acceptance_checks: checks.map(serializeCheck) })
        : rpc.call('task.spec_revise', params)
    },
    onSuccess: refresh,
  })
  return <details className="knowledge-review"><summary>{task.title} · Spec v{task.spec_revision || 1}</summary>
    <small>已绑定 Run：{Object.entries(task.spec_bindings || {}).map(([run, version]) => `${run.slice(0, 8)}(v${version})`).join('、') || '无'}</small>
    <small>修订后的约束从下一次 Run 开始生效；已有 Run 保持原绑定版本。</small>
    <label>目标<textarea value={goal} onChange={event => setGoal(event.target.value)}/></label>
    <label>约束（每行一条）<textarea value={constraints} onChange={event => setConstraints(event.target.value)}/></label>
    <label>验收条件（每行一条）<textarea value={criteria} onChange={event => setCriteria(event.target.value)}/></label>
    <label className="acceptance-toggle"><input type="checkbox" checked={enabled} disabled={managed}
      onChange={event => {
        setEnabled(event.target.checked)
        if (event.target.checked && !checks.length) {
          const labels = criteria.split('\n').map(value => value.trim()).filter(Boolean)
          setChecks(labels.length ? labels.map(label => checkDraft(undefined, label)) : [checkDraft()])
        }
      }}/>启用交付验收</label>
    {enabled && <div className="acceptance-editor">
      <small>所有检查通过后任务才会完成。文件发布与 JSON 检查以本次 Run 的交付物为准。</small>
      {checks.length > 20 && <p className="form-error">每份规格最多 20 项，请合并或移除部分验收项。</p>}
      {checks.map((check, index) => <fieldset key={check.id}>
        <legend>验收项 {index + 1}</legend>
        <label>验收说明<input maxLength={500} value={check.label} onChange={event => updateCheck(check.id, { label: event.target.value })}/></label>
        <label>检查类型<select value={check.kind} onChange={event => updateCheck(check.id, { kind: event.target.value as AcceptanceCheck['kind'], target: '' })}>
          {Object.entries(checkKinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        {check.kind === 'artifact_published' && <label>交付物类型<select value={check.artifactKind}
          onChange={event => updateCheck(check.id, { artifactKind: event.target.value as 'file' | 'url', target: '' })}>
          <option value="file">文件</option><option value="url">URL</option>
        </select></label>}
        {check.kind !== 'manual' && <label>{check.kind === 'file_exists' ? '工作区相对路径'
          : check.kind === 'artifact_published' && check.artifactKind === 'url' ? 'URL 交付物标题' : '发布文件名'}
          <input value={check.target} onChange={event => updateCheck(check.id, { target: event.target.value })}/>
        </label>}
        {check.kind === 'json_schema' && <label>JSON Schema<textarea rows={5} spellCheck={false} value={check.schema}
          onChange={event => updateCheck(check.id, { schema: event.target.value })}/></label>}
        <button onClick={() => setChecks(items => items.filter(item => item.id !== check.id))}>移除验收项</button>
      </fieldset>)}
      <button disabled={checks.length >= 20} onClick={() => setChecks(items => [...items, checkDraft()])}>添加验收项</button>
    </div>}
    {enabled && task.status === 'completed' && <small>保存后会重新打开任务并开始新的验收版本。</small>}
    <button disabled={revise.isPending || enabled && (!checks.length || checks.length > 20)} onClick={() => revise.mutate()}>保存 Spec v{(task.spec_revision || 1) + 1}</button>
    {revise.error && <p className="form-error">{String(revise.error)}</p>}
  </details>
}

export default function KnowledgeGovernance({ skills, installedSkills, refreshSkills }: { skills: any[]; installedSkills: any[]; refreshSkills(): unknown }) {
  const wiki = useQuery({ queryKey: ['wiki-governance'], queryFn: () => rpc.call<{entries: any[]}>('wiki.list', {}).then(value => value.entries) })
  const memory = useQuery({ queryKey: ['memory-governance'], queryFn: () => rpc.call<any>('memory.list', {}) })
  const tasks = useQuery({ queryKey: ['task-specs'], queryFn: () => rpc.call<{tasks: Task[]}>('task.list', {limit: 100}).then(value => value.tasks) })
  const [draft, setDraft] = useState({ title: '', summary: '', content: '', source_type: 'manual', source_ref: '', applicability: '', supersedes_id: '' })
  const [memoryDraft, setMemoryDraft] = useState({ id: '', title: '', summary: '', content: '', source_ref: '', applicability: 'user' })
  const propose = useMutation({ mutationFn: () => rpc.call('wiki.propose', draft), onSuccess: () => { setDraft({ title: '', summary: '', content: '', source_type: 'manual', source_ref: '', applicability: '', supersedes_id: '' }); wiki.refetch() } })
  const proposeMemory = useMutation({
    mutationFn: () => rpc.call(memoryDraft.id ? 'memory.revise' : 'memory.propose', {
      memory_id: memoryDraft.id, title: memoryDraft.title, summary: memoryDraft.summary,
      content: memoryDraft.content, source_ref: memoryDraft.source_ref, applicability: memoryDraft.applicability,
    }),
    onSuccess: () => { setMemoryDraft({ id: '', title: '', summary: '', content: '', source_ref: '', applicability: 'user' }); memory.refetch() },
  })
  const promote = useMutation({ mutationFn: (candidate: any) => rpc.call('skill.install', {
    name: candidate.action?.kind === 'update' ? candidate.action.skill_name : candidate.name,
    description: candidate.description, instructions: candidate.instructions,
    scope: 'project', candidate_id: candidate.id,
  }), onSuccess: refreshSkills })
  const refresh = () => { void wiki.refetch(); void memory.refetch(); refreshSkills() }
  return <><h2>知识审核</h2><p className="field-note">候选内容只会在人工核对后进入运行上下文。自动检查只能提示明显风险，来源和事实仍需人工判断。</p>
    <div className="extension-columns"><div><h3>项目 Wiki</h3>
      <div className="mini-form">
        <input placeholder="标题" value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })}/>
        <input placeholder="摘要" value={draft.summary} onChange={event => setDraft({ ...draft, summary: event.target.value })}/>
        <textarea placeholder="项目事实或经验" value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })}/>
        <select value={draft.source_type} onChange={event => setDraft({ ...draft, source_type: event.target.value })}><option value="manual">人工记录</option><option value="file">仓库文件</option><option value="run">任务运行</option></select>
        <input placeholder="来源路径或 Run ID" value={draft.source_ref} onChange={event => setDraft({ ...draft, source_ref: event.target.value })}/>
        <input placeholder="适用范围，例如 backend API" value={draft.applicability} onChange={event => setDraft({ ...draft, applicability: event.target.value })}/>
        {draft.supersedes_id && <small>正在修订 {draft.supersedes_id}</small>}
        <button disabled={!draft.title || !draft.summary || !draft.content || !draft.source_ref || !draft.applicability || propose.isPending} onClick={() => propose.mutate()}>提交候选</button>
        {propose.error && <p className="form-error">{String(propose.error)}</p>}
      </div>
      {wiki.data?.map(item => <div key={item.id}><Review item={item} kind="wiki" refresh={refresh}/>{item.status === 'active' || item.status === 'quarantined' ? <button onClick={() => setDraft({ title: item.title, summary: item.summary, content: item.content, source_type: item.source_type, source_ref: item.source_ref, applicability: item.applicability, supersedes_id: item.id })}>修订为新版本</button> : null}</div>)}
    </div><div><h3>长期记忆</h3>
      <div className="mini-form">
        {memoryDraft.id && <small>正在修订 {memoryDraft.id}</small>}
        <input placeholder="标题" value={memoryDraft.title} onChange={event => setMemoryDraft({ ...memoryDraft, title: event.target.value })}/>
        <input placeholder="摘要" value={memoryDraft.summary} onChange={event => setMemoryDraft({ ...memoryDraft, summary: event.target.value })}/>
        <textarea placeholder="长期事实或偏好" value={memoryDraft.content} onChange={event => setMemoryDraft({ ...memoryDraft, content: event.target.value })}/>
        <input placeholder="来源，例如用户明确说明" value={memoryDraft.source_ref} onChange={event => setMemoryDraft({ ...memoryDraft, source_ref: event.target.value })}/>
        <input placeholder="适用范围" value={memoryDraft.applicability} onChange={event => setMemoryDraft({ ...memoryDraft, applicability: event.target.value })}/>
        <button disabled={!memoryDraft.title || !memoryDraft.summary || !memoryDraft.content || !memoryDraft.source_ref || proposeMemory.isPending} onClick={() => proposeMemory.mutate()}>提交记忆候选</button>
        {proposeMemory.error && <p className="form-error">{String(proposeMemory.error)}</p>}
      </div>
      {memory.data?.candidates?.map((item: any) => <Review item={item} kind="memory" refresh={refresh} key={item.id}/>)}
      {[...(memory.data?.active || []), ...(memory.data?.quarantined || [])].map((item: any) => <div key={item.id}><Review item={item} kind="memory" refresh={refresh}/><button onClick={() => setMemoryDraft({ id: item.id, title: item.title, summary: item.summary, content: item.content, source_ref: 'user_revision', applicability: item.applicability || 'user' })}>修订为新版本</button></div>)}
      <h3>Skill 候选</h3>
      {skills.map((item: any) => <div key={item.id}><Review item={item} kind="skill" refresh={refresh}/>{item.status === 'approved' && <button disabled={promote.isPending} onClick={() => promote.mutate(item)}>{item.action?.kind === 'update' ? `替代 ${item.action.skill_name}` : '安装已审核 Skill'}</button>}</div>)}
      {promote.error && <p className="form-error">{String(promote.error)}</p>}
      <h3>Skill 版本</h3>{installedSkills.map((skill: any) => <SkillVersions key={`${skill.scope}:${skill.name}`} skill={skill} refresh={refreshSkills}/>)}
    </div></div>
    <h3>任务 Spec</h3>{tasks.data?.filter(task => ['active', 'blocked', 'pending'].includes(task.status) || task.status === 'completed' && task.acceptance_checks?.length).map(task => <TaskSpecEditor key={`${task.id}:${task.spec_revision}`} task={task} refresh={() => tasks.refetch()}/>)}
  </>
}

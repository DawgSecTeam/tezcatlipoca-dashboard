// One run's page, mounted under the competition shell (/c/:id/runs/:run): identity header,
// a sourced "General" panel (phase display, facts, actions), this run's own timeline and
// log, its teams and infrastructure, and the evidence it produced. /scrims/:id redirects here.
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, Spinner } from '../components/ui'
import { Crumbs, Icon, agoMs, fmtSpan, toMs, useTick } from '../components/kit'
import {
  ACTION_LABEL, NewRunModal, ResetBoxModal, StageStrip, StatePill,
  useRunActions, useRunEvents, useRunLog,
} from '../components/runui'
import RunTeams from '../components/RunTeams'
import RunInfra from '../components/RunInfra'

const TABS = ['teams', 'infrastructure', 'evidence']

// The pre-shell URL, kept alive because Live cards navigate to it.
export function ScrimRedirect() {
  const { id } = useParams()
  const [run, setRun] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => { api.run(id).then(setRun).catch(setErr) }, [id])
  if (err) return <div className="p-10"><ErrorBanner error={err} /></div>
  if (!run) return <div className="flex h-screen items-center justify-center"><Spinner /></div>
  return <Navigate to={`/c/${run.comp_id}/runs/${id}`} replace />
}

// Every fact names its source: the competition definition, this run's record, or the range's
// inventory. Conflating them is what put "2 teams" on a 1-team range — a range's team count
// comes from its inventory, never from run.params.teams.
const FACT_SOURCE = {
  comp: ['comp', 'From the competition definition (this workspace)'],
  run: ['run', "From this run's own record"],
  range: ['range', "From the range's inventory"],
}

function Facts({ facts }) {
  return (
    <dl className="grid grid-cols-[7rem_3.5rem_minmax(0,1fr)] items-baseline gap-x-3 gap-y-2 text-sm">
      {facts.map((f) => (
        <Fragment key={f.label}>
          <dt className="truncate text-[11px] font-semibold uppercase tracking-wider text-faint">{f.label}</dt>
          <dd className="truncate text-[10px] font-semibold uppercase tracking-wider text-faint/70" title={FACT_SOURCE[f.source][1]}>{FACT_SOURCE[f.source][0]}</dd>
          <dd className="min-w-0 truncate font-mono text-xs text-muted" title={f.title ?? String(f.value ?? '—')}>{f.value ?? '—'}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

// Lifecycle + stage events as a rail — the raw event dump read as a sequence, each entry
// stamped with its offset from the run's start.
function Timeline({ events, start }) {
  if (!events.length) return <p className="text-xs text-faint">Waiting for the run's first event.</p>
  return (
    <ol className="relative space-y-3 border-l-2 border-line pl-4">
      {events.map((ev) => {
        const d = ev.data
        const text = typeof d === 'string' ? d
          : d?.error || d?.message || d?.text || (Array.isArray(d?.cmd) ? d.cmd.join(' ') : '')
        return (
          <li key={ev.seq} className="relative">
            <span className={`absolute -left-[21px] top-1.5 h-2 w-2 rounded-full ${ev.kind === 'stage' ? 'bg-accent' : 'bg-line'}`} />
            <div className="flex items-baseline gap-2 text-xs">
              <span className="min-w-0 truncate font-semibold text-fg" title={ev.stage}>{ev.stage || d?.state || ev.kind}</span>
              <span className="ml-auto shrink-0 font-mono tabular-nums text-faint">{start && ev.ts ? `+${fmtSpan(start, ev.ts)}` : ''}</span>
            </div>
            <div className="font-mono text-[10px] text-faint">
              {ev.ts ? new Date(toMs(ev.ts)).toLocaleTimeString() : ''} · {ev.kind}
            </div>
            {text && <div className="mt-0.5 min-w-0 truncate text-xs text-muted" title={text}>{text}</div>}
          </li>
        )
      })}
    </ol>
  )
}

// Deploy phases announce themselves as "[3/8] …", scrims as "phase 3/8"; both become jump
// targets. The line is derived from the marker's character offset — every log line has the
// same height, so the offset ratio is an accurate scroll position.
const PHASE = /\[(\d+)\/(\d+)\]|phase\s+(\d+)\s*\/\s*(\d+)/gi

function phaseMarks(text) {
  const out = [], seen = new Set()
  for (const m of text.matchAll(PHASE)) {
    const n = m[1] ?? m[3]
    if (seen.has(n)) continue
    seen.add(n)
    out.push({ n, total: m[2] ?? m[4], line: text.slice(0, m.index).split('\n').length })
  }
  return out
}

// LogView owns its own <pre> ref, so the phase anchors need our own scroll container; the
// status row and follow toggle mirror it.
function Output({ log }) {
  const [follow, setFollow] = useState(true)
  const pre = useRef()
  useEffect(() => { if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [log.text, follow])
  // The page ticks every second; don't rescan a multi-megabyte log on each tick.
  const marks = useMemo(() => phaseMarks(log.text), [log.text])
  const jump = (m) => {
    const el = pre.current
    if (!el) return
    setFollow(false)
    el.scrollTop = (m.line / (log.text.split('\n').length || 1)) * el.scrollHeight
  }
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        {log.running ? <><Spinner /><span className="text-muted">Running…</span></>
          : <span className={`chip px-3 py-1 ${log.returncode === 0 ? 'bg-accent-soft text-accent' : 'bg-danger-soft text-danger'}`}>
              {log.returncode === 0 ? 'Finished' : `Failed · exit ${log.returncode}`}
            </span>}
        {marks.map((m) => (
          <button key={m.n} onClick={() => jump(m)} title={`Jump to phase ${m.n} of ${m.total}`}
            className="chip bg-sunken px-2 py-0.5 font-mono text-[11px] text-muted transition hover:text-accent">
            phase {m.n}/{m.total}
          </button>
        ))}
        <label className="ml-auto flex shrink-0 items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-[var(--accent)]" /> Follow
        </label>
      </div>
      <pre ref={pre} style={{ height: '50vh' }} className="overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">{log.text || ' '}</pre>
    </div>
  )
}

const fmtBytes = (n) => (n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)

// Order a reader wants: the report, then the team write-ups, then the raw harness capture.
const CATS = [
  ['Report', (rel, base) => ['REPORT.md', 'collection.json', 'index.json'].includes(base)],
  ['Blue', (rel, base) => base === 'BLUE-TEAM.md' || rel.startsWith('evidence/blue')],
  ['Red', (rel, base) => base === 'RED-TEAM.md' || rel.startsWith('evidence/red')],
  ['Harness', (rel, base) => rel.startsWith('evidence/harness') || base === '.deploy-timings.jsonl'],
]

function FileGroups({ rows, group, onDownload }) {
  const rel = (p) => (p.startsWith(`${group}/`) ? p.slice(group.length + 1) : p)
  const base = (p) => p.split('/').pop()
  const cats = CATS.map(([name, match]) => ({ name, files: rows.filter((r) => match(rel(r.path), base(rel(r.path)))) }))
  const rest = rows.filter((r) => !CATS.some(([, match]) => match(rel(r.path), base(rel(r.path)))))
  if (rest.length) cats.push({ name: 'Other', files: rest })
  return (
    <div className="space-y-4">
      {cats.filter((c) => c.files.length).map((c) => (
        <section key={c.name}>
          <div className="mb-1.5 flex items-baseline gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">{c.name}</h3>
            <span className="text-[11px] tabular-nums text-faint">{c.files.length}</span>
          </div>
          <ul className="space-y-1">
            {c.files.map((r) => (
              <li key={r.path}>
                <button onClick={() => onDownload(r.path, base(r.path))} title={`Download ${r.path}`}
                  className="flex w-full items-center gap-2 rounded-xl px-2 py-1 text-left text-xs transition hover:bg-sunken">
                  <Icon name="packet" className="h-3.5 w-3.5 shrink-0 text-faint" />
                  <span className="min-w-0 truncate font-mono text-muted">{rel(r.path)}</span>
                  <span className="ml-auto shrink-0 tabular-nums text-faint">{fmtBytes(r.bytes)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

function Evidence({ runId }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    setRows(null); setErr(null)
    api.runArtifacts(runId).then(setRows).catch(setErr)
  }, [runId])
  if (err) return <ErrorBanner error={err} />
  if (!rows) return <div className="flex justify-center p-10"><Spinner /></div>
  // The evidence root belongs to the whole workspace, so a path's first segment is the run
  // that produced it — a run page must never present another run's files as its own.
  const groups = {}
  rows.forEach((r) => { (groups[r.path.split('/')[0]] ||= []).push(r) })
  const mine = groups[runId] || []
  const others = Object.entries(groups).filter(([g]) => g !== runId)
  // The API needs the bearer header, so a row fetches the file and hands the blob to the browser.
  const grab = async (path, name) => {
    try {
      const blob = await api.runArtifactFile(runId, path)
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = name
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) { setErr(e) }
  }
  return (
    <div className="rise space-y-6 pb-6">
      <Card className="p-6">
        <div className="mb-4 flex items-baseline justify-between gap-2">
          <h2 className="label !mb-0">This run</h2>
          <span className="font-mono text-[11px] text-faint">{mine.length} file{mine.length === 1 ? '' : 's'}</span>
        </div>
        {mine.length
          ? <FileGroups rows={mine} group={runId} onDownload={grab} />
          : <p className="text-sm text-faint">No evidence collected for <span className="font-mono">{runId}</span> yet — teardown copies a run's evidence into the workspace's <span className="font-mono">.automated-tests/</span> folder.</p>}
      </Card>
      {others.length > 0 && (
        <Card className="p-6">
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-muted">Other runs' evidence in this workspace ({others.length})</summary>
            <div className="mt-4 space-y-6">
              {others.map(([g, list]) => (
                <div key={g}>
                  <div className="mb-2 font-mono text-xs font-semibold text-muted">{g} <span className="text-faint">· {list.length}</span></div>
                  <FileGroups rows={list} group={g} onDownload={grab} />
                </div>
              ))}
            </div>
          </details>
        </Card>
      )}
    </div>
  )
}

export default function Run() {
  const { run: id, '*': sub } = useParams()
  const tab = TABS.includes(sub) ? sub : 'overview'
  const [run, setRun] = useState(null)
  const [comp, setComp] = useState(null)
  const [inventory, setInventory] = useState(null)
  const [comps, setComps] = useState(null)
  const [events, setEvents] = useState([])
  const [err, setErr] = useState(null)
  const [newRun, setNewRun] = useState(false)
  const [resetBox, setResetBox] = useState(null) // null = closed, {} | {team} = prefill
  const log = useRunLog(id)
  useTick(1000)
  const reload = () => api.run(id).then(setRun).catch(() => {})
  const { submit, destroy, cancel, pause, resume, live, held, canPause } = useRunActions(run, reload)
  const { updated } = useRunEvents(id, (ev) => {
    setEvents((e) => (e.some((x) => x.seq === ev.seq) ? e : [...e, ev].sort((a, b) => (a.seq || 0) - (b.seq || 0))))
    reload()
  })

  useEffect(() => {
    setRun(null); setComp(null); setInventory(null); setEvents([]); setErr(null)
    api.run(id)
      .then((r) => { setRun(r); api.comp(r.comp_id).then(setComp).catch(() => {}) })
      .catch(setErr)
    api.runInventory(id).then(setInventory).catch(() => {})
    api.runEvents(id, 0).then((r) => setEvents(r.events || [])).catch(() => {})
  }, [id])
  useEffect(() => {
    document.title = run
      ? `${comp?.name || run.comp_id} · ${ACTION_LABEL[run.action] || run.action} · tezcatlipoca`
      : 'tezcatlipoca'
  }, [run, comp])

  if (err) return <div className="p-10"><ErrorBanner error={err} /></div>
  if (!run) return <div className="flex justify-center p-10"><Spinner /></div>

  const boxes = comp?.boxes || []
  const range = inventory?.range || null
  // A live range has no teardown journal at all; the journal line exists only once a destroy
  // touched the range, and there red01's absence is what separates "retired" from "a VM
  // holding the LLM key is still up".
  const teardown = inventory?.teardown?.run_id || inventory?.teardown?.done?.length ? inventory.teardown : null
  const teams = range?.teams ?? '—'
  const facts = [
    { label: 'Teams', source: 'range', value: teams, title: "The deployed range's live team count" },
    { label: 'Range', source: 'range', value: range?.state?.replace('_', ' ') ?? '—', title: 'building, live, retired, teardown_failed or failed — as the range last reported it' },
    { label: 'Deploy run', source: 'range', value: range?.deploy_run_id ?? '—', title: 'The run that deployed this range' },
    { label: 'Teardown', source: 'range', value: teardown ? `${teardown.run_id || '—'} · red01 ${teardown.red01_done ? 'down' : 'still up'}` : '—', title: teardown ? `Journal steps done: ${(teardown.done || []).join(', ') || 'none'}` : 'No teardown journal in this range workspace' },
    { label: 'Boxes', source: 'comp', value: comp?.boxes?.length ?? '—' },
    { label: 'Services', source: 'comp', value: comp?.services ?? '—' },
    { label: 'Box names', source: 'comp', value: boxes.map((b) => b.name).join(', ') || '—' },
    { label: 'Action', source: 'run', value: ACTION_LABEL[run.action] || run.action },
    { label: 'Engine ref', source: 'run', value: run.engine_ref || 'main' },
    { label: 'rc', source: 'run', value: run.returncode ?? '—' },
    { label: 'Created', source: 'run', value: agoMs(run.created_at) || '—' },
    { label: 'Started', source: 'run', value: agoMs(run.started_at || run.created_at) || '—' },
    { label: 'Ended', source: 'run', value: run.ended_at ? agoMs(run.ended_at) : '—' },
    { label: 'Duration', source: 'run', value: fmtSpan(run.started_at || run.created_at, run.ended_at) },
    { label: 'Run dir', source: 'run', value: run.run_dir || '—' },
  ]

  return (
    <div className="rise space-y-6 pb-6">
      <header>
        <Crumbs items={[['Comps', '/comps'], [comp?.name || run.comp_id, `/c/${run.comp_id}`], [run.id]]} />
        <div className="flex min-w-0 items-center gap-3">
          <Link to={`/c/${run.comp_id}`} className="display min-w-0 truncate text-4xl transition hover:text-accent">
            {comp?.name || run.comp_id}
          </Link>
          <span className="chip shrink-0 bg-sunken px-2.5 py-1 font-mono text-muted">{ACTION_LABEL[run.action] || run.action}</span>
          <StatePill state={run.state} />
          {range?.state === 'teardown_failed' && (
            <span className="chip shrink-0 bg-danger-soft px-2.5 py-1 text-danger" title="The range's terraform resources are gone, but a non-terraform VM survived">range teardown failed</span>
          )}
        </div>
        {/* One row, clipped rather than wrapped: a raggedy header was the old page's worst tell. */}
        <div className="mt-2 flex min-w-0 items-center gap-x-3 overflow-hidden whitespace-nowrap font-mono text-xs text-faint">
          <span className="min-w-0 truncate" title={run.id}>{run.id}</span>
          <span className="shrink-0" title="Engine ref">{run.engine_ref || 'main'}</span>
          <span className="shrink-0">rc {run.returncode ?? '—'}</span>
          <span className="shrink-0">started {agoMs(run.started_at || run.created_at) || '—'}</span>
          <span className="shrink-0">elapsed {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
          <span className="ml-auto flex shrink-0 items-center gap-1.5" title="Last SSE event or poll">
            <span className={`h-1.5 w-1.5 rounded-full ${Date.now() - (updated || 0) < 15000 ? 'bg-accent' : 'bg-line'}`} />
            updated {updated ? agoMs(updated) : '—'}
          </span>
        </div>
      </header>

      {tab === 'overview' && (
        <>
          <Card className="p-6">
            <h2 className="label !mb-3">General</h2>
            <div className="min-h-[5.5rem]">
              <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
                stageEvents={events.filter((e) => e.kind === 'stage')} />
            </div>
            <div className="mt-5">
              <Facts facts={facts} />
            </div>
            {range?.state === 'teardown_failed' && (
              <div className="mt-4 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger">
                Range teardown failed: terraform's resources are gone but a VM survives
                {teardown?.red01_done === false && ' — red01 is still up, holding the LLM key'}.
                {teardown?.run_id && <> The newest destroy run <Link className="underline" to={`/c/${run.comp_id}/runs/${teardown.run_id}`}>{teardown.run_id}</Link> has its log.</>}
              </div>
            )}
            {run.error && (
              <div className="mt-4 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger">{String(run.error)}</div>
            )}
            <div className="mt-6 flex flex-wrap gap-2">
              <Button onClick={() => submit({ action: 'verify' })}><Icon name="check" />Verify</Button>
              <Button onClick={() => { setNewRun(true); api.comps().then(setComps).catch(() => {}) }}>
                <Icon name="rocket" />New run (re-deploy)
              </Button>
              <Button onClick={() => setResetBox({})}><Icon name="box" />Reset a box…</Button>
              <Button variant="danger" onClick={destroy}><Icon name="trash" />Destroy / teardown</Button>
              {canPause && <Button onClick={pause}><Icon name="pause" />Pause after next stage</Button>}
              {held && <Button variant="primary" onClick={resume}><Icon name="play" />Resume</Button>}
              {live && <Button variant="danger" onClick={cancel}><Icon name="warn" />Cancel run</Button>}
            </div>
          </Card>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
            <Card className="p-6">
              <h2 className="label !mb-3">Timeline</h2>
              <div className="max-h-[52vh] overflow-auto pr-1">
                <Timeline events={events} start={run.started_at || run.created_at} />
              </div>
            </Card>
            <Card className="p-6">
              <div className="mb-3 flex items-baseline justify-between gap-2">
                <h2 className="label !mb-0">Output</h2>
                <span className="text-[11px] text-faint">this run's engine log</span>
              </div>
              <Output log={log} />
            </Card>
          </div>
        </>
      )}

      {tab === 'teams' && <RunTeams run={run} comp={comp} inventory={inventory} />}
      {tab === 'infrastructure' && <RunInfra run={run} comp={comp} inventory={inventory} />}
      {tab === 'evidence' && <Evidence runId={id} />}

      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
        initial={{
          comp_id: run.comp_id, action: 'deploy', engine_ref: run.engine_ref || 'main',
          teams: range?.teams ?? run.params?.teams ?? 2, scoring_vmid: run.params?.scoring_vmid ?? '',
        }}
        onSubmitted={reload} />
      <ResetBoxModal open={resetBox !== null} onClose={() => setResetBox(null)} run={run}
        initial={resetBox || {}} onSubmitted={reload} />
    </div>
  )
}

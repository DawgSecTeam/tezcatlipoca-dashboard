// One run's page, mounted under the competition shell (/c/:id/runs/:run): identity header,
// a sourced "General" panel (phase display, facts, actions), this run's own timeline and
// log, its teams and infrastructure, and the evidence it produced. /scrims/:id redirects here.
import { useEffect, useRef, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, SearchInput, Spinner } from '../components/ui'
import { Crumbs, Icon, agoMs, fmtSpan, toMs, useTick, useToast } from '../components/kit'
import {
  ACTION_LABEL, ArtifactPreview, LogView, NewRunModal, ResetBoxModal, StageStrip, StatePill, eventText,
  useRunActions, useRunEvents, useRunLog,
} from '../components/runui'
import RunTeams from '../components/RunTeams'
import RunInfra from '../components/RunInfra'
import CompetitionPanel from '../components/CompetitionPanel'

const TABS = ['teams', 'infrastructure', 'evidence']

// The pre-shell URL, kept alive because Live cards navigate to it.
export function ScrimRedirect() {
  const { id } = useParams()
  const [run, setRun] = useState(null)
  const [err, setErr] = useState(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => { api.run(id).then(setRun).catch(setErr) }, [id, attempt])
  if (err) return <div className="p-10"><ErrorBanner error={err} onRetry={() => { setErr(null); setAttempt((a) => a + 1) }} /></div>
  if (!run) return <div className="flex h-screen items-center justify-center"><Spinner /></div>
  return <Navigate to={`/c/${run.comp_id}/runs/${id}`} replace />
}

// One labeled key-value group; the old panel mixed every source in one grid and a middle
// "RANGE/COMP/RUN" column that read like a value.
function FactGroup({ title, children }) {
  return (
    <div>
      <h3 className="label !mb-2">{title}</h3>
      <dl className="grid grid-cols-[8.5rem_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5 text-sm">{children}</dl>
    </div>
  )
}

const KV = ({ k, v, title }) => (
  <>
    <dt className="truncate text-[11px] font-semibold uppercase tracking-wider text-faint" title={title}>{k}</dt>
    <dd className="min-w-0 truncate font-mono text-xs text-muted" title={title ?? String(v ?? '—')}>{v ?? '—'}</dd>
  </>
)

// The run's server-side directory, offered as a copy button — it is a server path, not a
// fact to read, and the old truncated display could not even be copied whole.
function CopyPath({ path }) {
  const toast = useToast()
  if (!path) return null
  const copy = async () => {
    try { await navigator.clipboard.writeText(path); toast('Path copied') } catch { toast('Copy failed', 'error') }
  }
  return (
    <button onClick={copy} title={`Copy ${path}`}
      className="flex min-w-0 items-center gap-2 rounded-xl px-2 py-1 text-left font-mono text-[11px] text-faint transition hover:text-accent">
      <Icon name="packet" className="h-3 w-3 shrink-0" />
      <span className="truncate">{path}</span>
      <span className="shrink-0 text-[10px] uppercase tracking-wider">copy</span>
    </button>
  )
}

// Lifecycle + stage events as a rail — the raw event dump read as a sequence, each entry
// stamped with its offset from the run's start.
export function Timeline({ events = [], start }) {
  if (!events.length) return <p className="text-xs text-faint">Waiting for the run's first event.</p>
  return (
    <ol className="relative space-y-3 border-l-2 border-line pl-4">
      {events.map((ev) => {
        const text = eventText(ev)
        return (
          <li key={ev.seq} className="relative">
            <span className={`absolute -left-[21px] top-1.5 h-2 w-2 rounded-full ${ev.kind === 'stage' ? 'bg-accent' : 'bg-line'}`} />
            <div className="flex items-baseline gap-2 text-xs">
              <span className="min-w-0 truncate font-semibold text-fg" title={ev.stage}>{ev.stage || ev.state || ev.data?.state || ev.kind}</span>
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

// The engine log renders through the shared LogView (ANSI colors, folded repeats, log
// search, exact phase jumps); this wrapper only carries the panel caption.
export function Output({ log, height = '50vh', showPhases = true }) {
  return <LogView log={log} height={height} showPhases={showPhases} />
}

const fmtBytes = (n) => (n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)

// Order a reader wants: the report, then the team write-ups, then the raw harness capture.
const CATS = [
  ['Report', (rel, base) => ['REPORT.md', 'collection.json', 'index.json'].includes(base)],
  ['Blue', (rel, base) => base === 'BLUE-TEAM.md' || rel.startsWith('evidence/blue')],
  ['Red', (rel, base) => base === 'RED-TEAM.md' || rel.startsWith('evidence/red')],
  ['Harness', (rel, base) => rel.startsWith('evidence/harness') || base === '.deploy-timings.jsonl'],
]

function FileGroups({ rows, group, onPreview, onDownload }) {
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
              <li key={r.path} className="group flex items-center gap-1">
                <button onClick={() => onPreview(r.path)} title={`Preview ${rel(r.path)}`}
                  className="flex min-w-0 flex-1 items-center gap-2 rounded-xl px-2 py-1 text-left text-xs transition hover:bg-sunken">
                  <Icon name="packet" className="h-3.5 w-3.5 shrink-0 text-faint" />
                  <span className="min-w-0 truncate font-mono text-muted">{rel(r.path)}</span>
                  <span className="ml-auto shrink-0 tabular-nums text-faint">{fmtBytes(r.bytes)}</span>
                </button>
                <button onClick={() => onDownload(r.path, base(r.path))} title={`Download ${rel(r.path)}`}
                  aria-label={`Download ${r.path}`}
                  className="shrink-0 rounded-lg px-2 py-1 text-[11px] text-muted transition hover:bg-sunken hover:text-accent focus-visible:outline focus-visible:outline-accent">
                  Download
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

// runId selects the API workspace; range opts into the whole range's evidence tree.
// Directory names are descriptive labels, never assumed to be valid dashboard run IDs.
export function Evidence({ runId, range }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  const [q, setQ] = useState('')
  const [preview, setPreview] = useState(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true, pending = false
    setRows(null); setErr(null); setPreview(null)
    if (!runId) return () => { active = false }
    const load = async () => {
      if (pending) return
      pending = true
      try {
        const files = await api.runArtifacts(runId)
        if (active) { setRows(files); setErr(null) }
      } catch (e) { if (active) setErr(e) }
      finally { pending = false }
    }
    void load()
    const timer = setInterval(load, 10000)
    return () => { active = false; clearInterval(timer) }
  }, [runId, attempt])
  if (!runId) return <p className="text-sm text-faint">No run is available to read this workspace's evidence.</p>
  if (!rows) return err
    ? <ErrorBanner error={err} onRetry={() => setAttempt((a) => a + 1)} />
    : <div className="flex justify-center p-10"><Spinner /></div>
  const rangeMode = !!range
  const filtered = q.trim() ? rows.filter((r) => r.path.toLowerCase().includes(q.trim().toLowerCase())) : rows
  const groups = new Map()
  filtered.forEach((r) => {
    const group = r.path.includes('/') ? r.path.split('/')[0] : ''
    if (!groups.has(group)) groups.set(group, [])
    groups.get(group).push(r)
  })
  const mine = groups.get(runId) || []
  const others = [...groups].filter(([g]) => g !== runId)
  // The API needs the bearer header, so a row fetches the file and hands the blob to the browser.
  const grab = async (path, name) => {
    try {
      const blob = await api.runArtifactFile(runId, path)
      const a = document.createElement('a')
      const url = URL.createObjectURL(blob)
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { setErr(e) }
  }
  // Browsers throttle programmatic downloads; a small gap keeps "download all" from dropping
  // files when a run's evidence carries a couple hundred of them.
  const grabAll = async (list) => {
    for (const r of list) {
      await grab(r.path, r.path.split('/').pop())
      await new Promise((res) => setTimeout(res, 250))
    }
  }
  const totalFiles = filtered.length
  return (
    <div className="rise space-y-6 pb-6">
      <ErrorBanner error={err} onRetry={() => setAttempt((a) => a + 1)} />
      <Card className="p-6">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <h2 className="label !mb-0">{rangeMode ? 'Range evidence' : 'This run'}</h2>
          <span className="font-mono text-[11px] text-faint">{rangeMode ? filtered.length : mine.length} files</span>
          <div className="ml-auto flex items-center gap-2">
            <SearchInput value={q} onChange={setQ} placeholder="Filter by path" className="w-56" />
            {(rangeMode ? filtered : mine).length > 1 && (
              <Button onClick={() => grabAll(rangeMode ? filtered : mine)}><Icon name="packet" />{q.trim() ? 'Download matching' : 'Download all'}</Button>
            )}
          </div>
        </div>
        {totalFiles !== rows.length && (
          <p className="mb-3 text-xs text-faint">{totalFiles} of {rows.length} files match “{q.trim()}”.</p>
        )}
        {rangeMode ? (
          groups.size ? <div className="space-y-6">{[...groups].map(([g, list]) => (
            <section key={g}>
              <h3 className="mb-2 break-all font-mono text-xs font-semibold text-muted">{g || 'Workspace root'} · {list.length} files</h3>
              <FileGroups rows={list} group={g} onPreview={(p) => setPreview({ path: p })} onDownload={grab} />
            </section>
          ))}</div> : <p className="text-sm text-faint">{q.trim() ? 'No evidence files match this filter.' : 'No evidence collected in this workspace yet.'}</p>
        ) : mine.length
          ? <FileGroups rows={mine} group={runId} onPreview={(p) => setPreview({ path: p })} onDownload={grab} />
          : <p className="text-sm text-faint">No evidence directory named <span className="font-mono">{runId}</span> yet. Workspace directories below may use engine-generated names rather than dashboard run IDs.</p>}
      </Card>
      {!rangeMode && others.length > 0 && (
        <Card className="p-6">
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-muted">Other evidence directories in this workspace ({others.length})</summary>
            <div className="mt-4 space-y-6">
              {others.map(([g, list]) => (
                <div key={g}>
                  <div className="mb-2 flex items-baseline gap-2 font-mono text-xs font-semibold text-muted">
                    {g || 'Workspace root'} <span className="text-faint">· {list.length}</span>
                    {list.length > 1 && (
                      <button onClick={() => grabAll(list)} className="ml-auto text-[11px] font-normal text-faint underline-offset-2 transition hover:text-accent hover:underline">
                        download all
                      </button>
                    )}
                  </div>
                  <FileGroups rows={list} group={g} onPreview={(p) => setPreview({ path: p })} onDownload={grab} />
                </div>
              ))}
            </div>
          </details>
        </Card>
      )}
      <ArtifactPreview runId={runId} file={preview} onClose={() => setPreview(null)} onDownload={(p) => grab(p, p.split('/').pop())} />
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
  const [attempt, setAttempt] = useState(0)
  const [newRun, setNewRun] = useState(false)
  const [resetBox, setResetBox] = useState(null) // null = closed, {} | {team} = prefill
  const log = useRunLog(id)
  const refresh = useRef(null)
  useTick(1000)
  const reload = () => refresh.current?.()
  const { submit, destroy, cancel, pause, resume, live, held, canPause } = useRunActions(run, reload, inventory?.range)
  useEffect(() => { setEvents([]) }, [id])
  const { updated, live: streamLive } = useRunEvents(id, (ev) => {
    setEvents((e) => (e.some((x) => x.seq === ev.seq) ? e : [...e, ev].sort((a, b) => (a.seq || 0) - (b.seq || 0))))
    reload()
  })

  useEffect(() => {
    let active = true, pending = null
    setRun(null); setComp(null); setInventory(null); setErr(null)
    const load = () => {
      if (pending) return pending
      pending = Promise.all([
        api.run(id).then((r) => { if (active) { setRun(r); setErr(null) } })
          .catch((e) => { if (active) setErr(e) }),
        api.runInventory(id).then((r) => { if (active) setInventory(r) }).catch(() => {}),
      ]).finally(() => { pending = null })
      return pending
    }
    refresh.current = load
    void load()
    // Range inventory changes during deploy/teardown AND during follow-up runs, even
    // when this diagnostic run is terminal. Do not leave it frozen at the first fetch.
    const timer = setInterval(load, 5000)
    return () => { active = false; refresh.current = null; clearInterval(timer) }
  }, [id, attempt])
  useEffect(() => {
    if (!run?.comp_id) return
    let active = true
    api.comp(run.comp_id).then((c) => { if (active) setComp(c) }).catch(() => {})
    return () => { active = false }
  }, [run?.comp_id])
  useEffect(() => {
    document.title = run
      ? `${comp?.name || run.comp_id} · ${ACTION_LABEL[run.action] || run.action} · tezcatlipoca`
      : 'tezcatlipoca'
  }, [run, comp])

  if (err) {
    // The first fetch of a deep link occasionally races the service (a fresh "Open run"
    // landed on a raw "no such run" / 500 that a reload cured) — offer the retry inline.
    return <div className="p-10"><ErrorBanner error={err} onRetry={() => { setErr(null); setAttempt((a) => a + 1) }} /></div>
  }
  if (!run) return <div className="flex justify-center p-10"><Spinner /></div>

  const boxes = comp?.boxes || []
  const range = inventory?.range || null
  // A live range has no teardown journal at all; the journal line exists only once a destroy
  // touched the range, and there red01's absence is what separates "retired" from "a VM
  // holding the LLM key is still up".
  const teardown = inventory?.teardown?.run_id || inventory?.teardown?.done?.length ? inventory.teardown : null
  const teams = range?.teams ?? '—'
  const runFacts = [
    { label: 'Action', value: ACTION_LABEL[run.action] || run.action },
    { label: 'Engine ref', value: run.engine_ref || 'estate-v0.3.0' },
    { label: 'rc', value: run.returncode ?? '—' },
    { label: 'Created', value: agoMs(run.created_at) || '—' },
    { label: 'Started', value: agoMs(run.started_at || run.created_at) || '—' },
    { label: 'Ended', value: run.ended_at ? agoMs(run.ended_at) : '—' },
    { label: 'Duration', value: fmtSpan(run.started_at || run.created_at, run.ended_at) },
  ]
  const rangeFacts = [
    { label: 'Teams', value: teams, title: 'Team count the range was deployed with (its deploy params)' },
    { label: 'Range', value: range?.state?.replace('_', ' ') ?? '—', title: 'building, live, retired, teardown_failed or failed — as the range last reported it' },
    { label: 'Deploy run', value: range?.deploy_run_id ?? '—', title: 'The run that deployed this range', link: range?.deploy_run_id },
    { label: 'Teardown', value: teardown ? `${teardown.run_id || '—'} · red01 ${teardown.red01_done ? 'down' : 'still up'}` : '—', title: teardown ? `Journal steps done: ${(teardown.done || []).join(', ') || 'none'}` : 'No teardown journal in this range workspace' },
  ]
  const compFacts = [
    { label: 'Boxes', value: comp?.boxes?.length ?? '—' },
    { label: 'Services', value: comp?.services ?? '—' },
    { label: 'Box names', value: boxes.map((b) => b.name).join(', ') || '—' },
  ]
  const FactList = ({ facts }) => (
    <dl className="space-y-1.5">
      {facts.map((f) => (
        <div key={f.label} className="flex items-baseline justify-between gap-3">
          <dt className="shrink-0 text-[11px] font-semibold uppercase tracking-wider text-faint" title={f.title}>{f.label}</dt>
          <dd className="min-w-0 truncate text-right font-mono text-xs text-muted" title={f.title ?? String(f.value ?? '—')}>
            {f.link && f.value && f.value !== '—'
              ? <Link to={`/c/${run.comp_id}/runs/${f.link}`} className="text-accent hover:underline">{f.value}</Link>
              : (f.value ?? '—')}
          </dd>
        </div>
      ))}
    </dl>
  )

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
          {range?.id && <Link to={`/ranges/${range.id}`} className="ml-auto shrink-0 text-sm font-semibold text-accent hover:underline">Open range</Link>}
          {range?.state === 'teardown_failed' && (
            <span className="chip shrink-0 bg-danger-soft px-2.5 py-1 text-danger" title="The estate's own machines are gone, but a VM it never owned survived">range teardown failed</span>
          )}
        </div>
        {/* One row, clipped rather than wrapped: a raggedy header was the old page's worst tell. */}
        <div className="mt-2 flex min-w-0 items-center gap-x-3 overflow-hidden whitespace-nowrap font-mono text-xs text-faint">
          <span className="min-w-0 truncate" title={run.id}>{run.id}</span>
          <span className="shrink-0" title="Engine ref">{run.engine_ref || 'estate-v0.3.0'}</span>
          <span className="shrink-0">rc {run.returncode ?? '—'}</span>
          <span className="shrink-0">started {agoMs(run.started_at || run.created_at) || '—'}</span>
          <span className="shrink-0">elapsed {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
          <span className="ml-auto flex shrink-0 items-center gap-1.5" title={streamLive ? 'The live event stream is connected' : 'Last event or poll'}>
            <span className={`h-1.5 w-1.5 rounded-full ${streamLive || Date.now() - (updated || 0) < 15000 ? 'animate-pulse bg-accent' : 'bg-line'}`} />
            {updated ? `updated ${agoMs(updated)}` : streamLive ? 'live' : '—'}
          </span>
        </div>
      </header>

      {tab === 'overview' && (
        <>
          <CompetitionPanel
            rangeKey={range?.key || run.params?.comp_dir || run.params?._destroy_comp_dir || (run.run_dir ? `${run.run_dir}/comp` : '')}
            compId={range?.comp_id || run.comp_id} engineRef={range?.engine_ref || run.engine_ref}
            onChanged={reload} show={['succeeded', 'failed', 'cancelled'].includes(run.state)} />
          <Card className="p-6">
            <h2 className="label !mb-3">General</h2>
            <div className="min-h-[5.5rem]">
              <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
                stageEvents={events.filter((e) => e.kind === 'stage')} />
            </div>
            <div className="mt-5 grid gap-x-10 gap-y-5 md:grid-cols-2">
              <FactGroup title="From this run">
                <FactList facts={runFacts} />
              </FactGroup>
              <div className="space-y-5">
                <FactGroup title="From the range">
                  <FactList facts={rangeFacts} />
                </FactGroup>
                <FactGroup title="From the competition">
                  <FactList facts={compFacts} />
                </FactGroup>
              </div>
            </div>
            <CopyPath path={run.run_dir} />
            {range?.state === 'teardown_failed' && (
              <div className="mt-4 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger">
                Range teardown failed: the estate's own machines are gone but a VM survives
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

      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps} allowedActions={['deploy']}
        initial={{
          comp_id: run.comp_id, action: 'deploy', engine_ref: run.engine_ref || 'estate-v0.3.0',
          teams: range?.teams ?? run.params?.teams ?? 2, scoring_vmid: run.params?.scoring_vmid ?? '',
        }}
        onSubmitted={reload} />
      <ResetBoxModal open={resetBox !== null} onClose={() => setResetBox(null)} run={run} range={range}
        teams={inventory?.teams} initial={resetBox || {}} onSubmitted={reload} />
    </div>
  )
}

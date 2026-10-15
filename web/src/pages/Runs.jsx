// Every run the service has executed: filterable table plus a detail drawer with the
// stage timeline, event list, live log and collected artifacts.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, SearchInput, Spinner } from '../components/ui'
import { EmptyState, Icon, PageHeader, agoMs, fmtSpan, useTick } from '../components/kit'
import {
  ACTION_LABEL, ArtifactsList, EventList, LogView, StageStrip, StatePill, stageLabel,
  useRunActions, useRunEvents, useRunLog,
} from '../components/runui'

const FILTERS = ['all', 'queued', 'running', 'pausing', 'paused', 'succeeded', 'failed', 'cancelled']
const terminal = (s) => s === 'succeeded' || s === 'failed' || s === 'cancelled'

// A row: state pill, identity (comp, run id), the prettified stage, timing — and, for a
// terminal failure, the stored error spelled out underneath, so eight identical-looking
// "failed · rc 1" rows stop hiding eight different causes.
function Row({ r, selected, onPick }) {
  const err = terminal(r.state) && r.error ? String(r.error) : ''
  return (
    <>
      <tr onClick={() => onPick(r.id)} className={`cursor-pointer transition hover:bg-sunken ${selected ? 'bg-sunken' : ''}`}>
        <td className="px-4 py-2.5"><StatePill state={r.state} /></td>
        <td className="px-4 py-2.5 font-mono text-sm">{r.comp_id}</td>
        <td className="px-4 py-2.5 font-mono text-xs text-muted" title={r.id}>{r.id.replace(/^run-/, '')}</td>
        <td className="px-4 py-2.5 text-sm text-muted">{ACTION_LABEL[r.action] || r.action}</td>
        <td className="max-w-40 truncate px-4 py-2.5 text-xs text-muted" title={r.stage || ''}>{stageLabel(r) || '—'}</td>
        <td className="px-4 py-2.5 text-xs text-muted">{agoMs(r.started_at || r.created_at) || '—'}</td>
        <td className="px-4 py-2.5 text-xs tabular-nums text-muted">{fmtSpan(r.started_at || r.created_at, r.ended_at)}</td>
        <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-muted">{r.returncode ?? '—'}</td>
      </tr>
      {err && (
        <tr className="cursor-pointer hover:bg-sunken" onClick={() => onPick(r.id)}>
          <td colSpan={8} className="pb-2.5 pl-14 pr-4">
            <span className="flex items-start gap-2 text-xs text-danger" title={err}>
              <Icon name="warn" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="line-clamp-2">{err}</span>
            </span>
          </td>
        </tr>
      )}
    </>
  )
}

// Retries repeat: N quick failures with the same comp/action/state/exit and no error text
// collapse into one row with a ×N badge until clicked. Only genuinely identical micro-runs
// fold — anything with a distinct error stays on its own row.
const durMs = (r) => ((r.ended_at || Date.now() / 1000) - (r.started_at || r.created_at)) * 1000

function collapseRuns(rows) {
  const groups = []
  for (const r of rows) {
    const g = groups[groups.length - 1]
    const first = g?.runs[0]
    if (
      first && terminal(r.state) && durMs(r) < 15000 && durMs(first) < 15000
      && first.comp_id === r.comp_id && first.action === r.action && first.state === r.state
      && (first.error || '') === (r.error || '') && (first.returncode ?? '') === (r.returncode ?? '')
    ) g.runs.push(r)
    else groups.push({ runs: [r] })
  }
  return groups
}

function GroupRow({ g, selected, onPick }) {
  const [open, setOpen] = useState(false)
  const r = g.runs[0]
  const n = g.runs.length
  const body = (
    <>
      <tr onClick={() => onPick(r.id)} className={`cursor-pointer transition hover:bg-sunken ${selected ? 'bg-sunken' : ''}`}>
        <td className="px-4 py-2.5"><StatePill state={r.state} /></td>
        <td className="px-4 py-2.5 font-mono text-sm">{r.comp_id}</td>
        <td className="px-4 py-2.5">
          <button onClick={(e) => { e.stopPropagation(); setOpen((o) => !o) }} title={open ? 'Show each attempt' : `${n} identical quick attempts — click to expand`}
            className="chip bg-danger-soft px-2 py-0.5 text-[10px] font-semibold text-danger">
            ×{n}
          </button>
        </td>
        <td className="px-4 py-2.5 text-sm text-muted">{ACTION_LABEL[r.action] || r.action}</td>
        <td className="max-w-40 truncate px-4 py-2.5 text-xs text-muted" title={r.stage || ''}>{stageLabel(r) || '—'}</td>
        <td className="px-4 py-2.5 text-xs text-muted">{agoMs(g.runs[n - 1].started_at || g.runs[n - 1].created_at) || '—'}</td>
        <td className="px-4 py-2.5 text-xs tabular-nums text-muted">{fmtSpan(g.runs[n - 1].started_at || g.runs[n - 1].created_at, g.runs[n - 1].ended_at)}</td>
        <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-muted">{r.returncode ?? '—'}</td>
      </tr>
      {open && g.runs.map((x) => <Row key={x.id} r={x} selected={selected === x.id} onPick={onPick} />)}
    </>
  )
  return body
}

function Drawer({ runId, onClose }) {
  useTick(1000)
  const [run, setRun] = useState(null)
  const [events, setEvents] = useState([])
  const [err, setErr] = useState(null)
  const log = useRunLog(runId)
  const { updated, live } = useRunEvents(runId, (ev) => {
    if (ev.seq != null) setEvents((e) => (e.some((x) => x.seq === ev.seq) ? e : [...e, ev].sort((a, b) => (a.seq || 0) - (b.seq || 0))))
    api.run(runId).then(setRun).catch(() => {})
  })

  useEffect(() => {
    setRun(null); setEvents([]); setErr(null)
    api.run(runId).then(setRun).catch(setErr)
    api.runEvents(runId, 0).then((r) => setEvents(r.events || [])).catch(() => {})
  }, [runId])

  // The shared action row: without it the pause/resume controls would be duplicated here.
  // runLive: this hook's live is "a run is in flight"; the stream live above is the SSE link.
  const { cancel, pause, resume, live: runLive, held, canPause } = useRunActions(run, () => {
    api.run(runId).then(setRun).catch(() => {})
  })

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-bg/60 backdrop-blur-sm" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="panel h-full w-[min(46rem,100vw)] overflow-auto rounded-r-none border-r-0 p-8">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="display text-3xl">{run ? `${ACTION_LABEL[run.action] || run.action} · ${run.comp_id}` : 'Run'}</h2>
              {run && <StatePill state={run.state} />}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-faint">
              <span>{runId}</span>
              {run && <span title="Engine ref">{run.engine_ref || 'estate-v0.3.0'}</span>}
              {run && <span>rc {run.returncode ?? '—'}</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {canPause && <Button onClick={pause}><Icon name="pause" />Pause after next stage</Button>}
            {held && <Button variant="primary" onClick={resume}><Icon name="play" />Resume</Button>}
            {runLive && <Button variant="danger" onClick={cancel}><Icon name="warn" />Cancel</Button>}
            <button onClick={onClose} className="btn btn-ghost h-9 w-9 !p-0 text-faint" aria-label="Close">✕</button>
          </div>
        </div>

        <ErrorBanner error={err} />
        {!run && !err && <div className="flex justify-center p-10"><Spinner /></div>}

        {run && (
          <div className="mt-8 space-y-8">
            <section>
              <h3 className="label">Stages</h3>
              <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
                stageEvents={events.filter((e) => e.kind === 'stage')} />
            </section>

            <section className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              {[
                ['Created', agoMs(run.created_at) || '—'],
                ['Started', agoMs(run.started_at) || '—'],
                ['Ended', run.ended_at ? agoMs(run.ended_at) : '—'],
                ['Duration', fmtSpan(run.started_at || run.created_at, run.ended_at)],
                ['Updated', live ? 'live' : updated ? agoMs(updated) : '—'],
                ['Run dir', run.run_dir || '—'],
              ].map(([k, v]) => (
                <div key={k} className="flex items-baseline justify-between gap-3 rounded-2xl bg-sunken px-4 py-2.5 shadow-inset">
                  <span className="text-xs font-semibold uppercase tracking-wider text-faint">{k}</span>
                  <span className="truncate font-mono text-xs text-muted" title={String(v)}>{String(v)}</span>
                </div>
              ))}
              {run.error && (
                <div className="col-span-2 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger">{String(run.error)}</div>
              )}
            </section>

            <section>
              <h3 className="label">Events</h3>
              <div className="max-h-72 overflow-auto rounded-2xl bg-sunken p-3 shadow-inset">
                <EventList events={events} />
              </div>
            </section>

            <section>
              <h3 className="label">Log</h3>
              <LogView log={log} height="42vh" />
            </section>

            <section>
              <h3 className="label">Artifacts</h3>
              <ArtifactsList runId={runId} />
            </section>
          </div>
        )}
      </aside>
    </div>
  )
}

export default function Runs() {
  const [filter, setFilter] = useState('all')
  const [runs, setRuns] = useState(null)
  const [err, setErr] = useState(null)
  const [sel, setSel] = useState(null)
  const [q, setQ] = useState('')
  const [comp, setComp] = useState('')
  const refresh = useCallback(
    () => api.runs(filter === 'all' ? undefined : filter).then(setRuns).catch(setErr),
    [filter],
  )
  useEffect(() => {
    setRuns(null)
    refresh()
    const t = setInterval(refresh, 8000)
    return () => clearInterval(t)
  }, [refresh])

  const comps = useMemo(() => [...new Set((runs || []).map((r) => r.comp_id))].sort(), [runs])
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const filtered = (runs || []).filter((r) =>
      (!comp || r.comp_id === comp)
      && (!needle || `${r.comp_id} ${r.id} ${r.action} ${r.engine_ref || ''} ${r.stage || ''} ${r.error || ''}`.toLowerCase().includes(needle)))
    return collapseRuns(filtered)
  }, [runs, q, comp])

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <TopNav />
      <PageHeader title="Runs" sub="Every deploy, plan, verify, scrim, redeploy and teardown this service has run" />
      <div className="rise rise-1 mb-6 flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${filter === f ? 'bg-accent-soft text-accent' : 'bg-sunken text-muted hover:text-fg'}`}>
            {f}
          </button>
        ))}
        <select value={comp} onChange={(e) => setComp(e.target.value)}
          className="field !w-auto !rounded-full !py-1.5 text-xs" aria-label="Filter by competition">
          <option value="">All competitions</option>
          {comps.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <SearchInput value={q} onChange={setQ} placeholder="Search runs" className="w-64" />
      </div>
      <ErrorBanner error={err} onRetry={() => { setErr(null); refresh() }} />
      <Card className="rise rise-2 overflow-hidden p-2">
        {!runs && !err && <div className="flex justify-center p-10"><Spinner /></div>}
        {runs && !runs.length && (
          <EmptyState icon="terminal" title={`No ${filter === 'all' ? '' : filter + ' '}runs`}>
            Runs appear here as soon as they are submitted from the Live page or the comp editor.
          </EmptyState>
        )}
        {runs && runs.length > 0 && (
          <table className="w-full text-left">
            <thead>
              <tr className="text-[11px] font-semibold uppercase tracking-wider text-faint">
                <th className="px-4 py-2">State</th>
                <th className="px-4 py-2">Comp</th>
                <th className="px-4 py-2">Run</th>
                <th className="px-4 py-2">Action</th>
                <th className="px-4 py-2">Stage</th>
                <th className="px-4 py-2">Started</th>
                <th className="px-4 py-2">Duration</th>
                <th className="px-4 py-2">rc</th>
              </tr>
            </thead>
            <tbody className="divide-y-2 divide-line">
              {groups.map((g, i) => (g.runs.length === 1
                ? <Row key={g.runs[0].id} r={g.runs[0]} selected={sel === g.runs[0].id} onPick={setSel} />
                : <GroupRow key={`${g.runs[0].id}-${i}`} g={g} selected={sel && g.runs.some((x) => x.id === sel)} onPick={setSel} />))}
            </tbody>
          </table>
        )}
      </Card>
      {sel && <Drawer runId={sel} onClose={() => setSel(null)} />}
    </div>
  )
}

// Every run the service has executed: filterable table plus a detail drawer with the
// stage timeline, event list, live log and collected artifacts.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, Spinner } from '../components/ui'
import { EmptyState, Icon, PageHeader, agoMs, fmtSpan, useConfirm, useTick, useToast } from '../components/kit'
import {
  ACTION_LABEL, ArtifactsList, EventList, LogView, StageStrip, StatePill,
  useRunEvents, useRunLog,
} from '../components/runui'

const FILTERS = ['all', 'queued', 'running', 'succeeded', 'failed', 'cancelled']

function Row({ r, selected, onPick }) {
  return (
    <tr onClick={() => onPick(r.id)} className={`cursor-pointer transition hover:bg-sunken ${selected ? 'bg-sunken' : ''}`}>
      <td className="px-4 py-2.5"><StatePill state={r.state} /></td>
      <td className="px-4 py-2.5 font-mono text-sm">{r.comp_id}</td>
      <td className="px-4 py-2.5 text-sm text-muted">{ACTION_LABEL[r.action] || r.action}</td>
      <td className="px-4 py-2.5 font-mono text-xs text-muted">{r.engine_ref || 'main'}</td>
      <td className="max-w-40 truncate px-4 py-2.5 text-xs text-muted" title={r.stage || ''}>{r.stage || '—'}</td>
      <td className="px-4 py-2.5 text-xs text-muted">{agoMs(r.started_at || r.created_at) || '—'}</td>
      <td className="px-4 py-2.5 text-xs tabular-nums text-muted">{fmtSpan(r.started_at || r.created_at, r.ended_at)}</td>
      <td className="px-4 py-2.5 font-mono text-xs tabular-nums text-muted">{r.returncode ?? '—'}</td>
    </tr>
  )
}

function Drawer({ runId, onClose }) {
  useTick(1000)
  const [run, setRun] = useState(null)
  const [events, setEvents] = useState([])
  const [err, setErr] = useState(null)
  const ask = useConfirm()
  const toast = useToast()
  const log = useRunLog(runId)
  const { updated } = useRunEvents(runId, (ev) => {
    if (ev.seq != null) setEvents((e) => (e.some((x) => x.seq === ev.seq) ? e : [...e, ev].sort((a, b) => (a.seq || 0) - (b.seq || 0))))
    api.run(runId).then(setRun).catch(() => {})
  })

  useEffect(() => {
    setRun(null); setEvents([]); setErr(null)
    api.run(runId).then(setRun).catch(setErr)
    api.runEvents(runId, 0).then((r) => setEvents(r.events || [])).catch(() => {})
  }, [runId])

  const live = run && (run.state === 'running' || run.state === 'queued')
  const cancel = async () => {
    const ok = await ask({
      title: 'Cancel this run?', danger: true, confirm: 'Cancel run',
      body: "Kills the run's process group. The range may be left half-built.",
    })
    if (!ok) return
    try { await api.cancelRun(runId); toast('Cancel requested') } catch (e) { toast(e.message, 'error') }
  }

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
              {run && <span title="Engine ref">{run.engine_ref || 'main'}</span>}
              {run && <span>rc {run.returncode ?? '—'}</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {live && <Button variant="danger" onClick={cancel}><Icon name="warn" />Cancel</Button>}
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
                ['Updated', updated ? agoMs(updated) : '—'],
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
      </div>
      <ErrorBanner error={err} />
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
                <th className="px-4 py-2">Action</th>
                <th className="px-4 py-2">Ref</th>
                <th className="px-4 py-2">Stage</th>
                <th className="px-4 py-2">Started</th>
                <th className="px-4 py-2">Duration</th>
                <th className="px-4 py-2">rc</th>
              </tr>
            </thead>
            <tbody className="divide-y-2 divide-line">
              {runs.map((r) => <Row key={r.id} r={r} selected={sel === r.id} onPick={setSel} />)}
            </tbody>
          </table>
        )}
      </Card>
      {sel && <Drawer runId={sel} onClose={() => setSel(null)} />}
    </div>
  )
}

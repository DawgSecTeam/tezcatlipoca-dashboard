// The operator's home: one card per RANGE — the unit is the infrastructure, not the run
// attempt. A range boards while it builds or is up and leaves the moment a destroy of it
// succeeds; the destroy is its tombstone line in the retired strip. Dead attempts (a build
// that never came up, an old failed scrim) are history and live on /runs.
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner } from '../components/ui'
import { EmptyState, Icon, PageHeader, agoMs, fmtSpan, toMs, useTick } from '../components/kit'
import { ACTION_LABEL, NewRunModal, StageStrip, useRunEvents } from '../components/runui'

// ── one range card ─────────────────────────────────────────────────────────────────────────

function RangeCard({ range, comp }) {
  const run = range.runs?.[0] // the store returns newest first
  const running = !!run && ['running', 'queued', 'pausing', 'paused'].includes(run.state)
  // Only a live run's clock moves; a finished card needs a nudge just for its relative times.
  useTick(running ? 1000 : 60000)
  const [stageEvents, setStageEvents] = useState([])
  // No stream for a terminal run: its history is already in the store, so SSE would cost a
  // connection and a render per second per long-dead card.
  useRunEvents(run?.id, (ev) => {
    if (ev.kind === 'stage') setStageEvents((s) => (s.some((x) => x.seq === ev.seq) ? s : [...s, ev]))
  }, running)

  // Past stage events (for per-stage elapsed) come from the cursor endpoint; the live ones
  // arrive over SSE.
  useEffect(() => {
    if (!run) return
    setStageEvents([])
    api.runEvents(run.id, 0)
      .then((r) => setStageEvents((r.events || []).filter((e) => e.kind === 'stage')))
      .catch(() => {})
  }, [run?.id])

  const started = run && (run.started_at || run.created_at)
  const meta = run && [run.id, run.engine_ref || 'main', `started ${agoMs(started) || '—'}`, `elapsed ${fmtSpan(started, run.ended_at)}`]
  const why = run?.state === 'failed' && (run.error ? String(run.error) : `rc ${run.returncode ?? '—'}`)
  // teardown_failed is the board's one card that needs a human: the range's terraform is gone
  // but something survived the destroy, so it must not wear a healthy range's chip.
  const chip = range.state === 'live' ? 'bg-accent-soft text-accent'
    : range.state === 'teardown_failed' ? 'bg-danger-soft text-danger' : 'bg-sunken text-muted'

  return (
    <Card className="rise flex h-full flex-col p-7">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2.5">
          <Link to={`/c/${range.comp_id}`} className="min-w-0 truncate text-lg font-semibold transition hover:text-accent">
            {comp?.name || range.comp_id}
          </Link>
          <span className={`chip px-2.5 py-1 ${chip}`}>{range.state.replace('_', ' ')}</span>
          <span className="chip bg-sunken px-2.5 py-1 font-mono text-muted">
            {ACTION_LABEL[run?.action] || run?.action || '—'}
          </span>
        </div>
        {meta && <div className="mt-1 truncate font-mono text-xs text-faint" title={meta.join(' · ')}>{meta.join(' · ')}</div>}
      </div>

      {/* A fixed floor makes a live run's expanded strip and a finished run's collapsed bar
          the same height, so every card in the grid matches. */}
      <div className="mt-5 min-h-[5.5rem]">
        {run
          ? <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
              stageEvents={stageEvents} />
          : <div className="text-xs text-faint">No runs recorded for this range</div>}
        {why && <div className="mt-1 truncate text-xs text-danger" title={why}>{why}</div>}
      </div>

      {/* teams comes from the range's own deploy params, not the latest run's form defaults —
          a destroy's params carry no teams and would read as "—". */}
      <div className="mb-5 mt-4 flex items-center gap-4 text-xs text-muted">
        <span className="flex items-center gap-1.5">
          <Icon name="boxes" className="h-3.5 w-3.5 text-faint" />
          <b className="font-semibold tabular-nums">{range.teams ?? '—'}</b> team{Number(range.teams) === 1 ? '' : 's'}
          {' · '}<b className="font-semibold tabular-nums">{comp?.boxes ?? '—'}</b> boxes
          {' · '}<b className="font-semibold tabular-nums">{comp?.services ?? '—'}</b> services
        </span>
        {running && <span className="ml-auto h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" title="The latest run is running" />}
      </div>

      {run && (
        <Link to={`/c/${range.comp_id}/runs/${run.id}`}
          className={`btn mt-auto w-full ${range.state === 'teardown_failed' ? 'btn-danger' : 'btn-primary'}`}>
          <Icon name="overview" />Open run
        </Link>
      )}
    </Card>
  )
}

// ── page ───────────────────────────────────────────────────────────────────────────────────

export default function Live() {
  const [ranges, setRanges] = useState(null)
  const [comps, setComps] = useState(null)
  const [err, setErr] = useState(null)
  const [newRun, setNewRun] = useState(false)
  const refresh = useCallback(async () => {
    try { setRanges(await api.ranges()); setErr(null) } catch (e) { setErr(e) }
  }, [])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10000)
    return () => clearInterval(t)
  }, [refresh])
  // Names and specs don't change while the board is open — one fetch, not one per poll.
  useEffect(() => { api.comps().then(setComps).catch(() => {}) }, [])

  const compMap = Object.fromEntries((comps || []).map((c) => [c.id, c]))
  // An incomplete teardown is the one card that needs a human, so it floats above the healthy
  // ones; the API's newest-activity order holds within each group (Array.sort is stable).
  const cards = (ranges || []).filter((r) => r.live)
    .sort((a, b) => (b.state === 'teardown_failed') - (a.state === 'teardown_failed'))
  const retired = (ranges || []).filter((r) => r.state === 'retired')
    .sort((a, b) => (toMs(b.retired_at) || 0) - (toMs(a.retired_at) || 0))
  const up = cards.filter((r) => r.state === 'live').length
  const building = cards.filter((r) => r.state === 'building').length
  const dirty = cards.filter((r) => r.state === 'teardown_failed').length
  // A failed teardown is not "up" — it is counted on its own so the header never reads as health.
  const sub = `${up} range${up === 1 ? '' : 's'} up · ${building} building`
    + (dirty ? ` · ${dirty} teardown${dirty === 1 ? '' : 's'} failed` : '')

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <TopNav />
      <PageHeader title="Active ranges"
        sub={ranges ? sub : undefined}
        actions={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>} />
      <ErrorBanner error={err} />
      {!ranges && !err && <div className="grid auto-rows-fr grid-cols-1 gap-6 xl:grid-cols-2">{[0, 1].map((i) => <div key={i} className="panel h-72 animate-pulse opacity-60" />)}</div>}
      {ranges && !cards.length && (
        <EmptyState icon="rocket" title="No active ranges"
          action={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>}>
          A range boards here while it builds and while it is up, and leaves when a destroy of it succeeds.
        </EmptyState>
      )}
      <div className="grid auto-rows-fr grid-cols-1 gap-6 xl:grid-cols-2">
        {cards.map((r) => <RangeCard key={r.key} range={r} comp={compMap[r.comp_id]} />)}
      </div>
      {!!retired.length && (
        <div className="mt-10">
          <h2 className="label">Recently retired</h2>
          <ul className="space-y-1.5">
            {retired.slice(0, 5).map((r) => (
              <li key={r.key}>
                <Link to={r.retired_by ? `/c/${r.comp_id}/runs/${r.retired_by}` : `/c/${r.comp_id}`}
                  className="flex items-baseline gap-3 text-xs text-faint transition hover:text-accent">
                  <span className="w-64 min-w-0 shrink truncate">{compMap[r.comp_id]?.name || r.comp_id}</span>
                  <span className="shrink-0 font-mono">{r.retired_by || '—'}</span>
                  <span className="shrink-0">{agoMs(r.retired_at) || '—'}</span>
                </Link>
              </li>
            ))}
          </ul>
          {retired.length > 5 && <div className="mt-1.5 text-xs text-faint">+{retired.length - 5} more</div>}
        </div>
      )}
      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
        onSubmitted={() => { setErr(null); refresh() }} />
    </div>
  )
}

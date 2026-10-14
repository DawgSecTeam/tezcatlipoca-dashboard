// The operator's home: one card per scrim attempt — identity, stage timeline, usage and
// the action menu; the Open button leads to the run's full management page. Dry runs
// don't board here (they live on the Runs page).
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, Modal } from '../components/ui'
import { EmptyState, Icon, PageHeader, agoMs, fmtSpan, toMs, useTick } from '../components/kit'
import {
  ACTION_LABEL, ArtifactsList, NewRunModal, ResetBoxModal,
  StageStrip, StatePill, useRunActions, useRunEvents,
} from '../components/runui'

const RECENT_MS = 6 * 3600 * 1000 // a finished run stays on the board this long

const isOnBoard = (r) => {
  if (r.state === 'running' || r.state === 'queued') return true
  const end = toMs(r.ended_at) || toMs(r.started_at) || toMs(r.created_at)
  return end && Date.now() - end < RECENT_MS
}

// ── kebab menu ─────────────────────────────────────────────────────────────────────────────

function MenuItem({ icon, label, onClick, danger, disabled, title }) {
  return (
    <button onClick={disabled ? undefined : onClick} disabled={disabled} title={title}
      className={`flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-sm transition ${
        disabled ? 'cursor-not-allowed text-faint opacity-60'
          : danger ? 'text-danger hover:bg-danger-soft' : 'text-muted hover:bg-sunken hover:text-fg'}`}>
      <Icon name={icon} className="h-4 w-4 shrink-0" />{label}
    </button>
  )
}

function RunMenu({ run, comps, onChanged }) {
  const [open, setOpen] = useState(false)
  const [resetBox, setResetBox] = useState(false)
  const [artifacts, setArtifacts] = useState(false)
  const [newRun, setNewRun] = useState(false)
  const navigate = useNavigate()
  const { submit, destroy, cancel, live } = useRunActions(run, onChanged)

  return (
    <div className="relative">
      <button onClick={() => setOpen(!open)} aria-label="Run actions" title="Run actions"
        className="btn h-9 w-9 !p-0 text-faint"><Icon name="dots" /></button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onMouseDown={() => setOpen(false)} />
          <div className="tile absolute right-0 top-full z-30 mt-2 w-64 p-2">
            <MenuItem icon="rocket" label="New run (re-deploy)" onClick={() => { setOpen(false); setNewRun(true) }} />
            <MenuItem icon="check" label="Verify" onClick={() => { setOpen(false); submit({ action: 'verify' }) }} />
            <MenuItem icon="trash" label="Destroy / teardown" danger onClick={() => { setOpen(false); destroy() }} />
            <MenuItem icon="warn" label="Cancel run" danger disabled={!live} title={live ? undefined : 'The run is not running'} onClick={() => { setOpen(false); cancel() }} />
            <MenuItem icon="box" label="Reset a box…" onClick={() => { setOpen(false); setResetBox(true) }} />
            <MenuItem icon="terminal" label="View artifacts" onClick={() => { setOpen(false); setArtifacts(true) }} />
            <MenuItem icon="edit" label="Open comp editor" onClick={() => { setOpen(false); navigate(`/c/${run.comp_id}`) }} />
            <div className="my-1 border-t-2 border-line" />
            <MenuItem icon="plus" label="Add/remove live team" disabled
              title="Not yet — the engine has no capability for this" />
          </div>
        </>
      )}
      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
        initial={{
          comp_id: run.comp_id, action: 'deploy', engine_ref: run.engine_ref || 'main',
          teams: run.params?.teams ?? 2, scoring_vmid: run.params?.scoring_vmid ?? '',
        }}
        onSubmitted={() => { onChanged() }} />
      <ResetBoxModal open={resetBox} onClose={() => setResetBox(false)} run={run}
        onSubmitted={() => { onChanged() }} />
      <Modal open={artifacts} onClose={() => setArtifacts(false)} title={`Artifacts · ${run.id}`}>
        <ArtifactsList runId={run.id} />
      </Modal>
    </div>
  )
}

// ── one scrim card: a single run attempt ───────────────────────────────────────────────────

function RunCard({ run, comp, comps, onChanged }) {
  useTick(1000)
  const navigate = useNavigate()
  const [stageEvents, setStageEvents] = useState([])
  const { updated } = useRunEvents(run.id, (ev) => {
    if (ev.kind === 'stage') setStageEvents((s) => (s.some((x) => x.seq === ev.seq) ? s : [...s, ev]))
    onChanged()
  })
  const teams = run.params?.teams

  // Past stage events (for per-stage elapsed) come from the cursor endpoint; the live
  // ones arrive over SSE.
  useEffect(() => {
    setStageEvents([])
    api.runEvents(run.id, 0)
      .then((r) => setStageEvents((r.events || []).filter((e) => e.kind === 'stage')))
      .catch(() => {})
  }, [run.id])

  return (
    <Card className="rise flex flex-col p-7">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <Link to={`/c/${run.comp_id}`} className="display truncate text-2xl transition hover:text-accent">
              {comp?.name || run.comp_id}
            </Link>
            <span className="chip bg-sunken px-2.5 py-1 font-mono text-muted">{ACTION_LABEL[run.action] || run.action}</span>
            <StatePill state={run.state} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-faint">
            <span title={run.id}>{run.id.length > 12 ? `${run.id.slice(0, 12)}…` : run.id}</span>
            <span title="Engine ref">{run.engine_ref || 'main'}</span>
            <span>started {agoMs(run.started_at || run.created_at) || '—'}</span>
            <span>elapsed {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
          </div>
        </div>
        <RunMenu run={run} comps={comps} onChanged={onChanged} />
      </div>

      <div className="mt-5">
        <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
          stageEvents={stageEvents} />
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span className="flex items-center gap-1.5"><Icon name="boxes" className="h-3.5 w-3.5 text-faint" />
          <b className="font-semibold tabular-nums">{teams ?? '—'}</b> team{Number(teams) === 1 ? '' : 's'}
          {' · '}<b className="font-semibold tabular-nums">{comp?.boxes ?? '—'}</b> boxes
          {' · '}<b className="font-semibold tabular-nums">{comp?.services ?? '—'}</b> services
        </span>
        <span className="ml-auto flex items-center gap-1.5 text-faint" title="Last SSE event or poll">
          <span className={`h-1.5 w-1.5 rounded-full ${Date.now() - (updated || 0) < 15000 ? 'bg-accent' : 'bg-line'}`} />
          updated {updated ? agoMs(updated) : '—'}
        </span>
      </div>

      <Button variant="primary" className="mt-5 w-full" onClick={() => navigate(`/scrims/${run.id}`)}>
        <Icon name="overview" />Open scrim page
      </Button>
    </Card>
  )
}

// ── page ───────────────────────────────────────────────────────────────────────────────────

export default function Live() {
  const [runs, setRuns] = useState(null)
  const [comps, setComps] = useState(null)
  const [err, setErr] = useState(null)
  const [newRun, setNewRun] = useState(false)
  const refresh = useCallback(async () => {
    try {
      const [rs, cs] = await Promise.all([api.runs(), api.comps()])
      setRuns(rs)
      setComps(cs)
      setErr(null)
    } catch (e) { setErr(e) }
  }, [])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10000)
    return () => clearInterval(t)
  }, [refresh])

  const compMap = Object.fromEntries((comps || []).map((c) => [c.id, c]))
  const cards = (runs || [])
    .filter((r) => r.action !== 'plan' && isOnBoard(r))
    .sort((a, b) => toMs(b.created_at) - toMs(a.created_at))

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <TopNav />
      <PageHeader title="Active scrims"
        sub="Running and recently finished scrims, one card per attempt"
        actions={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>} />
      <ErrorBanner error={err} />
      {!runs && !err && <div className="grid grid-cols-1 gap-8 xl:grid-cols-2">{[0, 1].map((i) => <div key={i} className="panel h-72 animate-pulse opacity-60" />)}</div>}
      {runs && !cards.length && (
        <EmptyState icon="rocket" title="No active scrims"
          action={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>}>
          Submit a deploy, scrim, verify or teardown and it shows up here while it runs.
        </EmptyState>
      )}
      <div className="grid grid-cols-1 items-start gap-8 xl:grid-cols-2">
        {cards.map((r) => (
          <RunCard key={r.id} run={r} comp={compMap[r.comp_id]} comps={comps} onChanged={refresh} />
        ))}
      </div>
      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
        onSubmitted={() => { setErr(null); refresh() }} />
    </div>
  )
}

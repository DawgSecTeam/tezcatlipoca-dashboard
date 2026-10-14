// The operator's home: one card per competition with a run on the board — identity,
// stage timeline, usage, live log tail and the action menu.
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, Modal } from '../components/ui'
import { EmptyState, Icon, PageHeader, agoMs, fmtSpan, toMs, useConfirm, useTick, useToast } from '../components/kit'
import {
  ACTION_LABEL, ArtifactsList, LogTail, NewRunModal, ResetBoxModal, RunLogModal,
  StageStrip, StatePill, useRunEvents, useRunLog,
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
  const toast = useToast()
  const ask = useConfirm()
  const live = run.state === 'running' || run.state === 'queued'

  const submit = async (body) => {
    try {
      await api.submitRun({ comp_id: run.comp_id, engine_ref: run.engine_ref, ...body })
      toast('Run submitted')
      onChanged()
    } catch (e) { toast(e.message, 'error') }
  }
  const destroy = async () => {
    const ok = await ask({
      title: `Teardown ${run.comp_id}?`, danger: true, confirm: 'Destroy',
      body: <>Runs <span className="font-mono">destroy-competition.py</span> against this run's ownership set — the VMs it created are removed.</>,
    })
    if (ok) submit({ action: 'destroy' })
  }
  const cancel = async () => {
    const ok = await ask({
      title: 'Cancel this run?', danger: true, confirm: 'Cancel run',
      body: "Kills the run's process group. The range may be left half-built.",
    })
    if (!ok) return
    try { await api.cancelRun(run.id); toast('Cancel requested'); onChanged() } catch (e) { toast(e.message, 'error') }
  }

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
        onSubmitted={() => { toast('Run submitted'); onChanged() }} />
      <ResetBoxModal open={resetBox} onClose={() => setResetBox(false)} run={run}
        onSubmitted={() => { toast('Redeploy submitted'); onChanged() }} />
      <Modal open={artifacts} onClose={() => setArtifacts(false)} title={`Artifacts · ${run.id}`}>
        <ArtifactsList runId={run.id} />
      </Modal>
    </div>
  )
}

// ── one competition card ───────────────────────────────────────────────────────────────────

function RunCard({ runs, comp, comps, onChanged }) {
  useTick(1000)
  const [selId, setSelId] = useState(runs[0].id)
  const [expanded, setExpanded] = useState(false)
  const [stageEvents, setStageEvents] = useState([])
  const run = runs.find((r) => r.id === selId) || runs[0]
  const log = useRunLog(run.id)
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

      {runs.length > 1 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {runs.slice(0, 5).map((r) => (
            <button key={r.id} onClick={() => setSelId(r.id)}
              className={`chip px-2.5 py-1 transition ${r.id === run.id ? 'bg-accent-soft text-accent' : 'bg-sunken text-muted hover:text-fg'}`}>
              {ACTION_LABEL[r.action] || r.action} · {r.state}
            </button>
          ))}
        </div>
      )}

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

      <div className="mt-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="label !mb-0">Log</h3>
          <Button variant="ghost" className="!py-1 text-xs" onClick={() => setExpanded(true)}>
            <Icon name="terminal" className="h-3.5 w-3.5" />Expand
          </Button>
        </div>
        <LogTail text={log.text} />
      </div>

      <RunLogModal open={expanded} onClose={() => setExpanded(false)} log={log}
        title={`${ACTION_LABEL[run.action] || run.action} · ${comp?.name || run.comp_id}`} />
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
  const groups = {}
  for (const r of runs || []) if (isOnBoard(r)) (groups[r.comp_id] ||= []).push(r)
  const cards = Object.entries(groups)
    .map(([id, rs]) => [id, rs.sort((a, b) => toMs(b.created_at) - toMs(a.created_at))])
    .sort((a, b) => toMs(b[1][0].created_at) - toMs(a[1][0].created_at))

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <TopNav />
      <PageHeader title="Active comps"
        sub="Running and recently finished range runs, one card per competition"
        actions={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>} />
      <ErrorBanner error={err} />
      {!runs && !err && <div className="grid grid-cols-1 gap-8 xl:grid-cols-2">{[0, 1].map((i) => <div key={i} className="panel h-72 animate-pulse opacity-60" />)}</div>}
      {runs && !cards.length && (
        <EmptyState icon="rocket" title="No active competitions"
          action={<Button variant="primary" onClick={() => setNewRun(true)}><Icon name="plus" />New run</Button>}>
          Submit a deploy, plan, verify, scrim or teardown and it shows up here while it runs.
        </EmptyState>
      )}
      <div className="grid grid-cols-1 items-start gap-8 xl:grid-cols-2">
        {cards.map(([id, rs]) => (
          <RunCard key={id} runs={rs} comp={compMap[id]} comps={comps} onChanged={refresh} />
        ))}
      </div>
      <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
        onSubmitted={() => { setErr(null); refresh() }} />
    </div>
  )
}

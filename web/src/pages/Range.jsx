import { useEffect, useRef, useState } from 'react'
import { Link, Navigate, NavLink, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, Spinner } from '../components/ui'
import { EmptyState, Icon, agoMs, fmtSpan, useConfirm, useTick, useToast } from '../components/kit'
import { ACTION_LABEL, ResetBoxModal, StageStrip, StatePill, useRunActions, useRunEvents, useRunLog } from '../components/runui'
import CompetitionPanel from '../components/CompetitionPanel'
import RunTeams, { ResetRequests } from '../components/RunTeams'
import RunInfra from '../components/RunInfra'
import Run, { Evidence, Output, Timeline } from './Run'
import { detailSummary, rangeIssues, rangeTitle, shortRangeId } from '../rangeView'
import { rangeActionBody } from '../rangeModel'
import { useCompetitionData, useRangeData } from '../useRangeData'

const SECTIONS = [['', 'Overview', 'overview'], ['teams', 'Teams', 'boxes'], ['infrastructure', 'Infrastructure', 'cpu'], ['activity', 'Activity', 'terminal'], ['evidence', 'Evidence', 'packet']]
const COMPETITION_LABEL = { ready: 'Ready to start', running: 'Scoring', paused: 'Scoring paused', ended: 'Competition ended', unknown: 'Engine unreachable' }
const RANGE_LABEL = { live: 'Deployed', building: 'Operation in progress', failed: 'Deploy failed', teardown_failed: 'Teardown incomplete', retired: 'Retired' }

// The range's less-common actions. A native <details> stays open on Escape and its popover
// covers the identity row, so it is driven by state and closes on Escape/outside click.
function ActionsMenu({ onDestroy }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const away = (event) => { if (!box.current?.contains(event.target)) setOpen(false) }
    const key = (event) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', key) }
  }, [open])
  return <div className="relative" ref={box}>
    <Button aria-label="More range actions" aria-expanded={open} onClick={() => setOpen((value) => !value)}><Icon name="dots" /></Button>
    {open && <div className="absolute right-0 z-20 mt-2 w-60 rounded-xl border border-line bg-bg p-2 shadow-tile">
      <Button variant="ghost" className="w-full !justify-start text-danger" onClick={() => { setOpen(false); onDestroy() }}><Icon name="trash" />Destroy this range…</Button>
    </div>}
  </div>
}

function CopyText({ text }) {
  const toast = useToast()
  return <button type="button" className="text-accent hover:underline" onClick={async () => {
    try { await navigator.clipboard.writeText(text); toast('Copied') } catch { toast('Copy failed', 'error') }
  }}>copy</button>
}

// One status line: range, scoring and active operation, so state is read in one place.
function StatusLine({ range, summary, competition }) {
  const retired = range.state === 'retired'
  const scoring = retired ? 'Competition archived' : competition.error ? 'Scoring status unavailable' : COMPETITION_LABEL[competition.view?.state] || 'Checking scoring…'
  const scoringBad = !retired && (competition.error || competition.view?.state === 'unknown')
  // /api/engine answers last_round as a round record ({ID, StartTime, Checks, SLAs}), not a number.
  const round = competition.view?.engine?.last_round?.ID
  const run = summary.active_runs[0]
  const danger = ['failed', 'teardown_failed'].includes(range.state)
  return <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
    <span className={`inline-flex items-center gap-2 font-semibold ${danger ? 'text-danger' : ''}`}><span className={`h-2 w-2 rounded-full ${danger ? 'bg-danger' : retired ? 'bg-line' : 'bg-accent'}`} />{RANGE_LABEL[range.state] || range.state}</span>
    <span className={scoringBad ? 'text-danger' : 'text-muted'}>{scoring}{round > 0 && !retired ? ` · round ${round}` : ''}</span>
    {run && <Link to={`/ranges/${range.id}/activity?operation=${run.id}`} className="text-accent hover:underline">{ACTION_LABEL[run.action] || run.action} {run.state}{run.stage ? ` · ${run.stage}` : ''}</Link>}
  </div>
}

function OperationRow({ run, range }) {
  return <Link to={`/ranges/${range.id}/activity?operation=${run.id}`} className="flex items-center justify-between gap-3 border-t border-line py-2.5 text-sm first:border-0 hover:text-accent">
    <span className="min-w-0">
      <span className="font-medium">{ACTION_LABEL[run.action] || run.action}</span>
      <span className="block text-[11px] text-faint">{agoMs(run.ended_at || run.started_at || run.created_at)} · {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
    </span>
    <StatePill state={run.state} />
  </Link>
}

// An in-flight operation, with its own controls, so a held deploy can be resumed from the
// overview instead of only from the Activity tab.
function ActiveOperation({ run, range, refresh }) {
  const { pause, resume, cancel, live, held, canPause } = useRunActions(run, refresh, range)
  return <Card className="p-5 sm:p-6">
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <h2 className="text-base font-semibold">{ACTION_LABEL[run.action] || run.action} in progress</h2>
      <StatePill state={run.state} />
      <span className="text-xs text-faint">started {agoMs(run.started_at || run.created_at)} · {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
      <Link to={`/ranges/${range.id}/activity?operation=${run.id}`} className="ml-auto text-xs font-semibold text-accent hover:underline">View output</Link>
    </div>
    <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at} />
    {(canPause || held || live) && <div className="mt-4 flex flex-wrap gap-2">
      {canPause && <Button onClick={pause}><Icon name="pause" />Pause after this stage</Button>}
      {held && <Button variant="primary" onClick={resume}><Icon name="play" />Resume</Button>}
      {live && <Button variant="danger" onClick={cancel}>Cancel operation</Button>}
    </div>}
  </Card>
}

function TeamsGlance({ range, summary }) {
  const teams = range.inventory?.teams || []
  const pending = new Map()
  for (const request of range.requests || []) if (request.status === 'pending') pending.set(request.team, (pending.get(request.team) || 0) + 1)
  return <Card className="overflow-hidden">
    <div className="flex items-center justify-between gap-3 px-5 py-4">
      <h2 className="text-base font-semibold">Teams <span className="font-normal text-faint">{summary.counts.teams ?? teams.length}</span></h2>
      <Link to={`/ranges/${range.id}/teams`} className="text-xs font-medium text-accent">Boxes & recovery</Link>
    </div>
    {!teams.length ? <p className="px-5 pb-4 text-sm text-muted">No teams are recorded for this range.</p> : <div className="overflow-x-auto"><table className="w-full min-w-[28rem] text-left">
      <thead><tr className="border-t border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
        <th className="px-5 py-2">Team</th><th className="px-5 py-2">Subnet</th><th className="px-5 py-2">Boxes</th><th className="px-5 py-2">Reset requests</th>
      </tr></thead>
      <tbody className="divide-y divide-line">{teams.map((team) => {
        const waiting = pending.get(team.key) || pending.get(team.identifier) || 0
        return <tr key={team.key} className="text-sm">
          <td className="px-5 py-2 font-mono">{team.key}{team.identifier && <span className="ml-2 text-xs text-faint">{team.identifier}</span>}</td>
          <td className="px-5 py-2 font-mono text-xs text-muted">{team.subnet || '—'}</td>
          <td className="px-5 py-2 tabular-nums text-muted">{(team.boxes || []).length}</td>
          <td className="px-5 py-2">{waiting ? <span className="chip bg-danger-soft text-danger">{waiting} pending</span> : <span className="text-xs text-faint">—</span>}</td>
        </tr>
      })}</tbody>
    </table></div>}
  </Card>
}

function RangeFacts({ range, summary }) {
  const origin = range.runs.find((run) => run.id === range.deploy_run_id)
  const facts = [
    // "Deployed" is only true once something is up; a range still building has started its
    // deploy run, and the range page claimed it had deployed 5 minutes ago.
    [range.state === 'building' ? 'Deploy started' : 'Deployed', range.deployed_at || origin?.created_at ? agoMs(range.deployed_at || origin?.created_at) : '—'],
    ['Deployed by', range.deploy_run_id ? <Link to={`/ranges/${range.id}/activity?operation=${range.deploy_run_id}`} className="text-accent hover:underline">{range.deploy_run_id}</Link> : '—'],
    ['Engine ref', range.engine_ref || '—'],
    ['Engine IP', summary.engine_ip || '—'],
    ['Nodes', summary.nodes.join(', ') || '—'],
    ['Recorded', `${summary.counts.teams ?? '—'} teams · ${summary.counts.boxes ?? '—'} boxes · ${summary.counts.networks ?? '—'} networks`],
  ]
  return <Card className="p-5">
    <div className="mb-3 flex items-center justify-between"><h2 className="text-base font-semibold">Range</h2><Link to={`/ranges/${range.id}/infrastructure`} className="text-xs font-medium text-accent">Infrastructure</Link></div>
    <dl className="space-y-2 text-sm">{facts.map(([label, value]) => <div key={label} className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2">
      <dt className="text-xs text-faint">{label}</dt><dd className="min-w-0 break-words font-mono text-xs text-muted">{value}</dd>
    </div>)}</dl>
  </Card>
}

function RangeOverview({ range, competition, onSubmitted, refresh, disabled }) {
  const summary = detailSummary(range)
  const issues = rangeIssues(summary).filter((issue) => issue.kind !== 'requests')
  const panel = { rangeKey: range.key, rangeId: range.id, compId: range.comp_id, engineRef: range.engine_ref, view: competition.view, loading: !competition.view && !competition.error, error: competition.error, onRefresh: refresh, onSubmitted, disabled, recorded: summary.has_recorded_vms }
  return <div className="range-overview">
    <div className="min-w-0 space-y-6">
      {!!issues.length && <section className="rounded-2xl border border-danger/20 bg-danger-soft p-5 sm:p-6" aria-label="Attention needed">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-danger"><Icon name="warn" />Attention needed</h2>
        <ul className="mt-3 space-y-3">{issues.map((issue, index) => <li key={`${issue.kind}-${index}`} className="flex flex-wrap items-baseline justify-between gap-2 text-sm leading-relaxed">
          <span>{issue.message}</span>
          <Link to={`/ranges/${range.id}/${issue.run_id ? `activity?operation=${issue.run_id}` : 'infrastructure'}`} className="shrink-0 text-xs font-semibold text-danger underline underline-offset-4">{issue.run_id ? 'View output' : 'Inspect infrastructure'}</Link>
        </li>)}</ul>
      </section>}
      {summary.pending_requests > 0 && <ResetRequests key={range.key} rangeKey={range.key} rangeId={range.id} disabled={disabled} onSubmitted={onSubmitted} pendingOnly />}
      {summary.active_runs.map((run) => <ActiveOperation key={run.id} run={run} range={range} refresh={refresh} />)}
      {range.state === 'retired'
        ? <Card className="p-5 sm:p-6"><h2 className="text-base font-semibold">Competition archived</h2><p className="mt-2 text-sm text-muted">Live competition polling is stopped for this retired range. Review its activity and evidence for the recorded results.</p></Card>
        : <CompetitionPanel {...panel} section="lifecycle" key={range.id} />}
      <TeamsGlance range={range} summary={summary} />
    </div>
    <aside className="min-w-0 space-y-6">
      {range.state !== 'retired' && <CompetitionPanel {...panel} section="access" key={`${range.id}-access`} />}
      <RangeFacts range={range} summary={summary} />
      <Card className="p-5">
        <div className="mb-1 flex items-center justify-between"><h2 className="text-base font-semibold">Recent activity</h2><Link to={`/ranges/${range.id}/activity`} className="text-xs font-medium text-accent">All {range.runs.length}</Link></div>
        {range.runs.length ? range.runs.slice(0, 5).map((run) => <OperationRow key={run.id} run={run} range={range} />) : <p className="text-sm text-muted">No operations recorded.</p>}
      </Card>
    </aside>
  </div>
}

function OperationDetail({ run, range, refresh }) {
  const [events, setEvents] = useState([])
  const [eventError, setEventError] = useState(null)
  const log = useRunLog(run.id)
  const { pause, resume, cancel, live, held, canPause } = useRunActions(run, refresh, range)
  const stream = useRunEvents(run.id, (event) => {
    setEvents((old) => old.some((item) => item.seq === event.seq) ? old : [...old, event].sort((a, b) => a.seq - b.seq))
    if (event.kind === 'lifecycle') refresh()
  })
  useEffect(() => {
    let active = true
    api.runEvents(run.id).then((response) => { if (active) setEvents((old) => [...new Map([...response.events, ...old].map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq)) }).catch((error) => { if (active) setEventError(error) })
    return () => { active = false }
  }, [run.id])
  return <div className="space-y-5">
    <Card className="p-5 sm:p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-lg font-semibold">{ACTION_LABEL[run.action] || run.action}</h3><p className="mt-1 break-all font-mono text-xs text-faint">{run.id}</p></div><StatePill state={run.state} /></div>
      <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at} stageEvents={events.filter((event) => event.kind === 'stage')} />
      <dl className="mt-5 grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">{[['Engine ref', run.engine_ref], ['Started', agoMs(run.started_at || run.created_at)], ['Duration', fmtSpan(run.started_at || run.created_at, run.ended_at)], ['Exit code', run.returncode ?? '—']].map(([label, value]) => <div key={label}><dt className="label">{label}</dt><dd className="break-words font-mono text-muted">{value || '—'}</dd></div>)}</dl>
      {(canPause || held || live) && <div className="mt-5 flex flex-wrap gap-2">{canPause && <Button onClick={pause}><Icon name="pause" />Pause deployment after stage</Button>}{held && <Button onClick={resume}><Icon name="play" />Resume deployment</Button>}{live && <Button variant="danger" onClick={cancel}>Cancel operation</Button>}</div>}
    </Card>
    <Card className="p-5 sm:p-6"><div className="mb-4 flex flex-wrap items-center justify-between gap-2"><h3 className="text-base font-semibold">Operation output</h3><span className="text-xs text-faint">{stream.live ? 'Event stream connected' : 'Recovering events by cursor'}</span></div><Output log={log} /></Card>
    <Card className="p-5 sm:p-6"><h3 className="mb-4 text-base font-semibold">Event timeline</h3><ErrorBanner error={eventError} /><div className="max-h-96 overflow-auto"><Timeline events={events} start={run.started_at || run.created_at} /></div></Card>
  </div>
}

function RangeActivity({ range, refresh }) {
  const [search, setSearch] = useSearchParams()
  const runs = range.runs || []
  const requested = search.get('operation')
  const selected = runs.find((run) => run.id === requested) || (!requested ? runs.find((run) => ['queued', 'running', 'pausing', 'paused'].includes(run.state)) || runs[0] : null)
  return <div>
    <h2 className="text-xl font-semibold">Activity & output</h2><p className="mb-5 mt-1 text-sm text-muted">Each operation has its own output. Selecting one does not change this range’s controls.</p>
    <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[250px_minmax(0,1fr)]">
      <div className="max-h-[65vh] space-y-1 overflow-auto xl:sticky xl:top-6" aria-label="Select operation">{runs.map((run) => <button key={run.id} onClick={() => setSearch({ operation: run.id })} aria-pressed={selected?.id === run.id} className={`w-full rounded-xl border p-3 text-left ${selected?.id === run.id ? 'border-accent/30 bg-accent-soft' : 'border-transparent hover:bg-sunken'}`}><span className="flex flex-wrap items-center justify-between gap-2 text-sm font-semibold">{ACTION_LABEL[run.action] || run.action}<StatePill state={run.state} /></span><span className="mt-2 block break-all font-mono text-[10px] text-faint">{run.id}</span><span className="mt-1 block text-xs text-muted">{agoMs(run.created_at)}</span></button>)}</div>
      {selected ? <OperationDetail key={selected.id} run={selected} range={range} refresh={refresh} /> : <ErrorBanner error={new Error(requested ? 'This operation does not belong to this range.' : 'No operations recorded.')} />}
    </div>
  </div>
}

export default function Range() {
  const { rangeId, '*': section = '' } = useParams()
  const { data: range, error, updated, refresh } = useRangeData(rangeId)
  const [competitionRevision, setCompetitionRevision] = useState(0)
  const competition = useCompetitionData(range?.state === 'retired' ? null : range?.key, competitionRevision)
  const [submitted, setSubmitted] = useState(null)
  const [pending, setPending] = useState(false)
  const [reset, setReset] = useState(false)
  const toast = useToast()
  const confirm = useConfirm()
  useTick(10000)
  const reload = () => { refresh(); setCompetitionRevision((value) => value + 1) }
  const onSubmitted = (run) => { if (run?.id) setSubmitted(run); reload() }
  useEffect(() => { setSubmitted(null); setReset(false) }, [rangeId])
  useEffect(() => { document.title = range ? `${rangeTitle(range)} · ${shortRangeId(range)} · tezcatlipoca` : 'Range · tezcatlipoca' }, [range])
  if (!range) return <main className="range-container"><TopNav /><Link to="/" className="text-sm text-accent">← Active ranges</Link><div className="mt-6">{error ? <ErrorBanner error={error} onRetry={refresh} /> : <div className="flex items-center gap-3 py-12 text-sm text-muted"><Spinner />Loading range…</div>}</div></main>
  const summary = detailSummary(range)
  const origin = range.runs.find((run) => run.id === range.deploy_run_id) || range.runs[0]
  const scopedInventory = { ...range.inventory, range: { ...range.inventory?.range, ...range, inventory: undefined, runs: undefined, requests: undefined } }
  const retired = range.state === 'retired'
  const blocked = retired || pending || !!error
  // A range whose newest destroy failed is not necessarily torn down (`teardown_failed`
  // is only the state where nothing is left): the Infrastructure tab must warn either way.
  const teardownIssue = rangeIssues(summary).some((issue) => issue.kind === 'teardown')
  const submit = async (action) => {
    if (blocked) return
    if (action === 'destroy' && !(await confirm({ title: `Destroy range ${shortRangeId(range)}?`, body: `Removes infrastructure owned by this range of ${rangeTitle(range)}. Other ranges of this competition are not targeted. Review active operations before continuing.`, danger: true, confirm: 'Destroy this range' }))) return
    setPending(true)
    try { const created = await api.submitRun(rangeActionBody(range, action)); onSubmitted(created); toast(`Operation ${created.id} submitted`) }
    catch (err) { toast(err.message, 'error') }
    finally { setPending(false) }
  }
  const validSection = SECTIONS.some(([path]) => path === section)
  if (!validSection) return <Navigate to={`/ranges/${rangeId}`} replace />
  return <main className="range-container">
    <TopNav />
    <header className="mb-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><Link to="/" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-accent"><Icon name="back" className="h-3 w-3" />Active ranges</Link>{range.comp_defined !== false && <Link to={`/c/${range.comp_id}`} className="text-xs text-faint hover:text-accent">Edit competition definition ↗</Link>}</div>
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="min-w-0">
          <h1 className="display break-words text-4xl sm:text-5xl">{rangeTitle(range)}</h1>
          <div className="mt-3"><StatusLine range={range} summary={summary} competition={competition} /></div>
        </div>
        <div className="flex flex-wrap gap-2"><Button onClick={() => submit('verify')} disabled={blocked}><Icon name="check" />Verify range</Button><Button onClick={() => setReset(true)} disabled={blocked || !range.inventory?.teams?.length}><Icon name="box" />Reset a box</Button>{!blocked && <ActionsMenu onDestroy={() => submit('destroy')} />}</div>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-faint">
        <details className="min-w-0">
          <summary className="cursor-pointer hover:text-accent">Identity: <span className="font-mono">{shortRangeId(range)}</span></summary>
          <dl className="mt-2 grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono">
            <dt className="font-sans">Range ID</dt><dd className="break-all">{range.id}</dd>
            <dt className="font-sans">Competition</dt><dd className="break-all">{range.comp_id}</dd>
            <dt className="font-sans">Engine ref</dt><dd className="break-all">{range.engine_ref || '—'}</dd>
            <dt className="font-sans">Workspace</dt><dd className="break-all">{range.key} <CopyText text={range.key} /></dd>
          </dl>
        </details>
        <button onClick={reload} className="ml-auto self-start text-xs text-faint hover:text-accent">{error ? 'Refresh failed · ' : ''}{updated ? `Fetched ${agoMs(updated)} · ` : ''}Refresh</button>
      </div>
    </header>
    <ErrorBanner error={error} onRetry={reload} />
    {retired && <p className="mb-5 rounded-xl bg-sunken px-4 py-3 text-sm text-muted">This range is retired. Its history and evidence remain available; range operations are disabled.</p>}
    {submitted && <div role="status" className="mb-5 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-accent-soft px-4 py-3 text-sm"><span>Submitted <span className="font-mono text-xs">{submitted.id}</span></span><Link className="font-semibold text-accent hover:underline" to={`/ranges/${range.id}/activity?operation=${submitted.id}`}>View operation</Link></div>}
    <nav className="mb-7 flex flex-wrap gap-1 border-b border-line" aria-label="Range sections">{SECTIONS.map(([path, label, icon]) => <NavLink key={path} end to={`/ranges/${range.id}${path ? `/${path}` : ''}`} className={({ isActive }) => `inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-3 text-sm ${isActive ? 'border-accent font-semibold text-accent' : 'border-transparent text-muted hover:text-fg'}`}><Icon name={icon} />{label}{path === 'teams' && summary.pending_requests > 0 && <span className="chip bg-danger-soft text-danger">{summary.pending_requests}</span>}</NavLink>)}</nav>
    {section === '' && <RangeOverview range={range} competition={competition} onSubmitted={onSubmitted} refresh={reload} disabled={blocked} />}
    {section === 'teams' && <RunTeams run={origin} inventory={scopedInventory} disabled={blocked} onSubmitted={onSubmitted} showMachines={false} />}
    {section === 'infrastructure' && <RunInfra run={origin} inventory={scopedInventory} teardownFailed={teardownIssue} />}
    {section === 'activity' && <RangeActivity range={range} refresh={reload} />}
    {section === 'evidence' && <Evidence runId={origin.id} range={range} />}
    <ResetBoxModal open={reset} onClose={() => setReset(false)} run={origin} range={range} teams={range.inventory?.teams} onSubmitted={onSubmitted} />
  </main>
}

export function LegacyRun() {
  const { run: runId, '*': section } = useParams()
  const redirect = ['teams', 'infrastructure', 'evidence'].includes(section)
  const [state, setState] = useState({ runId, rangeId: null, error: null })
  useEffect(() => {
    if (!redirect) return
    let active = true
    api.runInventory(runId).then((data) => { if (active) setState({ runId, rangeId: data.range?.id, error: null }) }).catch((error) => { if (active) setState({ runId, error }) })
    return () => { active = false }
  }, [runId, redirect])
  if (!redirect) return <Run />
  if (state.runId !== runId || (!state.rangeId && !state.error)) return <Spinner />
  if (state.error) return <ErrorBanner error={state.error} />
  return <Navigate to={`/ranges/${state.rangeId}/${section}`} replace />
}

// One run's management page — where a Live card's Open button lands: overview (state,
// stages, usage, active teams, log), team management and artifacts. Sidebar per the
// operator sketch: back on top, then Overview / Teams / Artifacts.
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, Spinner } from '../components/ui'
import { Icon, agoMs, fmtSpan, useTick } from '../components/kit'
import {
  ACTION_LABEL, ArtifactsList, EventList, LogView, NewRunModal, ResetBoxModal,
  StageStrip, StatePill, useRunActions, useRunEvents, useRunLog,
} from '../components/runui'

const TAB_ICON = { overview: 'overview', teams: 'boxes', artifacts: 'packet' }

function Stat({ k, v }) {
  return (
    <div className="flex items-baseline justify-between gap-3 rounded-2xl bg-sunken px-4 py-2.5 shadow-inset">
      <span className="text-xs font-semibold uppercase tracking-wider text-faint">{k}</span>
      <span className="min-w-0 truncate font-mono text-xs text-muted" title={String(v)}>{String(v)}</span>
    </div>
  )
}

function Teams({ run, comp, onReset }) {
  const n = Number(run.params?.teams) || 0
  const boxes = comp?.boxes || []
  if (!n) return <p className="text-sm text-faint">This run carries no team count.</p>
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {Array.from({ length: n }, (_, i) => (
        <Card key={i} className="p-5">
          <div className="flex items-center justify-between gap-2">
            <h3 className="label !mb-0">Team {i + 1}</h3>
            <Button variant="ghost" className="!py-1 text-xs" onClick={() => onReset(i + 1)}>
              <Icon name="box" className="h-3.5 w-3.5" />Reset a box…
            </Button>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {boxes.map((b) => <span key={b.name} className="chip bg-sunken px-2 py-0.5 font-mono text-[11px] text-muted">{b.name}</span>)}
            {!boxes.length && <span className="text-xs text-faint">no boxes defined</span>}
          </div>
        </Card>
      ))}
    </div>
  )
}

export default function Scrim() {
  const { id } = useParams()
  const [tab, setTab] = useState('overview')
  const [run, setRun] = useState(null)
  const [comp, setComp] = useState(null)
  const [comps, setComps] = useState(null)
  const [events, setEvents] = useState([])
  const [err, setErr] = useState(null)
  const [newRun, setNewRun] = useState(false)
  const [resetBox, setResetBox] = useState(null) // null = closed, {} | {team} = prefill
  const log = useRunLog(id)
  useTick(1000)
  const reload = () => api.run(id).then(setRun).catch(() => {})
  const { submit, destroy, cancel, live } = useRunActions(run, reload)
  const { updated } = useRunEvents(id, (ev) => {
    setEvents((e) => (e.some((x) => x.seq === ev.seq) ? e : [...e, ev].sort((a, b) => (a.seq || 0) - (b.seq || 0))))
    reload()
  })

  useEffect(() => {
    setRun(null); setComp(null); setEvents([]); setErr(null); setTab('overview')
    api.run(id)
      .then((r) => {
        setRun(r)
        api.comp(r.comp_id).then(setComp).catch(() => {})
        api.comps().then(setComps).catch(() => {})
      })
      .catch(setErr)
    api.runEvents(id, 0).then((r) => setEvents(r.events || [])).catch(() => {})
  }, [id])
  useEffect(() => {
    document.title = run
      ? `${comp?.name || run.comp_id} · ${ACTION_LABEL[run.action] || run.action} · tezcatlipoca`
      : 'tezcatlipoca'
  }, [run, comp])

  if (err) return <div className="p-10"><ErrorBanner error={err} /><Link className="text-accent" to="/">← Active scrims</Link></div>

  const teams = run?.params?.teams
  const boxes = comp?.boxes || []
  const usage = run && [
    ['Teams', teams ?? '—'],
    ['Boxes', comp?.boxes?.length ?? '—'],
    ['Services', comp?.services ?? '—'],
    ['Engine node', run.params?.engine_node || '—'],
    ['Team node', run.params?.team_node || '—'],
    ['Scoring VMID', run.params?.scoring_vmid ?? '—'],
    ['Engine ref', run.engine_ref || 'main'],
    ['Run dir', run.run_dir || '—'],
    ['Created', agoMs(run.created_at) || '—'],
    ['Started', agoMs(run.started_at) || '—'],
    ['Ended', run.ended_at ? agoMs(run.ended_at) : '—'],
    ['Duration', fmtSpan(run.started_at || run.created_at, run.ended_at)],
  ]

  return (
    <div className="flex h-screen">
      <div className="shrink-0 p-6 pr-0">
        <aside className="panel flex h-full w-64 flex-col gap-5 p-4">
          <Link to="/" className="group flex items-center gap-2 px-2 pt-1" title="Active scrims">
            <Icon name="back" className="h-4 w-4" />
            <span className="text-[13px] font-semibold tracking-tight">back</span>
          </Link>
          <nav className="space-y-1" aria-label="Scrim">
            {Object.keys(TAB_ICON).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`flex w-full items-center gap-3 rounded-full px-4 py-2 text-sm capitalize transition ${
                  tab === t ? 'bg-accent-soft font-semibold text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`}>
                <Icon name={TAB_ICON[t]} />{t}
              </button>
            ))}
          </nav>
          <div className="mt-auto truncate px-1 font-mono text-[11px] text-faint" title={id}>{id}</div>
        </aside>
      </div>
      <main className="min-w-0 flex-1 overflow-auto p-6 pl-9">
        {!run && !err && <div className="flex justify-center p-10"><Spinner /></div>}
        {run && (
          <div className="rise mx-auto max-w-5xl space-y-6 pb-10">
            <header>
              <div className="flex flex-wrap items-center gap-2.5">
                <Link to={`/c/${run.comp_id}`} className="display truncate text-3xl transition hover:text-accent">
                  {comp?.name || run.comp_id}
                </Link>
                <span className="chip bg-sunken px-2.5 py-1 font-mono text-muted">{ACTION_LABEL[run.action] || run.action}</span>
                <StatePill state={run.state} />
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-faint">
                <span title={run.id}>{run.id}</span>
                <span title="Engine ref">{run.engine_ref || 'main'}</span>
                <span>rc {run.returncode ?? '—'}</span>
                <span>started {agoMs(run.started_at || run.created_at) || '—'}</span>
                <span>elapsed {fmtSpan(run.started_at || run.created_at, run.ended_at)}</span>
                <span className="flex items-center gap-1.5" title="Last SSE event or poll">
                  <span className={`h-1.5 w-1.5 rounded-full ${Date.now() - (updated || 0) < 15000 ? 'bg-accent' : 'bg-line'}`} />
                  updated {updated ? agoMs(updated) : '—'}
                </span>
              </div>
            </header>

            {tab === 'overview' && (
              <>
                <Card className="p-6">
                  <StageStrip action={run.action} stage={run.stage} state={run.state} endedAt={run.ended_at}
                    stageEvents={events.filter((e) => e.kind === 'stage')} />
                </Card>

                <div className="flex flex-wrap gap-2">
                  {live && <Button variant="danger" onClick={cancel}><Icon name="warn" />Cancel run</Button>}
                  <Button onClick={() => submit({ action: 'verify' })}><Icon name="check" />Verify</Button>
                  <Button onClick={() => setNewRun(true)}><Icon name="rocket" />New run (re-deploy)</Button>
                  <Button onClick={() => setResetBox({})}><Icon name="box" />Reset a box…</Button>
                  <Button variant="danger" onClick={destroy}><Icon name="trash" />Destroy / teardown</Button>
                </div>

                <section className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm lg:grid-cols-3">
                  {usage.map(([k, v]) => <Stat key={k} k={k} v={v} />)}
                  {run.error && (
                    <div className="col-span-2 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger lg:col-span-3">
                      {String(run.error)}
                    </div>
                  )}
                </section>

                <section>
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="label !mb-0">Active teams</h3>
                    <button onClick={() => setTab('teams')} className="text-xs text-accent hover:underline">Manage →</button>
                  </div>
                  <p className="text-sm text-muted">
                    <b className="font-semibold tabular-nums">{teams ?? '—'}</b> teams deployed,
                    {' '}{boxes.length} boxes each: {boxes.map((b) => b.name).join(', ') || '—'}
                  </p>
                </section>

                <section>
                  <h3 className="label">Events</h3>
                  <div className="max-h-72 overflow-auto rounded-2xl bg-sunken p-3 shadow-inset">
                    <EventList events={events} />
                  </div>
                </section>

                <section>
                  <h3 className="label">Log</h3>
                  <LogView log={log} height="50vh" />
                </section>
              </>
            )}

            {tab === 'teams' && <Teams run={run} comp={comp} onReset={(n) => setResetBox({ team: String(n) })} />}

            {tab === 'artifacts' && (
              <Card className="p-6">
                <p className="mb-3 text-xs text-faint">Evidence collected under the workspace's .automated-tests/ — click a file to download it.</p>
                <ArtifactsList runId={id} />
              </Card>
            )}
          </div>
        )}

        {run && (
          <>
            <NewRunModal open={newRun} onClose={() => setNewRun(false)} comps={comps}
              initial={{
                comp_id: run.comp_id, action: 'deploy', engine_ref: run.engine_ref || 'main',
                teams: teams ?? 2, scoring_vmid: run.params?.scoring_vmid ?? '',
              }}
              onSubmitted={reload} />
            <ResetBoxModal open={resetBox !== null} onClose={() => setResetBox(null)} run={run}
              initial={resetBox || {}} onSubmitted={reload} />
          </>
        )}
      </main>
    </div>
  )
}

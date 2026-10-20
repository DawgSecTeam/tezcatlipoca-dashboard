import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, SearchInput } from '../components/ui'
import { EmptyState, Icon, agoMs, useSlashFocus, useTick } from '../components/kit'
import { ACTION_LABEL, NewRunModal, stageProgress } from '../components/runui'
import { filterCounts, filterRanges, heroStatus, rangeIssues, rangeTitle, scoringChip, shortRangeId } from '../rangeView'

const DOT = { danger: 'bg-danger', active: 'animate-pulse bg-accent', ok: 'bg-accent', muted: 'bg-line' }
const HERO_TEXT = { danger: 'text-danger', active: 'text-fg', ok: 'text-fg', muted: 'text-muted' }

// The live-operation progress bar: filled to the known stage, or an open-ended sweep
// while the record cannot say where in its stages the run is.
function Progress({ run }) {
  const { total, idx, label } = stageProgress(run)
  const held = run.state === 'paused' || run.state === 'pausing'
  const queued = run.state === 'queued'
  const known = idx >= 0 && total > 0
  return (
    <div>
      <div
        role="progressbar" aria-label={label ? `progress: ${label}` : 'operation in progress'}
        aria-valuemin={1} aria-valuemax={known ? total : undefined}
        aria-valuenow={known ? Math.min(idx + 1, total) : undefined}
        className="h-2.5 overflow-hidden rounded-full bg-sunken shadow-inset">
        {known
          ? <div className={`h-full rounded-full transition-all duration-500 ${held ? 'bg-accent/70' : 'bg-accent'}`}
              style={{ width: `${(Math.min(idx + 1, total) / total) * 100}%` }} />
          : <div className={`h-full w-1/3 rounded-full bg-accent ${queued ? 'opacity-40' : 'animate-sweep'}`} />}
      </div>
      <p className="mt-1.5 flex min-w-0 items-center gap-2 text-xs text-muted">
        <span className="min-w-0 truncate">{queued ? 'Queued — waiting for a worker' : label || 'Working…'}</span>
        {known && !/\d+\s*\/\s*\d+/.test(label) && <span className="ml-auto shrink-0 tabular-nums text-faint">{Math.min(idx + 1, total)}/{total}</span>}
      </p>
    </div>
  )
}

// Where an issue's chip points: its operation when it names one, the requests table
// for pending resets, the infrastructure records for degradations.
const issueTo = (range, issue) => issue.run_id
  ? `/ranges/${range.id}/activity?operation=${issue.run_id}`
  : issue.kind === 'requests' ? `/ranges/${range.id}/teams` : `/ranges/${range.id}/infrastructure`

function RangeCard({ range }) {
  const hero = heroStatus(range)
  const run = hero.run
  // The hero already names the problem or operation it came from; badge the rest.
  const issues = rangeIssues(range).filter((issue) => issue.message !== hero.issue?.message)
  const scoring = hero.kind === 'operation' ? scoringChip(range) : null
  const others = (range.active_runs || []).slice(1)
  const base = `/ranges/${range.id}`
  return (
    <article className="tile flex min-w-0 flex-col gap-4 p-5">
      <header className="min-w-0">
        <div className="flex items-center gap-2.5">
          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${DOT[hero.tone]}`} />
          <Link to={base} className="min-w-0 truncate text-lg font-semibold tracking-tight hover:text-accent" title={rangeTitle(range)}>{rangeTitle(range)}</Link>
        </div>
        {/* Every range of one competition is deployed from the same Compfile, so its
            name alone cannot tell two concurrent ranges apart: the deploy run and the
            engine address are the range's own identity. */}
        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-faint">
          <span className="font-mono">{shortRangeId(range)}</span>
          {range.engine_ip && <span className="font-mono">{range.engine_ip}</span>}
          <span>{range.deployed_at ? `deployed ${agoMs(range.deployed_at)}` : 'deploy time not recorded'}</span>
        </p>
      </header>

      {/* The hero: what this range is right now. An in-flight operation earns the
          whole block and its live progress; everything else is one clear line. */}
      <div className="min-w-0">
        {hero.kind === 'operation'
          ? <Link to={hero.to} className="block rounded-2xl bg-sunken p-3.5 shadow-inset transition hover:shadow-none">
              <p className="mb-2.5 flex flex-wrap items-center gap-2 text-sm font-semibold">
                {hero.label}
                {(run.state === 'paused' || run.state === 'pausing') && <span className="chip bg-accent-soft text-accent">held</span>}
                {run.state === 'queued' && <span className="chip bg-bg text-muted">queued</span>}
              </p>
              <Progress run={run} />
            </Link>
          : <Link to={hero.to} className={`flex flex-wrap items-baseline gap-2.5 text-base font-semibold hover:underline ${HERO_TEXT[hero.tone]}`}>
              {hero.label}
              {hero.detail && <span className="text-xs font-normal text-faint">{hero.detail}</span>}
            </Link>}
        {scoring && (
          <p className={`mt-2 flex items-center gap-2 text-xs font-medium ${scoring.tone === 'danger' ? 'text-danger' : 'text-muted'}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${DOT[scoring.tone]}`} />{scoring.label}
          </p>
        )}
        {others.map((other) => (
          <p key={other.id} className="mt-1.5 text-xs text-muted">Also: {ACTION_LABEL[other.action] || other.action} · {other.state}</p>
        ))}
      </div>

      {!!issues.length && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Needs attention">
          {issues.map((issue, index) => (
            <li key={`${issue.kind}-${index}`}>
              <Link to={issueTo(range, issue)} className="chip bg-danger-soft px-2.5 py-1 text-danger hover:underline" title={issue.message}>
                <Icon name="warn" className="mr-1 h-3 w-3" />{issue.message}
              </Link>
            </li>
          ))}
        </ul>
      )}

      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span><b className="tabular-nums text-fg">{range.counts?.teams ?? '—'}</b> teams</span>
        <span><b className="tabular-nums text-fg">{range.counts?.boxes ?? '—'}</b> boxes</span>
        {!!range.nodes?.length && <span className="truncate font-mono text-faint" title={range.nodes.join(', ')}>{range.nodes.join(',')}</span>}
      </p>

      <Link to={base} className="btn btn-primary mt-auto w-full">Open range<Icon name="chevron" className="h-3.5 w-3.5" /></Link>
    </article>
  )
}

export default function Live() {
  const [ranges, setRanges] = useState(null)
  const [comps, setComps] = useState([])
  const [error, setError] = useState(null)
  const [updated, setUpdated] = useState(null)
  const [attempt, setAttempt] = useState(0)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [deploy, setDeploy] = useState(false)
  const search = useRef(null)
  useSlashFocus(search)
  useTick(10000)
  useEffect(() => {
    let active = true
    let busy = false
    const refresh = async () => {
      if (busy) return
      busy = true
      try {
        const data = await api.rangeBoard()
        if (active) { setRanges(data); setError(null); setUpdated(Date.now()) }
      } catch (err) { if (active) setError(err) }
      finally { busy = false }
    }
    refresh()
    const timer = setInterval(refresh, 10000)
    return () => { active = false; clearInterval(timer) }
  }, [attempt])
  useEffect(() => { api.comps().then(setComps).catch(() => {}) }, [])
  useEffect(() => { document.title = 'Active ranges · tezcatlipoca' }, [])
  const counts = filterCounts(ranges || [])
  const visible = filterRanges(ranges || [], query, filter)
  const retired = (ranges || []).filter((range) => range.state === 'retired')

  return (
    <main className="range-container">
      <TopNav />
      <header className="mb-6 flex flex-wrap items-end justify-between gap-5">
        <div>
          <h1 className="display text-4xl sm:text-5xl">Active ranges</h1>
          <p className="mt-2 text-sm text-muted">One card per range: what it is doing right now, what needs you, and the way in.</p>
        </div>
        <Button variant="primary" onClick={() => setDeploy(true)}><Icon name="plus" />Deploy range</Button>
      </header>
      <ErrorBanner error={error} onRetry={() => setAttempt((n) => n + 1)} />
      <Card className="mb-5 flex flex-wrap items-center justify-between gap-4 !rounded-3xl p-4 sm:px-6">
        <div className="flex flex-wrap gap-1" aria-label="Filter ranges">{[['all', 'All active'], ['attention', 'Needs attention'], ['operating', 'Operating']].map(([value, label]) => (
          <button key={value} onClick={() => setFilter(value)} aria-pressed={filter === value} aria-label={label}
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-xs font-semibold ${filter === value ? 'bg-accent-soft text-accent' : 'text-muted hover:bg-sunken'}`}>
            {label}
            {ranges && <span className={`tabular-nums ${value === 'attention' && counts.attention ? 'text-danger' : 'text-faint'}`}>{counts[value]}</span>}
          </button>
        ))}</div>
        <SearchInput value={query} onChange={setQuery} inputRef={search} placeholder="Search ranges, nodes, addresses…" hotkey="/" className="w-full sm:w-80" />
      </Card>
      {!ranges && !error && <div className="p-10 text-sm text-muted" role="status">Loading ranges…</div>}
      {ranges && !visible.length && <Card><EmptyState icon="boxes" title={counts.all ? 'No matching ranges' : 'No active ranges'}>{counts.all ? 'Try a different search or filter.' : 'Deploy a range to begin. Past operations remain available in Runs.'}</EmptyState></Card>}
      {!!visible.length && <div className="range-board">{visible.map((range) => <RangeCard key={range.id} range={range} />)}</div>}
      <p className="mt-4 text-xs text-faint">{error && updated ? 'Refresh failed. Showing saved results. ' : ''}{updated ? `Fetched ${agoMs(updated)}. ` : ''}Counts and placement are recorded workspace state; scoring state is a cached engine observation refreshed every ~15s.</p>
      {!!retired.length && <details className="mt-9">
        <summary className="cursor-pointer text-sm font-medium text-muted">Retired ranges <span className="ml-2 text-faint">{retired.length}</span></summary>
        <div className="mt-3 divide-y divide-line">{retired.map((range) => <Link key={range.id} to={`/ranges/${range.id}`} className="flex flex-wrap items-center gap-x-5 gap-y-1 py-3 text-sm hover:text-accent"><span>{rangeTitle(range)}</span><span className="font-mono text-xs text-faint">{shortRangeId(range)}</span><span className="ml-auto text-xs text-faint">{agoMs(range.latest_run?.ended_at)}</span></Link>)}</div>
      </details>}
      <NewRunModal open={deploy} onClose={() => setDeploy(false)} comps={comps} allowedActions={['deploy']} initial={{ action: 'deploy' }} onSubmitted={() => setAttempt((n) => n + 1)} />
    </main>
  )
}

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import TopNav from '../components/Nav'
import { Button, Card, ErrorBanner, SearchInput } from '../components/ui'
import { EmptyState, Icon, agoMs, useSlashFocus, useTick } from '../components/kit'
import { ACTION_LABEL, NewRunModal } from '../components/runui'
import { filterCounts, filterRanges, rangeIssues, rangeStatus, rangeTitle, shortRangeId } from '../rangeView'

const TONE = { danger: 'text-danger', active: 'text-accent', ok: 'text-fg', muted: 'text-muted' }
const DOT = { danger: 'bg-danger', active: 'animate-pulse bg-accent', ok: 'bg-accent', muted: 'bg-line' }
const LINK_LABEL = { scoreboard: 'Scoreboard', admin: 'Admin', portal: 'Team portal' }

function RangeRow({ range }) {
  const status = rangeStatus(range)
  // The status line already names the problem it came from; list only the others.
  const issues = rangeIssues(range).filter((issue) => issue.message !== status.issue?.message && !(status.kind === 'requests' && issue.kind === 'requests'))
  const others = (range.active_runs || []).slice(1)
  const latest = range.latest_run
  const links = Object.entries(range.links || {}).filter(([name]) => LINK_LABEL[name])
  return (
    <article className="range-row">
      <div className="min-w-0">
        <Link to={`/ranges/${range.id}`} className="text-lg font-semibold tracking-tight hover:text-accent">{rangeTitle(range)}</Link>
        <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-faint">
          {range.name && range.name !== range.comp_id && <span className="font-mono">{range.comp_id}</span>}
          <span>{range.deployed_at ? `deployed ${agoMs(range.deployed_at)}` : 'deploy time not recorded'}</span>
        </div>
      </div>
      <div className="min-w-0">
        <Link to={status.to} className={`inline-flex items-center gap-2 text-sm font-semibold hover:underline ${TONE[status.tone]}`}>
          <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[status.tone]}`} />{status.label}
        </Link>
        <div className="pl-4">
          {(status.detail || status.issue?.message) && <p className="mt-0.5 break-words text-xs text-muted">{status.detail || status.issue.message}</p>}
          {!!issues.length && <ul className="mt-1 space-y-0.5 text-xs text-danger">{issues.map((issue, index) => <li key={`${issue.kind}-${index}`}>{issue.message}</li>)}</ul>}
          {others.map((run) => <p key={run.id} className="mt-0.5 text-xs text-muted">Also: {ACTION_LABEL[run.action] || run.action} · {run.state}</p>)}
          {!range.active_runs?.length && latest && status.tone !== 'danger' && (
            <p className="mt-0.5 text-xs text-faint">Last: {ACTION_LABEL[latest.action] || latest.action} {latest.state} · {agoMs(latest.ended_at || latest.created_at)}</p>
          )}
        </div>
      </div>
      <div className="flex gap-5 text-sm">
        <span><b className="tabular-nums">{range.counts?.teams ?? '—'}</b> <span className="text-muted">teams</span></span>
        <span><b className="tabular-nums">{range.counts?.boxes ?? '—'}</b> <span className="text-muted">boxes</span></span>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs lg:justify-end">
        {links.length ? links.map(([name, url]) => (
          <a key={name} href={url} target="_blank" rel="noopener noreferrer" className="font-medium text-accent hover:underline">{LINK_LABEL[name]} ↗</a>
        )) : <span className="text-faint">No engine address recorded</span>}
      </div>
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
          <p className="mt-2 text-sm text-muted">Problems first, then ranges with operations in progress.</p>
        </div>
        <Button variant="primary" onClick={() => setDeploy(true)}><Icon name="plus" />Deploy range</Button>
      </header>
      <ErrorBanner error={error} onRetry={() => setAttempt((n) => n + 1)} />
      <Card className="overflow-hidden !rounded-3xl">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-line p-4 sm:px-6">
          <div className="flex flex-wrap gap-1" aria-label="Filter ranges">{[['all', 'All active'], ['attention', 'Needs attention'], ['operating', 'Operating']].map(([value, label]) => (
            <button key={value} onClick={() => setFilter(value)} aria-pressed={filter === value} aria-label={label}
              className={`inline-flex items-center gap-1.5 rounded-full px-3 py-2 text-xs font-semibold ${filter === value ? 'bg-accent-soft text-accent' : 'text-muted hover:bg-sunken'}`}>
              {label}
              {ranges && <span className={`tabular-nums ${value === 'attention' && counts.attention ? 'text-danger' : 'text-faint'}`}>{counts[value]}</span>}
            </button>
          ))}</div>
          <SearchInput value={query} onChange={setQuery} inputRef={search} placeholder="Search ranges, nodes, addresses…" hotkey="/" className="w-full sm:w-80" />
        </div>
        <div className="range-row-head hidden border-b border-line px-6 py-3 text-[10px] font-semibold uppercase tracking-widest text-faint lg:grid"><span>Range</span><span>Status</span><span>Recorded size</span><span className="text-right">Open</span></div>
        {!ranges && !error && <div className="p-10 text-sm text-muted" role="status">Loading ranges…</div>}
        {ranges && !visible.length && <EmptyState icon="boxes" title={counts.all ? 'No matching ranges' : 'No active ranges'}>{counts.all ? 'Try a different search or filter.' : 'Deploy a range to begin. Past operations remain available in Runs.'}</EmptyState>}
        {visible.map((range) => <RangeRow key={range.id} range={range} />)}
      </Card>
      <p className="mt-4 text-xs text-faint">{error && updated ? 'Refresh failed. Showing saved results. ' : ''}{updated ? `Fetched ${agoMs(updated)}. ` : ''}Status and counts are recorded workspace state; open a range for live scoring status.</p>
      {!!retired.length && <details className="mt-9">
        <summary className="cursor-pointer text-sm font-medium text-muted">Retired ranges <span className="ml-2 text-faint">{retired.length}</span></summary>
        <div className="mt-3 divide-y divide-line">{retired.map((range) => <Link key={range.id} to={`/ranges/${range.id}`} className="flex flex-wrap items-center gap-x-5 gap-y-1 py-3 text-sm hover:text-accent"><span>{rangeTitle(range)}</span><span className="font-mono text-xs text-faint">{shortRangeId(range)}</span><span className="ml-auto text-xs text-faint">{agoMs(range.latest_run?.ended_at)}</span></Link>)}</div>
      </details>}
      <NewRunModal open={deploy} onClose={() => setDeploy(false)} comps={comps} allowedActions={['deploy']} initial={{ action: 'deploy' }} onSubmitted={() => setAttempt((n) => n + 1)} />
    </main>
  )
}

// Range-scoped competition lifecycle and access. A parent may supply the polled view;
// legacy callers can continue passing rangeKey alone and this panel owns polling.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, Spinner } from './ui'
import { Icon, useConfirm, useToast } from './kit'

const STATE_CLS = {
  ready: 'bg-accent-soft text-accent', running: 'bg-accent-soft text-accent',
  paused: 'bg-sunken text-muted', ended: 'bg-sunken text-faint', unknown: 'bg-danger-soft text-danger',
}
const STATE_TEXT = {
  ready: 'ready — not started', running: 'scoring', paused: 'paused', ended: 'ended', unknown: 'engine unreachable',
}
const LINK_TEXT = { admin: 'Admin', announcements: 'Announcements', injects: 'Injects', portal: 'Team portal', scoreboard: 'Scoreboard' }
const ACTION_TEXT = { start: 'Start', freeze: 'Pause', resume: 'Resume', end: 'End' }

function Cred({ user, password, label }) {
  const [revealed, setRevealed] = useState(false)
  const toast = useToast()
  if (!password) return null
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl bg-sunken px-3 py-2 text-xs">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">{label}</span>
      <span className="font-mono text-muted">{user}</span>
      <span className="break-all font-mono text-muted" aria-label={revealed ? undefined : `${label} password hidden`}>{revealed ? password : '••••••••••••'}</span>
      <button type="button" className="rounded px-1 py-0.5 text-accent hover:underline" aria-label={`${revealed ? 'Hide' : 'Reveal'} ${label} password`}
        aria-pressed={revealed} onClick={() => setRevealed((value) => !value)}>{revealed ? 'Hide' : 'Reveal'}</button>
      <button type="button" className="rounded px-1 py-0.5 text-accent hover:underline" aria-label={`Copy ${label} password`}
        onClick={async () => {
          try { await navigator.clipboard.writeText(password); toast('Password copied') }
          catch { toast('Could not copy password. Reveal it to copy manually.', 'error') }
        }}>Copy</button>
    </div>
  )
}

// Keying the body prevents credentials, pending results or stale responses from a previous
// workspace appearing after navigation. Controlled view=null explicitly means no data yet.
export default function CompetitionPanel(props) {
  if (props.show === false || !props.rangeKey) return null
  return <CompetitionBody key={props.rangeKey} {...props} />
}

// section: 'all' (default), 'lifecycle' (state + start/pause/end) or 'access' (links +
// logins). A range page renders the two separately from one controlled view.
function CompetitionBody({ rangeKey, rangeId, compId, engineRef, onSubmitted, disabled = false, section = 'all',
  view: suppliedView, loading: suppliedLoading, error: suppliedError, onRefresh, recorded }) {
  const controlled = suppliedView !== undefined
  const [polledView, setPolledView] = useState(null)
  const [pollError, setPollError] = useState(null)
  const [pollLoading, setPollLoading] = useState(!controlled)
  const [refreshError, setRefreshError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [identity, setIdentity] = useState(null)
  const [pending, setPending] = useState(null)
  const [submitError, setSubmitError] = useState(null)
  const [submitted, setSubmitted] = useState(null)
  const busy = useRef(false)
  const alive = useRef(true)
  const poll = useRef(null)
  const confirm = useConfirm()
  const toast = useToast()
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    if (controlled) return undefined
    let stopped = false, timer
    const load = async () => {
      if (!stopped) setPollLoading(true)
      try {
        const next = await api.competition(rangeKey)
        if (!stopped) { setPolledView(next); setPollError(null) }
      } catch (error) { if (!stopped) setPollError(error) }
      finally { if (!stopped) setPollLoading(false) }
    }
    poll.current = load
    const tick = async () => {
      await load()
      if (!stopped) timer = setTimeout(tick, 10000)
    }
    tick()
    return () => { stopped = true; clearTimeout(timer); poll.current = null }
  }, [controlled, rangeKey])
  const rawView = controlled ? suppliedView : polledView
  // Reject a stale controlled payload for a different workspace rather than exposing its
  // credentials or accidentally enabling actions against the newly selected workspace.
  const mismatched = !!rawView?.range && rawView.range !== rangeKey
  const view = mismatched ? null : rawView
  const error = mismatched ? new Error('Competition data belongs to another workspace. Refresh this range.') : suppliedError || pollError || refreshError
  const loading = suppliedLoading ?? pollLoading
  useEffect(() => {
    // Legacy callers supply neither compId nor engineRef. Resolve the originating ref from
    // this exact workspace's range record; never use a competing range's latest operation.
    if ((compId || view?.comp_id) && (engineRef || view?.engine_ref)) return undefined
    let stopped = false
    api.ranges().then((records) => {
      const range = records.find((record) => record.key === rangeKey)
      if (stopped || !range) return
      const origin = (range.runs || []).find((run) => run.id === range.deploy_run_id)
        || (range.runs || []).find((run) => run.action === 'deploy' || run.action === 'scrim')
      setIdentity({ comp_id: range.comp_id, engine_ref: range.engine_ref || origin?.engine_ref, state: range.state })
    }).catch(() => { /* Missing identity is explained beside the disabled actions. */ })
    return () => { stopped = true }
  }, [rangeKey, compId, engineRef, view?.comp_id, view?.engine_ref])
  const actionCompId = compId || view?.comp_id || identity?.comp_id
  const actionEngineRef = engineRef || view?.engine_ref || identity?.engine_ref
  const identityMismatch = !!compId && !!view?.comp_id && compId !== view.comp_id
  const rangeState = view?.range_state || identity?.state
  const retired = rangeState === 'retired'
  // Scoring a range whose infrastructure is gone (or was never torn down cleanly) is not a
  // lifecycle step: the engine may answer from a leftover VM while its range is being
  // reclaimed. Start/Pause/Resume/End belong to a range that is actually there.
  const torn = retired || ['failed', 'teardown_failed'].includes(rangeState)
  const missingIdentity = !actionCompId || !actionEngineRef || identityMismatch
  const blocked = disabled || torn || missingIdentity || !!error || !!pending
  const refresh = useCallback(async () => {
    setRefreshError(null); setRefreshing(true)
    try { if (controlled) await onRefresh?.(); else await poll.current?.() }
    catch (nextError) { if (alive.current) setRefreshError(nextError) }
    finally { if (alive.current) setRefreshing(false) }
  }, [controlled, onRefresh])
  const submit = async (action) => {
    if (blocked || busy.current) return
    busy.current = true; setPending(action); setSubmitError(null); setSubmitted(null)
    let created
    try {
      created = await api.submitRun({
        comp_id: actionCompId, engine_ref: actionEngineRef, action,
        params: { comp_dir: rangeKey },
      })
    } catch (nextError) {
      if (alive.current) setSubmitError(nextError)
      busy.current = false
      if (alive.current) setPending(null)
      return
    }
    if (alive.current) {
      setSubmitted(created)
      toast(`${ACTION_TEXT[action]} operation submitted`)
    }
    try { await onSubmitted?.(created) }
    catch (nextError) { if (alive.current) setSubmitError(new Error(`Operation submitted, but refresh failed: ${nextError.message || nextError}`)) }
    if (alive.current) { setPending(null); refresh() }
    busy.current = false
  }
  const end = async () => {
    if (blocked || busy.current) return
    const ok = await confirm({
      title: 'End this range’s competition?',
      body: <><p>Captures the final scoreboard and inject state into this workspace's evidence directory, then freezes scoring. This is terminal; starting again requires a deliberate reset-event.</p><p className="mt-3 break-all font-mono text-xs">{rangeKey}</p></>,
      confirm: 'End the competition', danger: true,
    })
    if (ok && alive.current) await submit('end')
  }
  const lifecycle = section !== 'access'
  const access = section !== 'lifecycle'
  const operationHref = (run) => rangeId
    ? `/ranges/${rangeId}/activity?operation=${encodeURIComponent(run.id || run.run_id)}`
    : `/c/${encodeURIComponent(run.comp_id || actionCompId)}/runs/${encodeURIComponent(run.id || run.run_id)}`
  const links = Object.entries(view?.links || {})
  const creds = [view?.creds?.admin && ['admin', view.creds.admin], view?.creds?.inject && ['inject', view.creds.inject]].filter(Boolean)
  const noEngine = recorded === false
    ? 'This range records no infrastructure, so the engine address in its workspace is not a live link.'
    : 'No engine address is recorded.'
  if (!lifecycle) {
    return (
      <Card className="p-5">
        <h2 className="mb-3 text-base font-semibold">Access</h2>
        {mismatched && <ErrorBanner error={error} />}
        {!view && !mismatched && <p className="text-sm text-muted">{loading ? 'Loading access details…' : 'No access details are available for this workspace.'}</p>}
        {view && <>
          {links.length ? <ul className="space-y-1.5 text-sm">{links.map(([name, url]) => (
            <li key={name}><a className="flex items-center gap-1.5 text-accent hover:underline" href={url} target="_blank" rel="noopener noreferrer">
              <Icon name={name === 'portal' ? 'boxes' : 'overview'} className="h-3.5 w-3.5" />
              {LINK_TEXT[name] || name}<span className="ml-auto truncate font-mono text-[11px] text-faint">{url.replace(/^https?:\/\//, '')}</span>
            </a></li>
          ))}</ul> : <p className="text-sm text-muted">{noEngine}</p>}
          {!!creds.length && <div className="mt-4 space-y-2">{creds.map(([label, cred]) => <Cred key={`${label}-${cred.password}`} label={label} {...cred} />)}</div>}
        </>}
      </Card>
    )
  }
  return (
    <Card className="p-5">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h2 className={access ? 'label !mb-0' : 'text-base font-semibold'}>{access ? 'Competition & access' : 'Competition'}</h2>
        {view && <span className={`chip px-2.5 py-1 ${STATE_CLS[view.state] || STATE_CLS.unknown}`}>{STATE_TEXT[view.state] || view.state || 'unknown'}</span>}
        {(loading || refreshing) && <span role="status" aria-label="Refreshing competition"><Spinner /></span>}
        {view?.engine?.last_round?.ID > 0 && <span className="font-mono text-xs text-faint">round {view.engine.last_round.ID}</span>}
        {(!controlled || onRefresh) && <Button variant="ghost" className="ml-auto !py-1 text-xs" disabled={refreshing || loading} onClick={refresh}>Refresh</Button>}
      </div>
      {error && <><ErrorBanner error={error} onRetry={!controlled || onRefresh ? refresh : undefined} />
        {view && <p className="mb-3 text-xs text-muted">Refresh failed. The information below may be stale; lifecycle actions are disabled until a successful refresh.</p>}</>}
      <ErrorBanner error={submitError} onClose={() => setSubmitError(null)} />
      {pending && <p className="mb-3 text-sm text-accent" role="status">Submitting {ACTION_TEXT[pending].toLowerCase()} operation…</p>}
      {submitted && <p className="mb-3 text-sm text-muted" role="status">Operation submitted{(submitted.id || submitted.run_id) && <> · <Link className="font-mono text-accent hover:underline" to={operationHref(submitted)}>{submitted.id || submitted.run_id}</Link></>}. Submission does not mean scoring has changed yet.</p>}
      {!view && !loading && !error && <p className="text-sm text-muted">No competition or access details are available for this workspace.</p>}
      {view && <>
        {!torn && <div className="flex flex-wrap gap-2">
          {view.state === 'ready' && <Button variant="primary" disabled={blocked} onClick={() => submit('start')}><Icon name="play" />Start the competition</Button>}
          {view.state === 'running' && <Button disabled={blocked} onClick={() => submit('freeze')}><Icon name="pause" />Pause scoring</Button>}
          {view.state === 'paused' && <Button variant="primary" disabled={blocked} onClick={() => submit('resume')}><Icon name="play" />Resume scoring</Button>}
          {(view.state === 'running' || view.state === 'paused') && <Button variant="danger" disabled={blocked} onClick={end}><Icon name="warn" />End</Button>}
          {view.state === 'ended' && <p className="text-sm text-muted">The event has ended. Its final scoreboard is recorded in the workspace evidence directory.</p>}
        </div>}
        {retired ? <p className="mt-2 text-xs text-muted">This range is retired; lifecycle actions are disabled.</p>
          : torn ? <p className="mt-2 text-xs text-danger">This range's infrastructure is incomplete — its last teardown did not finish. Lifecycle actions are disabled; retry the teardown from the range page first.</p>
            : missingIdentity && <p className="mt-2 text-xs text-danger">Lifecycle actions require the range's competition identity and originating engine ref. Refresh the range details before submitting.</p>}
        {access && <>
          <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            {links.map(([name, url]) => (
              <a key={name} className="flex items-center gap-1.5 text-accent hover:underline" href={url} target="_blank" rel="noopener noreferrer">
                <Icon name={name === 'portal' ? 'boxes' : 'overview'} className="h-3.5 w-3.5" />
                {LINK_TEXT[name] || name}
              </a>
            ))}
          </div>
          {!links.length && <p className="mt-4 text-sm text-muted">{noEngine}</p>}
          <div className="mt-4 space-y-2">
            {creds.map(([label, cred]) => <Cred key={`${label}-${cred.password}`} label={label} {...cred} />)}
          </div>
        </>}
        {!view.reachable && view.state !== 'ended' && <p className="mt-3 text-xs text-muted">The engine is not answering. Any available workspace details are recorded state, not a current engine observation.</p>}
      </>}
    </Card>
  )
}

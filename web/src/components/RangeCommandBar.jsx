// The range page's command bar: scoring state with its lifecycle controls on the
// left, the operator's tools and credentials on the right — the two things an
// operator reaches for, in one strip directly under the range's title. The
// CompetitionPanel stays for the run page; this is its range-scoped successor and
// submits through rangeActionBody like every other range action.
import { useRef, useState } from 'react'
import { api } from '../api'
import { Button, Card, ErrorBanner, Spinner } from './ui'
import { Icon, useConfirm, useToast } from './kit'
import { Cred } from './CompetitionPanel'
import { rangeActionBody } from '../rangeModel'

const STATE_TEXT = { ready: 'Ready to start', running: 'Scoring', paused: 'Scoring paused', ended: 'Competition ended', unknown: 'Engine unreachable' }
const STATE_CLS = {
  ready: 'bg-accent-soft text-accent', running: 'bg-accent-soft text-accent',
  paused: 'bg-sunken text-muted', ended: 'bg-sunken text-faint', unknown: 'bg-danger-soft text-danger',
}
const TOOL_TEXT = { scoreboard: 'Scoreboard', admin: 'Admin', announcements: 'Announcements', injects: 'Injects', portal: 'Team portal' }

export default function RangeCommandBar({ range, competition, onSubmitted, disabled = false }) {
  const view = competition.view
  const loading = !view && !competition.error
  const retired = range.state === 'retired'
  // Scoring a range whose infrastructure is gone (or was never torn down cleanly) is
  // not a lifecycle step: the engine may answer from a leftover VM while its range is
  // being reclaimed. Start/Pause/Resume/End belong to a range that is actually there.
  const torn = ['failed', 'teardown_failed'].includes(range.state)
  const missingIdentity = !range.comp_id || !range.engine_ref
  const [pending, setPending] = useState(null)
  const [submitError, setSubmitError] = useState(null)
  const busy = useRef(false)
  const confirm = useConfirm()
  const toast = useToast()
  const blocked = disabled || retired || torn || missingIdentity || !!competition.error || !!pending || loading

  const submit = async (action) => {
    if (blocked || busy.current) return
    busy.current = true
    setPending(action)
    setSubmitError(null)
    try {
      const created = await api.submitRun(rangeActionBody(range, action))
      toast(`Operation ${created.id} submitted`)
      onSubmitted?.(created)
    } catch (error) {
      setSubmitError(error)
    } finally {
      busy.current = false
      setPending(null)
    }
  }

  const end = async () => {
    if (blocked) return
    const ok = await confirm({
      title: 'End this range’s competition?',
      body: <>Captures the final scoreboard and inject state into this workspace's evidence directory, then freezes scoring. This is terminal; starting again requires a deliberate reset-event.</>,
      confirm: 'End the competition', danger: true,
    })
    if (ok) await submit('end')
  }

  const state = view?.state
  const round = view?.engine?.last_round?.ID
  const links = Object.entries(view?.links || {}).filter(([name]) => TOOL_TEXT[name])
  const creds = [view?.creds?.admin && ['admin', view.creds.admin], view?.creds?.inject && ['inject', view.creds.inject]].filter(Boolean)

  if (retired) {
    // A retired range that still records this competition's kept templates has one
    // operation left — the header's Destroy becomes the reclaim — so the bar says so
    // instead of a plain archive note.
    const kept = range.kept_templates || []
    return (
      <Card className="flex flex-wrap items-center gap-3 p-4 text-sm text-muted sm:px-5">
        <Icon name="trash" className="h-4 w-4 shrink-0 text-faint" />
        {range.reclaimable_templates
          ? <span>This range is retired, but it still records this competition's kept templates{kept.length ? ` (vmids ${kept.map((template) => template.vmid).join(', ')})` : ''}. Destroy it to reclaim them; every other operation is disabled.</span>
          : <span>Competition archived — live scoring polling is stopped for this retired range. Its history and evidence remain available.</span>}
      </Card>
    )
  }

  return (
    <Card className="grid gap-0 overflow-hidden lg:grid-cols-[minmax(0,1fr)_auto]">
      {/* Scoring: the engine's live answer, and the controls that change it. */}
      <section className="min-w-0 p-4 sm:p-5" aria-label="Scoring">
        <div className="flex flex-wrap items-center gap-3">
          <span className="label !mb-0">Scoring</span>
          {view && <span className={`chip px-2.5 py-1 text-xs ${STATE_CLS[state] || STATE_CLS.unknown}`}>
            {state === 'running' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
            {STATE_TEXT[state] || state || 'unknown'}
          </span>}
          {round > 0 && <span className="font-mono text-xs text-faint">round {round}</span>}
          {(loading || competition.error) && <Spinner />}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {pending && <p className="text-sm text-accent" role="status">Submitting {pending}…</p>}
          {state === 'ready' && <Button variant="primary" disabled={blocked} onClick={() => submit('start')}><Icon name="play" />Start the competition</Button>}
          {state === 'running' && <Button disabled={blocked} onClick={() => submit('freeze')}><Icon name="pause" />Pause scoring</Button>}
          {state === 'paused' && <Button variant="primary" disabled={blocked} onClick={() => submit('resume')}><Icon name="play" />Resume scoring</Button>}
          {(state === 'running' || state === 'paused') && <Button variant="danger" disabled={blocked} onClick={end}><Icon name="warn" />End</Button>}
          {state === 'ended' && <p className="text-sm text-muted">The event has ended. Its final scoreboard is recorded in the workspace evidence directory.</p>}
          {state === 'unknown' && view && !view.reachable && <p className="text-sm text-muted">The engine is not answering; lifecycle actions are unavailable until it does.</p>}
        </div>
        <ErrorBanner error={submitError || competition.error} onClose={submitError ? () => setSubmitError(null) : undefined} />
        {torn && <p className="mt-2 text-xs text-danger">This range's infrastructure is incomplete — its last teardown did not finish. Retry the teardown before driving the competition.</p>}
        {!torn && missingIdentity && <p className="mt-2 text-xs text-danger">Lifecycle actions require the range's competition identity and originating engine ref. Refresh the range details before submitting.</p>}
        {view && !view.reachable && state !== 'unknown' && state !== 'ended' && <p className="mt-2 text-xs text-muted">The engine is not answering. What shows here is recorded workspace state, not a current engine observation.</p>}
      </section>

      {/* Tools: the operator's way in, and the logins they need on the way. */}
      <section className="min-w-0 border-t border-line p-4 sm:p-5 lg:border-t-0 lg:border-l" aria-label="Tools and credentials">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="label !mb-0">Tools</span>
          {!!links.length && links.map(([name, url]) => (
            <a key={name} href={url} target="_blank" rel="noopener noreferrer" className="btn !px-3.5 !py-1.5 text-xs">
              <Icon name={name === 'portal' ? 'boxes' : 'overview'} className="h-3.5 w-3.5" />{TOOL_TEXT[name]} ↗
            </a>
          ))}
        </div>
        {loading && <p className="text-sm text-muted">Checking access details…</p>}
        {!loading && !links.length && <p className="text-xs text-muted">{range.has_recorded_vms === false ? 'This range records no infrastructure, so there is no live engine to open.' : 'No engine addresses are recorded.'}</p>}
        {!!creds.length && <div className="flex flex-wrap gap-2">{creds.map(([label, cred]) => <Cred key={`${label}-${cred.password}`} label={label} {...cred} />)}</div>}
      </section>
    </Card>
  )
}

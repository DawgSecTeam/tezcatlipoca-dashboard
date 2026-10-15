// The range's competition lifecycle, on the run page of a deploy: what the scoring engine
// reports right now, Start/Pause/Resume/End, and the operator's way into the Quotient
// admin pages (announcements, injects) with the logins that work there.
//
// The payload comes from GET /api/competition?range=<workspace>, which reads the
// workspace's estate state document (the legacy .deploy_state.json for a pre-v0.3 range)
// and polls the engine's API as the `scoring` automation
// account — so a Start here can never evict the operator's `admin` browser session.
import { useEffect, useState } from 'react'
import { api } from '../api'
import { Button, Card, ErrorBanner } from './ui'
import { Icon, useConfirm, useToast } from './kit'

const STATE_CLS = {
  ready: 'bg-accent-soft text-accent',
  running: 'bg-accent-soft text-accent',
  paused: 'bg-sunken text-muted',
  ended: 'bg-sunken text-faint',
  unknown: 'bg-danger-soft text-danger',
}
const STATE_TEXT = {
  ready: 'deployed — not started',
  running: 'scoring',
  paused: 'frozen',
  ended: 'ended',
  unknown: 'engine unreachable',
}

function Cred({ user, password, label }) {
  const toast = useToast()
  if (!password) return null
  return (
    <div className="flex items-center gap-2 font-mono text-xs">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-faint">{label}</span>
      <span className="text-muted">{user}</span>
      <span className="text-muted">/ {password}</span>
      <button className="text-faint transition hover:text-accent" title="Copy password"
        onClick={async () => {
          try { await navigator.clipboard.writeText(password); toast('Password copied') }
          catch { toast('Copy failed', 'error') }
        }}>copy</button>
    </div>
  )
}

// One lifecycle action is a run: the runner provisions a worktree at the engine ref the
// deploy used — logging, events and cancel come free, and competition-ops.py is the exact
// command line an operator would type.

export default function CompetitionPanel({ rangeKey, show = true }) {
  const [view, setView] = useState(null)
  const [err, setErr] = useState(null)
  const confirm = useConfirm()
  const toast = useToast()
  const reload = () => api.competition(rangeKey).then((v) => { setView(v); setErr(null) })
    .catch((e) => { if (e.status === 404) setView(false); else setErr(e) })
  useEffect(() => {
    if (!rangeKey || !show) return undefined
    reload()
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [rangeKey, show]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!show || !rangeKey || view === false || (!view && !err)) return null
  const lifecycle = (action) => async () => {
    try {
      await api.submitRun({ action, params: { comp_dir: rangeKey } })
      toast(`${action[0].toUpperCase()}${action.slice(1)} submitted`)
      reload()
    } catch (e) { toast(e.message, 'error') }
  }
  const end = async () => {
    const ok = await confirm({
      title: 'End the competition?',
      body: 'Captures the final scoreboard + inject state into the range\'s evidence dir, then freezes scoring. This is terminal — starting again afterwards needs a deliberate reset-event.',
      confirm: 'End the competition', danger: true,
    })
    if (ok) lifecycle('end')()
  }

  return (
    <Card className="p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h2 className="label !mb-0">Competition</h2>
        {view
          ? <span className={`chip px-2.5 py-1 ${STATE_CLS[view.state]}`}>
              {view.state === 'running' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
              {STATE_TEXT[view.state]}
            </span>
          : <ErrorBanner error={err} onRetry={reload} />}
        {view?.engine?.last_round != null && (
          <span className="font-mono text-xs text-faint" title="The scoring engine's last completed round">
            round {view.engine.last_round}
          </span>
        )}
      </div>

      {view && (
        <>
          <div className="flex flex-wrap gap-2">
            {view.state === 'ready' && (
              <Button variant="primary" onClick={lifecycle('start')}><Icon name="play" />Start the competition</Button>
            )}
            {view.state === 'running' && (
              <Button onClick={lifecycle('freeze')}><Icon name="pause" />Pause scoring</Button>
            )}
            {view.state === 'paused' && (
              <Button variant="primary" onClick={lifecycle('resume')}><Icon name="play" />Resume scoring</Button>
            )}
            {(view.state === 'running' || view.state === 'paused') && (
              <Button variant="danger" onClick={end}><Icon name="warn" />End</Button>
            )}
            {view.state === 'ended' && (
              <p className="text-sm text-faint">The event is over — the final scoreboard is in the range's evidence dir. Tear the range down with Destroy when done.</p>
            )}
          </div>

          <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 text-sm">
            {Object.entries(view.links || {}).map(([name, url]) => (
              <a key={name} className="flex items-center gap-1.5 text-accent hover:underline"
                href={url} target="_blank" rel="noopener noreferrer">
                <Icon name={name === 'portal' ? 'boxes' : 'overview'} className="h-3.5 w-3.5" />
                {name === 'admin' ? 'Admin' : name === 'announcements' ? 'Announcements'
                  : name === 'injects' ? 'Injects' : name === 'portal' ? 'Team portal' : 'Scoreboard'}
              </a>
            ))}
          </div>
          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1.5">
            {view.creds?.admin && <Cred label="admin" {...view.creds.admin} />}
            {view.creds?.inject && <Cred label="inject" {...view.creds.inject} />}
          </div>
          {!view.reachable && view.state !== 'ended' && (
            <p className="mt-3 text-xs text-faint">
              The engine is not answering right now — the state above falls back to this range's
              deploy state; the buttons still work.
            </p>
          )}
        </>
      )}
    </Card>
  )
}

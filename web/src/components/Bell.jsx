// The operator's reset-request bell: a fixed top-right button with the count of pending
// requests not yet seen, and a dropdown listing them (across every live range) with
// quick approve/deny. Unseen is a client-side notion — one shared token, so there is no
// per-user inbox to store; last-seen rides localStorage.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Button } from './ui'
import { Icon, agoMs, useToast } from './kit'

const SEEN_KEY = 'tez-requests-seen'

const seenTs = () => {
  try { return Number(localStorage.getItem(SEEN_KEY)) || 0 } catch { return 0 }
}

const REQUEST_CLS = {
  pending: 'bg-accent-soft text-accent',
  approved: 'bg-accent-soft text-accent',
  done: 'bg-sunken text-muted',
  denied: 'bg-sunken text-faint',
  failed: 'bg-danger-soft text-danger',
}

export default function Bell() {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState([])
  const [seen, setSeen] = useState(seenTs)
  const box = useRef(null)
  const toast = useToast()
  const reload = useCallback(() => api.requests().then(setRows).catch(() => {}), [])
  useEffect(() => {
    reload()
    const t = setInterval(reload, 15000)
    return () => clearInterval(t)
  }, [reload])
  // click-outside closes the dropdown
  useEffect(() => {
    if (!open) return undefined
    const close = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  const unread = rows.filter((r) => r.status === 'pending' && (r.created_ts || 0) > seen).length
  const markSeen = () => {
    const now = Date.now() / 1000
    setSeen(now)
    try { localStorage.setItem(SEEN_KEY, String(now)) } catch { /* storage blocked */ }
  }
  const act = (id, fn, label) => async () => {
    try { await fn(id); toast(label); reload() } catch (e) { toast(e.message, 'error') }
  }
  return (
    <div className="fixed right-6 top-5 z-50" ref={box}>
      <button
        onClick={() => { setOpen((o) => { if (!o) markSeen(); return !o }) }}
        title="Reset requests from the teams"
        aria-label={`Reset requests${unread ? ` (${unread} new)` : ''}`}
        className="relative flex h-10 w-10 items-center justify-center rounded-full bg-sunken text-muted shadow-inset transition hover:text-fg">
        <Icon name="bell" className="h-5 w-5" />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1 text-[11px] font-bold text-on-accent">
            {unread}
          </span>
        )}
      </button>
      {open && (
        <aside className="panel absolute right-0 top-12 w-[24rem] p-5">
          <h3 className="label !mb-3">Reset requests</h3>
          {!rows.length ? (
            <p className="text-sm text-faint">No team has asked for anything yet.</p>
          ) : (
            <ul className="max-h-[60vh] space-y-2 overflow-auto">
              {rows.slice(0, 20).map((r) => (
                <li key={r.id} className="rounded-2xl bg-sunken px-4 py-2.5 text-sm shadow-inset">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold">{r.team}</span>
                    <span className="font-mono text-xs text-muted">{r.box}</span>
                    <span className={`chip ml-auto px-2 py-0.5 text-[11px] ${REQUEST_CLS[r.status] || 'bg-sunken text-muted'}`}>
                      {r.status === 'approved' && <span className="mr-1 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
                      {r.status}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-faint">
                    <span>{r.comp_id} · by {r.requested_by || '?'} · {r.requested_at || agoMs(r.created_ts)}</span>
                    <Link to={`/c/${r.comp_id}`} className="ml-auto text-accent hover:underline"
                      onClick={() => setOpen(false)}>team page</Link>
                  </div>
                  {r.status === 'pending' && (
                    <div className="mt-2 flex justify-end gap-2">
                      <Button variant="ghost" className="!py-1 text-xs" onClick={act(r.id, api.approveRequest, 'Reset approved')}>
                        Approve
                      </Button>
                      <Button variant="ghost" className="!py-1 text-xs" onClick={act(r.id, api.denyRequest, 'Reset denied')}>
                        Deny
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </aside>
      )}
    </div>
  )
}

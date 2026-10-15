// Reset-request inbox. Workspace keys resolve through /api/ranges to stable range IDs;
// a competition ID alone is never enough to pick one of its concurrent ranges.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Button, ErrorBanner } from './ui'
import { Icon, agoMs, useToast } from './kit'

const SEEN_KEY = 'tez-requests-seen'
const seenTs = () => {
  try { return Number(localStorage.getItem(SEEN_KEY)) || 0 } catch { return 0 }
}
const REQUEST_CLS = {
  pending: 'bg-accent-soft text-accent', approved: 'bg-accent-soft text-accent',
  done: 'bg-sunken text-muted', denied: 'bg-sunken text-faint', failed: 'bg-danger-soft text-danger',
}

export default function Bell() {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState([])
  const [ranges, setRanges] = useState([])
  const [error, setError] = useState(null)
  const [rangeError, setRangeError] = useState(null)
  const [pending, setPending] = useState(null)
  const [seen, setSeen] = useState(seenTs)
  const box = useRef(null)
  const busy = useRef(false)
  const mounted = useRef(true)
  const toast = useToast()
  const reload = useCallback(async () => {
    await Promise.all([
      api.requests().then((requests) => { if (mounted.current) { setRows(requests); setError(null) } })
        .catch((nextError) => { if (mounted.current) setError(nextError) }),
      api.ranges().then((records) => { if (mounted.current) { setRanges(records); setRangeError(null) } })
        .catch((nextError) => { if (mounted.current) setRangeError(nextError) }),
    ])
  }, [])
  useEffect(() => {
    mounted.current = true
    let stopped = false, timer
    const tick = async () => { await reload(); if (!stopped) timer = setTimeout(tick, 15000) }
    tick()
    return () => { stopped = true; mounted.current = false; clearTimeout(timer) }
  }, [reload])
  useEffect(() => {
    if (!open) return undefined
    const close = (event) => { if (box.current && !box.current.contains(event.target)) setOpen(false) }
    const escape = (event) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', escape) }
  }, [open])
  const unread = rows.filter((request) => request.status === 'pending' && (request.created_ts || 0) > seen).length
  const toggle = () => {
    if (!open) {
      const now = Date.now() / 1000
      setSeen(now)
      try { localStorage.setItem(SEEN_KEY, String(now)) } catch { /* storage blocked */ }
      reload()
    }
    setOpen(!open)
  }
  const act = (request, approve) => async () => {
    if (busy.current) return
    busy.current = true; setPending(request.id)
    try {
      await (approve ? api.approveRequest : api.denyRequest)(request.id)
      toast(approve ? 'Reset approved' : 'Reset denied')
      await reload()
    } catch (nextError) { toast(nextError.message, 'error') }
    finally { busy.current = false; if (mounted.current) setPending(null) }
  }
  const byWorkspace = new Map(ranges.map((range) => [range.key, range]))
  return (
    <div className="relative inline-flex" ref={box}>
      <button type="button" onClick={toggle} title="Reset requests from the teams"
        aria-label={`Reset requests${unread ? ` (${unread} new)` : ''}`} aria-expanded={open} aria-controls="reset-request-inbox"
        className="relative flex h-10 w-10 items-center justify-center rounded-full bg-sunken text-muted shadow-inset transition hover:text-fg">
        <Icon name="bell" className="h-5 w-5" />
        {unread > 0 && <span className="absolute -right-0.5 -top-0.5 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent px-1 text-[11px] font-bold text-on-accent">{unread}</span>}
      </button>
      {open && <aside id="reset-request-inbox" aria-label="Reset requests" className="panel absolute right-0 top-12 z-30 w-[min(26rem,calc(100vw-3rem))] p-4">
        <h3 className="label !mb-3">Reset requests</h3>
        <ErrorBanner error={error} onRetry={reload} />
        {rangeError && <p className="mb-3 text-xs text-danger">Range links could not be refreshed. <button type="button" className="underline" onClick={reload}>Retry</button></p>}
        {!rows.length ? <p className="text-sm text-faint">{error ? 'Requests are unavailable.' : 'No team has asked for a reset yet.'}</p> : (
          <ul className="max-h-[60vh] space-y-2 overflow-auto">
            {rows.slice(0, 20).map((request) => {
              const range = byWorkspace.get(request.range_key)
              const stableId = range?.comp_id === request.comp_id ? range.id : null
              return (
                <li key={request.id} className="rounded-xl bg-sunken px-3 py-2.5 text-sm shadow-inset">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono font-semibold">{request.team}</span>
                    <span className="font-mono text-xs text-muted">{request.box}</span>
                    <span className={`chip ml-auto px-2 py-0.5 text-[11px] ${REQUEST_CLS[request.status] || 'bg-sunken text-muted'}`}>{request.status}</span>
                  </div>
                  <p className="mt-1 text-xs text-muted">{request.comp_id} · by {request.requested_by || '?'} · {request.requested_at || agoMs(request.created_ts)}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    {stableId ? <Link to={`/ranges/${encodeURIComponent(stableId)}/teams`} className="text-accent hover:underline" onClick={() => setOpen(false)}>Range teams</Link>
                      : <span className="text-faint" title={request.range_key}>Range link unavailable</span>}
                    {request.approved_run && <Link to={`/c/${encodeURIComponent(request.comp_id)}/runs/${encodeURIComponent(request.approved_run)}`} className="text-accent hover:underline" onClick={() => setOpen(false)}>Reset operation</Link>}
                  </div>
                  <details className="mt-1 text-[11px] text-faint"><summary className="cursor-pointer">Workspace</summary><p className="mt-1 break-all font-mono">{request.range_key || 'Unknown'}</p></details>
                  {(request.status === 'pending' || request.status === 'failed') && <div className="mt-2 flex justify-end gap-2">
                    <Button variant="ghost" className="!py-1 text-xs" disabled={pending != null || range?.state === 'retired'} onClick={act(request, true)}>{pending === request.id ? 'Submitting…' : request.status === 'failed' ? 'Retry reset' : 'Approve'}</Button>
                    <Button variant="ghost" className="!py-1 text-xs" disabled={pending != null} onClick={act(request, false)}>Deny</Button>
                  </div>}
                </li>
              )
            })}
          </ul>
        )}
      </aside>}
    </div>
  )
}

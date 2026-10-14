// Run-facing primitives shared by the Live and Runs pages and the deploy panel: state
// pills, the stage progress strip, offset-polled log views, the event list, the artifacts
// tree, and the two submit forms (new run / reset a box).
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, streamRunEvents } from '../api'
import { Button, ErrorBanner, Field, Modal, Spinner, inputCls } from './ui'
import { Icon, Stepper, fmtDur, toMs, useConfirm, useTick, useToast } from './kit'

export const ACTION_LABEL = {
  deploy: 'Deploy', plan: 'Dry run', verify: 'Verify',
  destroy: 'Destroy', scrim: 'Scrim', redeploy: 'Redeploy',
}
export const RUN_ACTIONS = Object.keys(ACTION_LABEL)

// The eight deploy phases (create-competition.py) and the eight scrim stages
// (run-agent-scrim.py), in order.
export const DEPLOY_STAGES = ['Cleanup', 'Engine template', 'Prepare engine', 'Golden set', 'Firewall', 'Repair sweep', 'Domains/final', 'Seed']
export const SCRIM_STAGES = ['author', 'deploy', 'verify', 'blues', 'red', 'event', 'capture', 'teardown']

export const stagesFor = (action) =>
  action === 'scrim' ? SCRIM_STAGES
    : action === 'destroy' ? ['Teardown']
      : action === 'verify' ? ['Verify']
        : DEPLOY_STAGES

const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '')

// ── state pill ─────────────────────────────────────────────────────────────────────────────

const STATE_CLS = {
  queued: 'bg-sunken text-muted',
  running: 'bg-accent-soft text-accent',
  succeeded: 'bg-accent-soft text-accent',
  failed: 'bg-danger-soft text-danger',
  cancelled: 'bg-sunken text-faint',
}

export function StatePill({ state }) {
  return (
    <span className={`chip px-2.5 py-1 ${STATE_CLS[state] || 'bg-sunken text-muted'}`}>
      {state === 'running' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
      {state}
    </span>
  )
}

// ── run actions: submit a follow-up run, teardown, cancel ──────────────────────────────────

// The shared action logic for the Live card menu and the scrim page's action row.
export function useRunActions(run, onChanged) {
  const toast = useToast()
  const ask = useConfirm()
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
  const live = !!run && (run.state === 'running' || run.state === 'queued')
  return { submit, destroy, cancel, live }
}

// ── stage progress strip ───────────────────────────────────────────────────────────────────

// Seconds spent per stage, keyed by normalized stage name: consecutive stage events bound
// each stage; the last one runs to the run's end (or now).
function stageDurations(stageEvents, endedAt) {
  const out = {}
  stageEvents.forEach((ev, i) => {
    const start = toMs(ev.ts)
    if (!start || !ev.stage) return
    const next = stageEvents[i + 1]
    const end = next ? toMs(next.ts) : (endedAt ? toMs(endedAt) : Date.now())
    out[norm(ev.stage)] = (end - start) / 1000
  })
  return out
}

export function StageStrip({ action, stage, state, endedAt, stageEvents = [] }) {
  useTick(1000) // the current stage's elapsed keeps moving
  const current = stage || stageEvents.at(-1)?.stage || ''
  let stages = stagesFor(action)
  if (!stages.length && current) stages = [current]
  const durs = stageDurations(stageEvents, endedAt)
  const succeeded = state === 'succeeded'
  const failed = state === 'failed' || state === 'cancelled'
  let idx = stages.findIndex((s) => norm(s) === norm(current))
  if (idx < 0 && current) idx = stages.findIndex((s) => norm(current).includes(norm(s)) || norm(s).includes(norm(current)))
  const curDur = current ? durs[norm(stages[idx] || current)] : null

  return (
    <div>
      {/* Eight stages don't fit one row of a half-width card — wrap to a 2x4 grid. */}
      <div className={stages.length > 4 ? 'grid grid-cols-4 gap-x-1 gap-y-2' : 'flex items-stretch gap-1'}>
        {stages.map((s, i) => {
          const done = succeeded || (idx >= 0 && i < idx)
          const isCur = i === idx
          const d = durs[norm(s)]
          return (
            <div key={s} className="min-w-0 flex-1" title={`${s}${d != null ? ` · ${fmtDur(d)}` : ''}`}>
              <div className={`h-2.5 rounded-full transition-colors ${
                isCur && failed ? 'bg-danger'
                  : isCur ? 'animate-pulse bg-accent'
                    : done ? 'bg-accent' : 'bg-line'}`} />
              <div className={`mt-1 truncate text-[9px] font-semibold uppercase tracking-wider ${
                isCur && failed ? 'text-danger' : isCur ? 'text-accent' : done ? 'text-muted' : 'text-faint'}`}>{s}</div>
              <div className="truncate text-[9px] tabular-nums text-faint">{d != null ? fmtDur(d) : ''}</div>
            </div>
          )
        })}
      </div>
      {(current || !stages.length) && (
        <div className="mt-2 flex items-center gap-2 text-xs text-muted">
          <span className={`h-2 w-2 shrink-0 rounded-full ${failed ? 'bg-danger' : 'animate-pulse bg-accent'}`} />
          <span className="min-w-0 truncate">{current || 'waiting for the first stage'}</span>
          {curDur != null && <span className="ml-auto shrink-0 tabular-nums text-faint">{fmtDur(curDur)}</span>}
        </div>
      )}
    </div>
  )
}

// ── log: offset polling of /runs/{id}/log ─────────────────────────────────────────────────

export function useRunLog(runId) {
  const [log, setLog] = useState({ text: '', running: true, returncode: null })
  useEffect(() => {
    if (!runId) return
    let stop = false, timer, offset = 0
    setLog({ text: '', running: true, returncode: null })
    const tick = async () => {
      try {
        const r = await api.runLog(runId, offset)
        offset = r.offset
        setLog((l) => ({ text: l.text + (r.text || ''), running: !!r.running, returncode: r.returncode }))
        if (!r.running && !r.text) return
      } catch { /* server restarted; keep polling */ }
      if (!stop) timer = setTimeout(tick, 1500)
    }
    tick()
    return () => { stop = true; clearTimeout(timer) }
  }, [runId])
  return log
}

export function LogView({ log, height = '60vh' }) {
  const [follow, setFollow] = useState(true)
  const pre = useRef()
  useEffect(() => { if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [log.text, follow])
  return (
    <div>
      <div className="mb-3 flex items-center gap-3 text-sm">
        {log.running ? <><Spinner /><span className="text-muted">Running…</span></>
          : <span className={`chip px-3 py-1 ${log.returncode === 0 ? 'bg-accent-soft text-accent' : 'bg-danger-soft text-danger'}`}>
              {log.returncode === 0 ? 'Finished' : `Failed · exit ${log.returncode}`}
            </span>}
        <label className="ml-auto flex shrink-0 items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-[var(--accent)]" /> Follow
        </label>
      </div>
      <pre ref={pre} style={{ height }} className="overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">{log.text || ' '}</pre>
    </div>
  )
}

export function RunLogModal({ open, onClose, title, log }) {
  return (
    <Modal wide open={open} onClose={onClose} title={title || 'Run log'}>
      {log && <LogView log={log} />}
    </Modal>
  )
}

// Small always-following tail for run cards.
export function LogTail({ text, height = '8rem' }) {
  const pre = useRef()
  useEffect(() => { if (pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [text])
  return <pre ref={pre} style={{ height }} className="overflow-auto rounded-2xl bg-sunken p-3 font-mono text-[11px] leading-relaxed text-fg shadow-inset">{text || ' '}</pre>
}

// ── live events: SSE with an offset-polling fallback ───────────────────────────────────────

export function useRunEvents(runId, onEvent, enabled = true) {
  const [updated, setUpdated] = useState(null) // ms timestamp of the last event or poll
  const cb = useRef(onEvent)
  cb.current = onEvent
  useEffect(() => {
    if (!runId || !enabled) return
    let stop = false, ctrl, timer
    let seq = 0
    const take = (ev) => {
      if (ev?.seq != null) seq = Math.max(seq, ev.seq)
      setUpdated(Date.now())
      cb.current?.(ev)
    }
    const poll = async () => {
      try {
        const r = await api.runEvents(runId, seq)
        seq = Math.max(seq, r.next || 0)
        r.events?.forEach(take)
        if (!r.events?.length) setUpdated(Date.now())
      } catch { /* transient */ }
    }
    const loop = async () => {
      while (!stop) {
        ctrl = new AbortController()
        try {
          await streamRunEvents(runId, take, { since: seq, signal: ctrl.signal })
        } catch { /* stream refused or dropped; fall back to polling */ }
        if (stop) return
        await poll()
        await new Promise((r) => { timer = setTimeout(r, 3000) })
      }
    }
    loop()
    return () => { stop = true; ctrl?.abort(); clearTimeout(timer) }
  }, [runId, enabled])
  return { updated }
}

// ── events + artifacts ─────────────────────────────────────────────────────────────────────

export function EventList({ events }) {
  if (!events?.length) return <p className="text-xs text-faint">No events yet.</p>
  return (
    <ul className="space-y-1.5">
      {events.map((ev) => (
        <li key={ev.seq} className="flex items-baseline gap-2 text-xs">
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-faint">{ev.ts ? new Date(toMs(ev.ts)).toLocaleTimeString() : ''}</span>
          <span className={`chip shrink-0 px-1.5 py-0 ${ev.kind === 'stage' ? 'bg-accent-soft text-accent' : 'bg-sunken text-faint'}`}>{ev.kind}</span>
          <span className="min-w-0 truncate text-muted">
            {ev.stage && <b className="font-semibold text-fg">{ev.stage} </b>}
            {typeof ev.data === 'string' ? ev.data : ev.data?.text || ev.data?.message || (ev.data ? JSON.stringify(ev.data) : '')}
          </span>
        </li>
      ))}
    </ul>
  )
}

// The artifacts endpoint returns an evidence tree; accept the plausible shapes and
// flatten to rows. Leaf fields beyond path/size are surfaced as a title tooltip.
function flattenArtifacts(node, prefix = '', out = []) {
  if (!node) return out
  if (Array.isArray(node)) { node.forEach((n) => flattenArtifacts(n, prefix, out)); return out }
  if (typeof node === 'string') { out.push({ path: prefix ? `${prefix}/${node}` : node }); return out }
  const name = node.path || node.name || ''
  const full = prefix && name ? `${prefix}/${name}` : name
  const kids = node.children || node.files || node.items
  if (kids) flattenArtifacts(kids, full, out)
  else if (full) out.push({ ...node, path: full })
  return out
}

export function ArtifactsList({ runId }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    setRows(null); setErr(null)
    api.runArtifacts(runId)
      .then((d) => setRows(flattenArtifacts(d?.artifacts ?? d?.files ?? d?.items ?? d?.tree ?? d?.children ?? d)))
      .catch(setErr)
  }, [runId])
  if (err) return <ErrorBanner error={err} />
  if (!rows) return <Spinner />
  if (!rows.length) return <p className="text-xs text-faint">No artifacts collected yet.</p>
  // A row's click fetches the file (the API needs the bearer header, so a plain href
  // cannot do it) and hands the blob to the browser as a download.
  const grab = async (path) => {
    try {
      const blob = await api.runArtifactFile(runId, path)
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = path.split('/').pop()
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) { setErr(e) }
  }
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li key={r.path}>
          <button onClick={() => grab(r.path)} title={`Download ${r.path}`}
            className="flex w-full items-center gap-2 rounded-xl px-2 py-0.5 text-left text-xs transition hover:bg-sunken">
            <Icon name="packet" className="h-3.5 w-3.5 shrink-0 text-faint" />
            <span className="min-w-0 truncate font-mono text-muted">{r.path}</span>
            {(r.bytes ?? r.size) != null && <span className="ml-auto shrink-0 tabular-nums text-faint">{r.bytes ?? r.size}</span>}
          </button>
        </li>
      ))}
    </ul>
  )
}

// ── submit forms ───────────────────────────────────────────────────────────────────────────

// The run submit form: comp picker, action, engine ref, teams, scoring vmid.
export function NewRunModal({ open, onClose, comps, initial = {}, onSubmitted }) {
  const [form, setForm] = useState({ comp_id: '', action: 'deploy', engine_ref: 'main', teams: 2, scoring_vmid: '' })
  const [err, setErr] = useState(null)
  useEffect(() => {
    if (open) {
      setForm({ comp_id: '', action: 'deploy', engine_ref: 'main', teams: 2, scoring_vmid: '', ...initial })
      setErr(null)
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const submit = async (e) => {
    e.preventDefault()
    try {
      const run = await api.submitRun({
        comp_id: form.comp_id,
        action: form.action,
        engine_ref: form.engine_ref,
        teams: Number(form.teams) || 0,
        scoring_vmid: form.scoring_vmid ? Number(form.scoring_vmid) : null,
      })
      onSubmitted?.(run)
      onClose()
    } catch (e2) { setErr(e2) }
  }
  return (
    <Modal open={open} onClose={onClose} title="New run">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <Field label="Competition">
          <select className={inputCls} value={form.comp_id} onChange={set('comp_id')} required autoFocus>
            <option value="" disabled>Choose a competition…</option>
            {(comps || []).map((c) => <option key={c.id} value={c.id}>{c.name} ({c.id})</option>)}
          </select>
        </Field>
        <Field label="Action">
          <select className={inputCls} value={form.action} onChange={set('action')}>
            {RUN_ACTIONS.map((a) => <option key={a} value={a}>{ACTION_LABEL[a]} ({a})</option>)}
          </select>
        </Field>
        <div className="grid grid-cols-3 gap-4">
          <Field label="Engine ref" hint="Branch, SHA or tag">
            <input className={`${inputCls} font-mono`} value={form.engine_ref} onChange={set('engine_ref')} placeholder="main" />
          </Field>
          <Field label="Teams"><Stepper value={form.teams} onChange={(v) => setForm({ ...form, teams: v })} /></Field>
          <Field label="Engine VMID" hint="Free Proxmox VMID">
            <input inputMode="numeric" className={`${inputCls} font-mono`} value={form.scoring_vmid} placeholder="auto"
              onChange={(e) => setForm({ ...form, scoring_vmid: e.target.value.replace(/\D/g, '') })} />
          </Field>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.comp_id}><Icon name="rocket" />Submit run</Button>
        </div>
      </form>
    </Modal>
  )
}

// "Reset a box…" — redeploy action with box/team/mode fields.
const REDEPLOY_MODES = ['reset', 'rebuild', 'rollback-ready', 'rollback-base', 'reconfigure', 'resync', 'engine-recovery']

export function ResetBoxModal({ open, onClose, run, initial = {}, onSubmitted }) {
  const [form, setForm] = useState({ box: '', team: '', mode: 'reset' })
  const [boxes, setBoxes] = useState([])
  const [err, setErr] = useState(null)
  useEffect(() => {
    if (!open || !run) return
    setForm({ box: '', team: '', mode: 'reset', ...initial })
    setErr(null)
    api.comp(run.comp_id).then((c) => setBoxes(c.boxes || [])).catch(() => setBoxes([]))
  }, [open, run]) // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const submit = async (e) => {
    e.preventDefault()
    try {
      const r = await api.submitRun({
        comp_id: run.comp_id,
        action: 'redeploy',
        engine_ref: run.engine_ref,
        params: { box: form.box, team: form.team, mode: form.mode },
      })
      onSubmitted?.(r)
      onClose()
    } catch (e2) { setErr(e2) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Reset a box">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <Field label="Box" hint="From boxes.json">
          <input className={`${inputCls} font-mono`} list="reset-box-names" value={form.box} onChange={set('box')} required autoFocus placeholder="web01" />
          <datalist id="reset-box-names">{boxes.map((b) => <option key={b.name} value={b.name} />)}</datalist>
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Team" hint="e.g. 2 or team2,team3">
            <input className={`${inputCls} font-mono`} value={form.team} onChange={set('team')} placeholder="all" />
          </Field>
          <Field label="Mode">
            <select className={inputCls} value={form.mode} onChange={set('mode')}>
              {REDEPLOY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Field>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.box}><Icon name="play" />Submit redeploy</Button>
        </div>
      </form>
    </Modal>
  )
}

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
        : action === 'redeploy' ? ['Redeploy']
          : DEPLOY_STAGES

const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '')

// ── state pill ─────────────────────────────────────────────────────────────────────────────

// In-flight is the accent (with a pulse); a finished run is neutral, so a board of finished
// cards does not read as a wall of "go" colour next to the one thing still moving.
const STATE_CLS = {
  queued: 'bg-sunken text-muted',
  running: 'bg-accent-soft text-accent',
  succeeded: 'bg-sunken text-muted',
  failed: 'bg-danger-soft text-danger',
  cancelled: 'bg-sunken text-faint',
}

export function StatePill({ state }) {
  return (
    <span className={`chip px-2.5 py-1 ${STATE_CLS[state] || 'bg-sunken text-muted'}`}>
      {state === 'running' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
      {state === 'succeeded' && <Icon name="check" className="mr-1 h-3 w-3" />}
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

// Seconds spent per step: consecutive stage events bound each step, and the last runs to the
// run's end (or now). Keyed by index when the event carries one, else by normalized name —
// records written before the runner stamped index/total only have the name.
function stageDurations(stageEvents, endedAt) {
  const out = {}
  stageEvents.forEach((ev, i) => {
    const start = toMs(ev.ts)
    if (!start) return
    const next = stageEvents[i + 1]
    const end = next ? toMs(next.ts) : (endedAt ? toMs(endedAt) : Date.now())
    const d = (end - start) / 1000
    if (Number.isFinite(ev.index)) out[ev.index - 1] = d
    if (ev.stage) out[norm(ev.stage)] = d
  })
  return out
}

// Which step the run is on. The index the runner stamps on the stage event is authoritative;
// matching stage names is the fallback for older records. A started single-step action is in
// its one step by definition — without that a failed teardown drew an all-grey "no progress"
// bar, because a destroy's stage string never matched "Teardown" (2026-10-14).
function currentStep(action, stage, state, stageEvents) {
  const stages = stagesFor(action)
  const current = stage || stageEvents.at(-1)?.stage || ''
  const indexed = [...stageEvents].reverse().find((e) => Number.isFinite(e.index))
  const total = stages.length || indexed?.total || 0
  let idx = -1
  if (indexed) idx = Math.min(indexed.index, total) - 1
  else if (current) {
    idx = stages.findIndex((s) => norm(s) === norm(current))
    if (idx < 0) idx = stages.findIndex((s) => norm(current).includes(norm(s)) || norm(s).includes(norm(current)))
  }
  if (idx < 0 && total === 1 && state !== 'queued' && (indexed || current)) idx = 0
  return { stages, current, total, idx }
}

export function StageStrip({ action, stage, state, endedAt, stageEvents = [] }) {
  // Only a live run's elapsed moves; a finished strip is static, so a board of finished cards
  // must not re-render every second.
  useTick(state === 'running' || state === 'queued' ? 1000 : 60000)
  const [open, setOpen] = useState(false)
  const { stages, current, total, idx } = currentStep(action, stage, state, stageEvents)
  const durs = stageDurations(stageEvents, endedAt)
  const succeeded = state === 'succeeded'
  const failed = state === 'failed' || state === 'cancelled'
  // The runner records the last COMPLETED step, so a failure is the one after it (or the
  // first step when nothing completed).
  const failIdx = failed && total ? Math.min(idx + 1, total - 1) : -1
  const startMs = toMs(stageEvents[0]?.ts)
  const elapsed = startMs ? ((endedAt ? toMs(endedAt) : Date.now()) - startMs) / 1000 : null
  const dur = (i, s) => durs[i] ?? durs[norm(s)]
  const stepState = (i) => {
    if (failed) return i === failIdx ? 'fail' : i <= idx ? 'done' : 'todo'
    if (succeeded) return 'done'
    return i === idx ? 'cur' : idx >= 0 && i < idx ? 'done' : 'todo'
  }

  // A finished run collapses to its summary bar: the steps are history, and every finished
  // card on a board then has the same height.
  if ((succeeded || failed) && !open) {
    const label = failed
      ? `failed at ${stages[failIdx] || current || 'the last step'}`
      : 'complete'
    return (
      <button onClick={() => setOpen(true)} className="group w-full text-left" title="Show the steps">
        <div className={`h-2.5 rounded-full ${failed ? 'bg-danger' : 'bg-accent'}`} />
        <div className="mt-1.5 flex items-center gap-2 text-[11px]">
          <span className={`font-semibold tabular-nums ${failed ? 'text-danger' : 'text-muted'}`}>
            {succeeded ? total : Math.max(idx + 1, 0)}/{total}
          </span>
          <span className={`min-w-0 truncate ${failed ? 'text-danger' : 'text-muted'}`}>{label}</span>
          {elapsed != null && <span className="shrink-0 tabular-nums text-faint">{fmtDur(elapsed)}</span>}
          <Icon name="chevron" className="ml-auto h-3.5 w-3.5 shrink-0 rotate-90 text-faint transition group-hover:text-fg" />
        </div>
      </button>
    )
  }

  return (
    <div>
      {/* Eight stages don't fit one row of a half-width card — wrap to a 2x4 grid. */}
      <div className={stages.length > 4 ? 'grid grid-cols-4 gap-x-1 gap-y-2' : 'flex items-stretch gap-1'}>
        {stages.map((s, i) => {
          const st = stepState(i)
          const d = dur(i, s)
          return (
            <div key={s} className="min-w-0 flex-1" title={`${s}${d != null ? ` · ${fmtDur(d)}` : ''}`}>
              <div className={`h-2.5 rounded-full transition-colors ${
                st === 'fail' ? 'bg-danger'
                  : st === 'cur' ? 'animate-pulse bg-accent'
                    : st === 'done' ? 'bg-accent' : 'bg-line'}`} />
              <div className={`mt-1 truncate text-[9px] font-semibold uppercase tracking-wider ${
                st === 'fail' ? 'text-danger' : st === 'cur' ? 'text-accent' : st === 'done' ? 'text-muted' : 'text-faint'}`}>{s}</div>
              <div className="truncate text-[9px] tabular-nums text-faint">{d != null ? fmtDur(d) : ''}</div>
            </div>
          )
        })}
      </div>
      <div className="mt-2 flex items-center gap-2 text-xs text-muted">
        <span className={`h-2 w-2 shrink-0 rounded-full ${failed ? 'bg-danger' : succeeded ? 'bg-accent' : 'animate-pulse bg-accent'}`} />
        <span className="min-w-0 truncate">
          {failed ? `failed at ${stages[failIdx] || current || 'the last step'}`
            : succeeded ? `${total} step${total === 1 ? '' : 's'} complete`
              : current || 'waiting for the first stage'}
        </span>
        {elapsed != null && <span className="ml-auto shrink-0 tabular-nums text-faint">{fmtDur(elapsed)}</span>}
        {(succeeded || failed) && (
          <button onClick={() => setOpen(false)} className="shrink-0 rounded-full p-0.5 text-faint hover:text-fg"
            title="Collapse the steps" aria-label="Collapse the steps">
            <Icon name="chevron" className="h-3.5 w-3.5 -rotate-90" />
          </button>
        )}
      </div>
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
export const REDEPLOY_MODES = ['reset', 'rebuild', 'rollback-ready', 'rollback-base', 'reconfigure', 'resync', 'engine-recovery']

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
        // The runner maps these straight onto the driver's flags (boxes -> --boxes,
        // teams -> --teams); `box`/`team` are silently dropped, which turns a per-box
        // reset into a range-wide one.
        params: { boxes: form.box, teams: form.team, mode: form.mode },
      })
      onSubmitted?.(r)
      onClose()
    } catch (e2) { setErr(e2) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Reset a box">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <Field label="Box" hint="Comma-separated, from boxes.json — empty means every box of the selected teams">
          <input className={`${inputCls} font-mono`} list="reset-box-names" value={form.box} onChange={set('box')} autoFocus placeholder="web01" />
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
          <Button variant="primary" disabled={!form.box && !form.team}><Icon name="play" />Submit redeploy</Button>
        </div>
      </form>
    </Modal>
  )
}

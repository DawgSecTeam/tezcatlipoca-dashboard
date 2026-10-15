// Run-facing primitives shared by the Live and Runs pages and the deploy panel: state
// pills, the stage progress strip, offset-polled log views, the event list, the artifacts
// tree, and the two submit forms (new run / reset a box).
import { useEffect, useRef, useState } from 'react'
import { marked } from 'marked'
import { api, streamRunEvents } from '../api'
import { Button, ErrorBanner, Field, Modal, Spinner, inputCls } from './ui'
import { Icon, Stepper, fmtDur, toMs, useConfirm, useTick, useToast } from './kit'
import { LogView as LogViewImpl, prettyStage, stripEsc } from './logtex'

export { prettyStage } from './logtex'

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

// What the Stage column should say for a run record alone (no events). The runner stamps
// single-step actions with one stage of their own, but the store's `stage` string can carry
// the workspace's deploy phases — a 6-second Destroy then reads "phase 8/8 seed", which is
// nonsense. Single-step actions show their step only while in flight; deploy-family actions
// show the phase string prettified ("6/8 · Repair sweep").
const SINGLE_STEP = { verify: 'Verify', destroy: 'Teardown', redeploy: 'Redeploy' }

export function stageLabel(run) {
  const single = SINGLE_STEP[run?.action]
  if (single) return run?.state === 'running' || run?.state === 'queued' ? single : ''
  return prettyStage(run?.stage)
}

// ── state pill ─────────────────────────────────────────────────────────────────────────────

// In-flight is the accent (with a pulse); a finished run is neutral, so a board of finished
// cards does not read as a wall of "go" colour next to the one thing still moving.
const STATE_CLS = {
  queued: 'bg-sunken text-muted',
  running: 'bg-accent-soft text-accent',
  pausing: 'bg-sunken text-accent',
  paused: 'bg-accent-soft text-accent',
  succeeded: 'bg-sunken text-muted',
  failed: 'bg-danger-soft text-danger',
  cancelled: 'bg-sunken text-faint',
}

export function StatePill({ state }) {
  return (
    <span className={`chip px-2.5 py-1 ${STATE_CLS[state] || 'bg-sunken text-muted'}`}>
      {state === 'running' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
      {state === 'paused' && <Icon name="pause" className="mr-1 h-3 w-3" />}
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
  const pause = async () => {
    try {
      const r = await api.pauseRun(run.id)
      toast(r?.detail || 'Pause requested')
      onChanged()
    } catch (e) { toast(e.message, 'error') }
  }
  const resume = async () => {
    try { await api.resumeRun(run.id); toast('Resume requested'); onChanged() } catch (e) { toast(e.message, 'error') }
  }
  // A held run's process is alive, so Cancel stays offered; only a moving deploy can be asked
  // to hold at a boundary.
  const live = !!run && ['running', 'queued', 'pausing', 'paused'].includes(run.state)
  const held = !!run && (run.state === 'pausing' || run.state === 'paused')
  const canPause = !!run && run.action === 'deploy' && (run.state === 'running' || run.state === 'queued')
  return { submit, destroy, cancel, pause, resume, live, held, canPause }
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
      ? `failed at ${stages[failIdx] || prettyStage(current) || 'the last step'}`
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
          {failed ? `failed at ${stages[failIdx] || prettyStage(current) || 'the last step'}`
            : succeeded ? `${total} step${total === 1 ? '' : 's'} complete`
              : prettyStage(current) || 'waiting for the first stage'}
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

// The shared log view lives in logtex.jsx (ANSI parsing, repeat folding, search, exact phase
// jumps); the drawer and the run page's Output panel both render through it.
export function LogView({ log, height = '60vh', showPhases = false }) {
  return <LogViewImpl log={log} height={height} showPhases={showPhases} />
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
  return <pre ref={pre} style={{ height }} className="overflow-auto rounded-2xl bg-sunken p-3 font-mono text-[11px] leading-relaxed text-fg shadow-inset">{stripEsc(text) || ' '}</pre>
}

// ── live events: SSE with an offset-polling fallback ───────────────────────────────────────

export function useRunEvents(runId, onEvent, enabled = true) {
  const [updated, setUpdated] = useState(null) // ms timestamp of the last event or keepalive
  const [live, setLive] = useState(false) // an SSE stream is currently open
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
          setLive(true)
          // The server sends a keepalive comment every 15s; the stream can otherwise sit
          // silent for minutes between phase changes, and "updated" must not read as dead.
          await streamRunEvents(runId, take, { since: seq, signal: ctrl.signal, onKeepalive: () => setUpdated(Date.now()) })
        } catch { /* stream refused or dropped; fall back to polling */ }
        setLive(false)
        if (stop) return
        await poll()
        await new Promise((r) => { timer = setTimeout(r, 3000) })
      }
    }
    loop()
    return () => { stop = true; ctrl?.abort(); clearTimeout(timer) }
  }, [runId, enabled])
  return { updated, live }
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
  const [preview, setPreview] = useState(null) // {path, name}
  useEffect(() => {
    setRows(null); setErr(null)
    api.runArtifacts(runId)
      .then((d) => setRows(flattenArtifacts(d?.artifacts ?? d?.files ?? d?.items ?? d?.tree ?? d?.children ?? d)))
      .catch(setErr)
  }, [runId])
  if (err) return <ErrorBanner error={err} />
  if (!rows) return <Spinner />
  if (!rows.length) return <p className="text-xs text-faint">No artifacts collected yet.</p>
  // The API needs the bearer header, so a plain href cannot do it. A row click opens the
  // file in-app; the trailing icon downloads it without leaving the page.
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
    <>
      <ul className="space-y-1.5">
        {rows.map((r) => (
          <li key={r.path} className="group flex items-center gap-1">
            <button onClick={() => setPreview({ path: r.path })} title={`Preview ${r.path}`}
              className="flex min-w-0 flex-1 items-center gap-2 rounded-xl px-2 py-0.5 text-left text-xs transition hover:bg-sunken">
              <Icon name="packet" className="h-3.5 w-3.5 shrink-0 text-faint" />
              <span className="min-w-0 truncate font-mono text-muted">{r.path}</span>
              {(r.bytes ?? r.size) != null && <span className="shrink-0 tabular-nums text-faint">{r.bytes ?? r.size}</span>}
            </button>
            <button onClick={() => grab(r.path)} title={`Download ${r.path}`}
              className="shrink-0 rounded-lg px-1.5 py-0.5 text-[11px] text-faint opacity-0 transition hover:text-accent group-hover:opacity-100">
              ↓
            </button>
          </li>
        ))}
      </ul>
      <ArtifactPreview runId={runId} file={preview} onClose={() => setPreview(null)} onDownload={(p) => grab(p)} />
    </>
  )
}

// An artifact/evidence file opened in-app. Markdown renders, JSON pretty-prints, everything
// else shows as text — a 200-file evidence tree is useless if the report itself can't be
// read. Oversized files fall back to download; binary types go straight to download.
const TEXTY = /\.(md|json|jsonl|txt|log|env|py|ya?ml|conf|ini|toml)$/i

export function ArtifactPreview({ runId, file, onClose, onDownload }) {
  const [state, setState] = useState(null) // {kind: 'md'|'json'|'text', text} | {kind: 'binary'}
  const [err, setErr] = useState(null)
  const name = file?.path?.split('/').pop() || ''
  useEffect(() => {
    setState(null); setErr(null)
    if (!file) return
    ;(async () => {
      try {
        const blob = await api.runArtifactFile(runId, file.path)
        const pretty = file.path.endsWith('.json') || file.path.endsWith('.jsonc')
        if (!TEXTY.test(file.path) && !blob.type.startsWith('text/')) { setState({ kind: 'binary' }); return }
        const text = await blob.text()
        if (text.length > 2_000_000) { setState({ kind: 'binary' }); return } // preview limit; download still works
        setState({ kind: pretty ? 'json' : file.path.endsWith('.md') ? 'md' : 'text', text })
      } catch (e) { setErr(e) }
    })()
  }, [file, runId]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Modal wide open={!!file} onClose={onClose} title={name}>
      <ErrorBanner error={err} />
      {!file ? null : !state ? <div className="flex justify-center p-8"><Spinner /></div> : (
        <>
          {state.kind === 'md' && (
            <div className="prose prose-sm max-h-[60vh] max-w-none overflow-auto rounded-2xl bg-sunken p-5 text-fg shadow-inset dark:prose-invert prose-headings:font-[family-name:var(--font-display)] prose-headings:font-semibold prose-a:text-accent"
              dangerouslySetInnerHTML={{ __html: marked.parse(state.text) }} />
          )}
          {state.kind === 'json' && (
            <pre className="max-h-[60vh] overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">
              {(() => { try { return JSON.stringify(JSON.parse(state.text), null, 2) } catch { return state.text } })()}
            </pre>
          )}
          {state.kind === 'text' && (
            <pre className="max-h-[60vh] overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">{state.text}</pre>
          )}
          {state.kind === 'binary' && (
            <p className="text-sm text-muted">This file is too large or not text to preview here{onDownload && <> — use the download button below</>}.</p>
          )}
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className="truncate font-mono text-[11px] text-faint" title={file.path}>{file.path}</span>
            {onDownload && <Button onClick={() => onDownload(file.path)}><Icon name="packet" />Download</Button>}
          </div>
        </>
      )}
    </Modal>
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
        <Field label="Action" hint="Scrim, redeploy and teardown options (teams to target, per-box modes) live on the competition and run pages.">
          <select className={inputCls} value={form.action} onChange={set('action')}>
            {RUN_ACTIONS.map((a) => <option key={a} value={a}>{ACTION_LABEL[a]}</option>)}
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

// "Reset a box…" — redeploy action with box/team/mode fields. Box and team are pickers, not
// free text: the driver's --boxes/--teams take comma lists ("2" or "team2,team3"), and a
// typo used to silently widen a per-box reset to every team on the range.
export const REDEPLOY_MODES = ['reset', 'rebuild', 'rollback-ready', 'rollback-base', 'reconfigure', 'resync', 'engine-recovery']

const MODE_HINT = {
  reset: 'Re-provision the box in place — config and misconfigs are re-applied.',
  rebuild: 'Delete the VM and build it fresh from its template.',
  'rollback-ready': 'Roll back to the last known-good (ready) snapshot.',
  'rollback-base': 'Roll back to the pristine base snapshot.',
  reconfigure: 'Re-run configuration management without touching the VM itself.',
  resync: 'Re-sync the box state to what the range expects, no rebuild.',
  'engine-recovery': 'Recover the scoring engine VM, not a team box.',
}

// One team chip per deployed team; with no inventory, plain numbers from the run's params.
function teamChoices(run, inventoryTeams) {
  if (inventoryTeams?.length) return inventoryTeams.map((t) => ({ value: t.identifier || t.key, label: t.key }))
  const n = Number(run?.params?.teams) || 0
  return n > 0 ? Array.from({ length: n }, (_, i) => ({ value: String(i + 1), label: `team ${i + 1}` })) : []
}

export function ResetBoxModal({ open, onClose, run, initial = {}, teams, onSubmitted }) {
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
  const toggle = (k, v) => {
    const cur = form[k] ? form[k].split(',').filter(Boolean) : []
    const next = cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]
    setForm({ ...form, [k]: next.join(',') })
  }
  const picked = (k) => (form[k] ? form[k].split(',').filter(Boolean) : [])
  const submit = async (e) => {
    e.preventDefault()
    try {
      const r = await api.submitRun({
        comp_id: run.comp_id,
        action: 'redeploy',
        engine_ref: run.engine_ref,
        params: { boxes: form.box, teams: form.team, mode: form.mode },
      })
      onSubmitted?.(r)
      onClose()
    } catch (e2) { setErr(e2) }
  }
  const boxNames = boxes.map((b) => b.name)
  const teamOpts = teamChoices(run, teams)
  const Chip = ({ k, v, label }) => (
    <button type="button" onClick={() => toggle(k, v)}
      className={`chip px-2.5 py-1 font-mono text-xs transition ${picked(k).includes(v) ? 'bg-accent-soft text-accent' : 'bg-sunken text-muted hover:text-fg'}`}>
      {label || v}
    </button>
  )
  return (
    <Modal open={open} onClose={onClose} title="Reset a box">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <Field label="Boxes" hint={picked('box').length ? `--boxes ${form.box}` : 'None picked — every box of the selected teams is reset.'}>
          <div className="flex flex-wrap gap-2 rounded-2xl bg-sunken p-3 shadow-inset">
            {boxNames.map((n) => <Chip key={n} k="box" v={n} />)}
            {!boxNames.length && <span className="text-xs text-faint">The competition defines no boxes.</span>}
          </div>
        </Field>
        <Field label="Teams" hint={picked('team').length ? `--teams ${form.team}` : 'None picked — the redeploy covers the whole range.'}>
          <div className="flex flex-wrap gap-2 rounded-2xl bg-sunken p-3 shadow-inset">
            {teamOpts.map((t) => <Chip key={t.value} k="team" v={t.value} label={t.label} />)}
            {!teamOpts.length && (
              <input className={`${inputCls} font-mono`} value={form.team} onChange={set('team')} placeholder="e.g. 2 or team2,team3" />
            )}
          </div>
        </Field>
        <Field label="Mode" hint={MODE_HINT[form.mode]}>
          <select className={inputCls} value={form.mode} onChange={set('mode')}>
            {REDEPLOY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.box && !form.team}><Icon name="play" />Submit redeploy</Button>
        </div>
      </form>
    </Modal>
  )
}

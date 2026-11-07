// Overview's deploy panel: same form as the old job-based panel, but every action
// submits a run to POST /api/runs and watches it through the run log API.
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, Field, Modal, inputCls } from './ui'
import { Icon, Stepper, agoMs, fmtMem, useToast } from './kit'
import { ACTION_LABEL, RunLogModal, StatePill, useRunLog } from './runui'

function Footprint({ comp, teams }) {
  const n = Number(teams) || 0
  const managed = comp.boxes
  const sum = (k) => managed.reduce((a, b) => a + (Number(b[k]) || 0), 0)
  const rows = [
    ['VMs', `${n * managed.length}`, `${n} × ${managed.length} boxes, + engine`],
    ['RAM', fmtMem(n * sum('memory_mb')), 'team boxes only'],
    ['vCPU', `${n * sum('cpu')}`, ''],
  ]
  return (
    <div className="grid grid-cols-3 gap-2 rounded-2xl bg-sunken p-3 shadow-inset">
      {rows.map(([k, v, hint]) => (
        <div key={k} title={hint} className="text-center">
          <div className="text-lg font-semibold tabular-nums">{v}</div>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">{k}</div>
        </div>
      ))}
    </div>
  )
}

// The engine derives every team box's vmid as `200 + identifier*10 + boxIndex`. A vmid the
// operator picks "because it's free" can therefore collide with a box THIS deploy creates
// (identifier 121 puts box 0 on 1410), and preflight only says so after a 13-minute build.
// These two helpers make the arithmetic visible and refuse the submit up front
// (2026-11-06 audit). Exported for tests.
export function derivedBoxVmids(identifiers, teams, boxCount) {
  const ids = String(identifiers || '').split(',').map((s) => s.trim()).filter(Boolean).map(Number)
  const n = Number(teams) || 0
  return ids.filter(Number.isFinite).slice(0, n).map((id) => ({
    identifier: id,
    vmids: Array.from({ length: Math.max(0, Number(boxCount) || 0) }, (_, i) => 200 + id * 10 + i),
  }))
}

// The collision itself, or null: `{vmid, identifier, boxIndex}` naming the box the Engine
// VM ID would overwrite. An empty vmid is "auto" — nothing to check.
export function engineVmidConflict(identifiers, teams, boxCount, vmid) {
  const raw = String(vmid || '').trim()
  if (!raw || !/^\d+$/.test(raw)) return null
  const v = Number(raw)
  for (const team of derivedBoxVmids(identifiers, teams, boxCount)) {
    const boxIndex = team.vmids.indexOf(v)
    if (boxIndex >= 0) return { vmid: v, identifier: team.identifier, boxIndex }
  }
  return null
}

export default function DeployPanel({ comp }) {
  const [nodes, setNodes] = useState(null)
  const [form, setForm] = useState({ engine_ref: '', teams: 2, scoring_vmid: '', engine_node: '', team_node: '', nodes: [], start_after_deploy: false, auto_reclaim: false, team_identifiers: '', engine_mgmt_ip: '' })
  const [runs, setRuns] = useState([])
  const [viewing, setViewing] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState([])
  const [err, setErr] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const toast = useToast()
  const busy = useRef(false)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const viewingLog = useRunLog(viewing?.id)
  const prevRunning = useRef(null)
  const catalog = nodes?.nodes || []
  const selected = catalog.filter((n) => form.nodes.includes(n.name))

  const refreshRuns = () => api.runs().then((rs) => setRuns(rs.filter((r) => r.comp_id === comp.id))).catch(() => {})
  useEffect(() => {
    api.nodes().then((c) => {
      setNodes(c)
      setForm((f) => ({ ...f, nodes: (c.nodes || []).map((n) => n.name) }))
    }).catch(() => {})
    refreshRuns()
    // Another operator's deploy can start while this panel is open: without a poll the
    // collision warning below stayed silent even though the engine lock was already held.
    const timer = setInterval(refreshRuns, 15000)
    return () => clearInterval(timer)
  }, [comp.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggleNode = (name) => setForm((f) => ({
    ...f, nodes: f.nodes.includes(name) ? f.nodes.filter((x) => x !== name) : [...f.nodes, name],
    engine_node: f.engine_node === name && f.nodes.includes(name) ? '' : f.engine_node,
  }))
  const setRow = (i, key) => (e) => setDraft((rows) => rows.map((r, j) => (j === i ? { ...r, [key]: e.target.value } : r)))
  const saveCatalog = async () => {
    setErr(null)
    try {
      const saved = await api.putNodes({ nodes: draft, balancing: nodes?.balancing || {} })
      setNodes(saved)
      setForm((f) => ({ ...f, nodes: saved.nodes.map((n) => n.name) }))
      setEditing(false)
    } catch (e) { setErr(e) }
  }

  // Toast when a run we are watching comes to rest.
  useEffect(() => {
    const wasLive = viewing && (viewing.state === 'running' || viewing.state === 'queued')
    if (wasLive && prevRunning.current === true && !viewingLog.running) {
      toast(`${ACTION_LABEL[viewing.action] || viewing.action} ${viewingLog.returncode === 0 ? 'finished' : `failed (exit ${viewingLog.returncode})`}`,
        viewingLog.returncode === 0 ? 'ok' : 'error')
      refreshRuns()
    }
    prevRunning.current = viewingLog.running
  }, [viewingLog.running, viewing]) // eslint-disable-line react-hooks/exhaustive-deps

  const noBoxes = comp.boxes.length === 0
  // The derived box vmids, so the arithmetic is visible, and the collision the panel used
  // to leave to a 13-minute preflight (see engineVmidConflict above).
  const boxVmids = derivedBoxVmids(form.team_identifiers, form.teams, comp.boxes.length)
  const vmidConflict = engineVmidConflict(form.team_identifiers, form.teams, comp.boxes.length, form.scoring_vmid)
  const conflictCopy = vmidConflict
    ? `VMID ${vmidConflict.vmid} collides with this deploy's own boxes: team ${vmidConflict.identifier} `
      + `box ${vmidConflict.boxIndex + 1}${comp.boxes[vmidConflict.boxIndex]?.name ? ` (${comp.boxes[vmidConflict.boxIndex].name})` : ''} `
      + `lands on ${vmidConflict.vmid} (200 + ${vmidConflict.identifier}×10 + ${vmidConflict.boxIndex}). `
      + 'Choose a different Engine VM ID or team identifiers.'
    : null
  const submit = async (action) => {
    // The confirm dialog closes before the request lands; a second click used to submit a
    // second deploy/plan for the same form.
    if (busy.current) return
    // The up-front guard behind the disabled buttons: a self-colliding vmid is arithmetic,
    // not a race — never spend a deploy discovering it.
    if (action === 'deploy' && vmidConflict) return
    busy.current = true; setSubmitting(true); setErr(null)
    try {
      const run = await api.submitRun({
        comp_id: comp.id,
        action,
        engine_ref: form.engine_ref,
        teams: Number(form.teams) || 0,
        scoring_vmid: form.scoring_vmid ? Number(form.scoring_vmid) : null,
        engine_node: form.engine_node,
        team_node: form.team_node,
        nodes: form.nodes,
        // The per-run estate gates: both are submit params the preflight reads, and without them
        // a second range on a shared node cannot be submitted at all (2026-11-06 audit).
        team_identifiers: form.team_identifiers.trim() || null,
        engine_mgmt_ip: form.engine_mgmt_ip.trim() || null,
        // deploy and start are separate steps; the checkbox buys the one-shot back.
        // auto_reclaim (the other checkbox): on failure the runner submits the destroy that
        // reclaims the engine VM + bridge this deploy leaves behind — the leftovers that
        // otherwise collide with every retry at preflight (2026-11-06: run-90f48b3a poisoned
        // the estate twice until a destroy ran).
        ...(form.start_after_deploy || form.auto_reclaim
          ? {
            params: {
              ...(form.start_after_deploy ? { start_after_deploy: true } : {}),
              ...(form.auto_reclaim ? { auto_reclaim: true } : {}),
            },
          }
          : {}),
      })
      // A deploy that closed with no toast, no link and no redirect left the operator
      // staring at an unchanged panel (2026-11-06 audit). The range-page actions already
      // toast `Operation run-… submitted`; match that pattern and make the run one click
      // away — /runs?run=<id> opens the run drawer directly.
      toast(<>
        {ACTION_LABEL[action] || action} submitted ·{' '}
        <Link className="underline" to={`/runs?run=${encodeURIComponent(run.id)}`}>{run.id}</Link>
      </>)
      setViewing(run)
      refreshRuns()
    } catch (e) { setErr(e) }
    finally { busy.current = false; setSubmitting(false) }
  }

  return (
    <Card className="rise rise-2 p-6">
      <div className="mb-5 flex items-center justify-between">
        <h2 className="display text-2xl">Deploy</h2>
        {comp.deployed && <span className="chip bg-accent-soft px-2.5 py-1 text-accent" title="An estate state document exists for this competition"><span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-accent" />has a deploy</span>}
      </div>
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="space-y-5">
        {/* The engine preflight is the real collision authority, but a second deploy while
            one is running deserves a visible heads-up before the operator burns a build. */}
        {(() => {
          // A held deploy is still in flight and still owns the workspace/engine lock; only
          // counting running/queued made the collision warning vanish exactly when the
          // operator parked a build.
          const busy = runs.find((r) => ['running', 'queued', 'pausing', 'paused'].includes(r.state))
          const held = busy && (busy.state === 'pausing' || busy.state === 'paused')
          return busy ? (
            <div className="rounded-2xl bg-accent-soft px-4 py-2.5 text-xs text-accent">
              <span className="font-semibold">{ACTION_LABEL[busy.action] || busy.action}</span> is {held ? 'held' : 'running now'}
              ({busy.id}, {agoMs(busy.started_at || busy.created_at)}) —
              a second concurrent run may collide on the engine.{' '}
              <Link to={`/c/${comp.id}/runs/${busy.id}`} className="underline">Open it</Link>.
            </div>
          ) : null
        })()}
        <Field label="Engine ref" hint="The tezcatlipoca ref the run pins (branch, SHA or tag). Empty uses the service's own pin — main, the integration branch.">
          <input className={`${inputCls} font-mono`} value={form.engine_ref} onChange={set('engine_ref')} placeholder="main" />
        </Field>
        <Field label="Teams"><Stepper value={form.teams} onChange={(v) => setForm({ ...form, teams: v })} /></Field>
        <Field label="Competition start" hint="Off (default): the deploy leaves the engine paused and the clock unstarted — start it from the run page when the range is ready. On: the old one-shot deploy that starts scoring immediately.">
          <label className="flex items-center gap-3 rounded-xl bg-sunken px-3 py-2 text-sm shadow-inset">
            <input type="checkbox" checked={form.start_after_deploy}
              onChange={(e) => setForm({ ...form, start_after_deploy: e.target.checked })} />
            Start the competition right after deploying
          </label>
        </Field>
        {/* The failed-deploy trap (2026-11-06): a deploy that dies mid-build keeps its
            engine VM and bridge, and every later deploy then collides on them. On, the
            runner submits the reclaiming destroy automatically when this deploy fails;
            off (default) leaves the half-built range for inspection, as before. */}
        <Field label="On failure" hint="Off (default): a failed deploy keeps its VMs and bridge for inspection — destroy that range before retrying, or its leftovers will collide with the retry's preflight. On: the dashboard submits the reclaiming destroy for you the moment the deploy fails.">
          <label className="flex items-center gap-3 rounded-xl bg-sunken px-3 py-2 text-sm shadow-inset">
            <input type="checkbox" checked={form.auto_reclaim}
              onChange={(e) => setForm({ ...form, auto_reclaim: e.target.checked })} />
            Reclaim leftover VMs automatically if this deploy fails
          </label>
        </Field>
        <Footprint comp={comp} teams={form.teams} />
        <Field label="Proxmox nodes"
          hint={selected.length ? 'The pinned engine node hosts the engine; teams fill the selected nodes.' : 'Nothing selected — the run falls back to the service .env (single node).'}>
          <div className="space-y-2">
            {catalog.length === 0 && <p className="text-xs text-faint">The node catalog is empty — add the hosts this dashboard may deploy to.</p>}
            {catalog.map((n) => (
              <label key={n.name} className="flex items-center gap-3 rounded-xl bg-sunken px-3 py-2 shadow-inset">
                <input type="checkbox" checked={form.nodes.includes(n.name)} onChange={() => toggleNode(n.name)} />
                <span className="font-mono text-sm">{n.name}</span>
                {!n.has_token && <span className="chip bg-danger-soft px-2 py-0.5 text-danger">no token</span>}
                <span className="ml-auto font-mono text-[11px] text-faint">{n.node}/{n.datastore}</span>
              </label>
            ))}
            <Button type="button" onClick={() => { setDraft(catalog.map((n) => ({ ...n, token: '' }))); setEditing(true) }}>
              <Icon name="net" />Nodes…
            </Button>
            {selected.length > 0 && (
              <div className="space-y-2">
                <select className={inputCls} value={form.engine_node} onChange={set('engine_node')}>
                  <option value="">Engine: auto (node with most teams)</option>
                  {selected.map((n) => <option key={n.name} value={n.name}>Engine on {n.name}</option>)}
                </select>
                <input className={`${inputCls} font-mono`} placeholder="Team pins, e.g. 130=node-a" value={form.team_node} onChange={set('team_node')} />
              </div>
            )}
          </div>
        </Field>
        <Field label="Engine VM ID" hint={boxVmids.length
          // Show the derived box vmids: the collision below is invisible arithmetic otherwise.
          ? `Box vmids are derived from the identifiers: ${boxVmids.map((t) => `team ${t.identifier} → ${t.vmids[0]}–${t.vmids[t.vmids.length - 1]}`).join(', ')}. The engine must not be one of them.`
          : 'A free Proxmox VMID for the scoring engine. Preflight refuses collisions.'}>
          <input inputMode="numeric" className={`${inputCls} font-mono`} value={form.scoring_vmid} onChange={(e) => setForm({ ...form, scoring_vmid: e.target.value.replace(/\D/g, '') })} placeholder="auto" />
          {conflictCopy && (
            <p className="mt-1 text-xs font-semibold text-danger" role="alert">{conflictCopy}</p>
          )}
        </Field>
        {/* The two per-run estate gates. Both are submit params the API has always accepted, but
            the panel never exposed them, so a second range on a shared node could not be submitted
            in a form the preflight accepts (2026-11-06 audit). */}
        <Field label="Team identifiers" hint="Comma list, e.g. 120,121. Drives BOTH the box vmids (200 + id*10 + box) and the team subnet (192.168.<id>.0/24) — it must match what the competition's DNAT rules assume, not merely be free. Empty uses TEZ_TEAM_IDENTIFIERS.">
          <input className={`${inputCls} font-mono`} value={form.team_identifiers}
            onChange={(e) => setForm({ ...form, team_identifiers: e.target.value.replace(/[^\d,]/g, '') })}
            placeholder="120,121" />
        </Field>
        <Field label="Engine management IP" hint="The engine's mgmt address on the flat 10.0.0.0/24. Contested cluster-wide, so a deploy must pin one you have checked yourself — the preflight refuses the unverifiable default. Empty uses the node catalog's value.">
          <input className={`${inputCls} font-mono`} value={form.engine_mgmt_ip}
            onChange={(e) => setForm({ ...form, engine_mgmt_ip: e.target.value.replace(/[^\d.]/g, '') })}
            placeholder="10.0.0.252" />
        </Field>
        <div className="space-y-3 pt-1">
          {/* A self-colliding engine vmid is arithmetic, not a preflight secret: the
              Deploy path stays shut while the collision stands (the Dry run touches no
              Proxmox, so it stays available for definition debugging). */}
          <Button variant="primary" className="w-full !py-2.5" disabled={noBoxes || submitting || !!vmidConflict} onClick={() => setConfirm(true)}>
            <Icon name="rocket" />{submitting ? 'Submitting…' : `Deploy ${form.teams || 0} team${Number(form.teams) === 1 ? '' : 's'}`}
          </Button>
          <Button disabled={noBoxes || submitting} onClick={() => submit('plan')} title="create-competition.py --plan-only: renders the plan and touches nothing. It does NOT contact the node, so it proves the definition is buildable, not that the estate can host it.">
            <Icon name="play" />Dry run (plan only)
          </Button>
          <p className="text-center text-[11px] leading-relaxed text-faint">
            A dry run renders the plan from the competition definition. It never contacts Proxmox,
            so a green dry run does not mean the estate can build it.
          </p>
          {/* Verify is a RANGE action, and this competition may have several ranges: the
              submit API would pick one of them implicitly. It lives on the range page. */}
          {noBoxes && <p className="text-center text-xs text-faint">Add a box before deploying.</p>}
        </div>
      </div>

      <Modal open={confirm} onClose={() => setConfirm(false)} title={`Deploy ${comp.name}?`}>
        <p className="text-sm leading-relaxed text-muted">
          This builds real VMs on Proxmox: <b className="text-fg">{form.teams} team{Number(form.teams) === 1 ? '' : 's'}</b> × {comp.boxes.length} boxes, plus the scoring engine
          {form.scoring_vmid && <> (VMID <span className="font-mono">{form.scoring_vmid}</span>)</>}.
        </p>
        <div className="mt-4"><Footprint comp={comp} teams={form.teams} /></div>
        <code className="mt-4 block rounded-xl bg-sunken p-3 font-mono text-[11px] text-muted shadow-inset">
          create-competition.py --competition {comp.id} --teams {form.teams} --yes{form.scoring_vmid && ` --scoring-vmid ${form.scoring_vmid}`}{form.team_identifiers.trim() && ` --team-identifiers ${form.team_identifiers.trim()}`}{form.engine_mgmt_ip.trim() && ` --engine-mgmt-ip ${form.engine_mgmt_ip.trim()}`}
        </code>
        <div className="mt-6 flex justify-end gap-3">
          <Button onClick={() => setConfirm(false)}>Cancel</Button>
          <Button variant="primary" autoFocus disabled={submitting || !!vmidConflict} onClick={() => { setConfirm(false); submit('deploy') }}><Icon name="rocket" />Deploy</Button>
        </div>
      </Modal>
      <Modal open={editing} onClose={() => setEditing(false)} title="Proxmox nodes" wide>
        <p className="mb-3 text-xs leading-relaxed text-faint">
          Held by the dashboard (0600), never in the engine env. Tokens are write-only: leave blank
          to keep the stored one. A run hands its selected nodes to the engine as the node list.
        </p>
        <div className="space-y-3">
          {draft.map((n, i) => (
            <div key={i} className="space-y-2 rounded-xl bg-sunken p-3 shadow-inset">
              <div className="grid grid-cols-4 gap-2">
                <input className={`${inputCls} font-mono`} placeholder="name" value={n.name || ''} onChange={setRow(i, 'name')} />
                <input className={`${inputCls} font-mono`} placeholder="https://host:8006" value={n.endpoint || ''} onChange={setRow(i, 'endpoint')} />
                <input className={`${inputCls} font-mono`} placeholder="pve node" value={n.node || ''} onChange={setRow(i, 'node')} />
                <input className={`${inputCls} font-mono`} placeholder="datastore" value={n.datastore || ''} onChange={setRow(i, 'datastore')} />
              </div>
              <div className="grid grid-cols-4 gap-2">
                <input className={`${inputCls} font-mono`} placeholder="engine base vmid" value={n.engine_base_vmid || ''} onChange={setRow(i, 'engine_base_vmid')} />
                <input className={`${inputCls} font-mono`} placeholder="engine mgmt IP" value={n.engine_mgmt_ip || ''} onChange={setRow(i, 'engine_mgmt_ip')} />
                <input type="password" className={`${inputCls} font-mono`} placeholder={n.has_token ? 'token (kept)' : 'API token'} value={n.token || ''} onChange={setRow(i, 'token')} />
                <Button type="button" onClick={() => setDraft((rows) => rows.filter((_, j) => j !== i))}>Remove</Button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <Button type="button" onClick={() => setDraft((rows) => [...rows, { name: '', endpoint: '', node: '', datastore: '', engine_base_vmid: '', engine_mgmt_ip: '' }])}>Add node</Button>
          <div className="flex gap-3">
            <Button type="button" onClick={() => setEditing(false)}>Cancel</Button>
            <Button type="button" variant="primary" onClick={saveCatalog}>Save</Button>
          </div>
        </div>
      </Modal>
      <RunLogModal open={!!viewing} onClose={() => setViewing(null)} log={viewingLog}
        title={viewing ? `${ACTION_LABEL[viewing.action] || viewing.action} · ${comp.name}` : ''} />
    </Card>
  )
}

// The competition's recent runs, as a card in the Overview's left column — the deploy panel
// was the only thing tall enough to fill its column, and a bare half-page next to it read as
// an unfinished layout. Rows navigate to the run page, so the drawer log modal stays here.
export function RecentRuns({ compId }) {
  const [runs, setRuns] = useState([])
  useEffect(() => { api.runs().then((rs) => setRuns(rs.filter((r) => r.comp_id === compId))).catch(() => {}) }, [compId])
  return (
    <Card className="rise rise-3 p-6">
      <h3 className="label">Recent runs</h3>
      {runs.length === 0
        ? <p className="text-xs text-faint">No runs for this competition yet.</p>
        : (
          <ul className="space-y-2">
            {runs.slice(0, 6).map((r) => (
              <li key={r.id}>
                <Link to={`/c/${compId}/runs/${r.id}`}
                  className="tile tile-hover flex w-full items-center gap-3 px-4 py-2 text-sm">
                  <span className={`h-2 w-2 shrink-0 rounded-full ${r.state === 'running' || r.state === 'queued' ? 'animate-pulse bg-accent' : r.state === 'succeeded' ? 'bg-accent' : r.state === 'failed' ? 'bg-danger' : 'bg-line'}`} />
                  <span className="font-medium">{ACTION_LABEL[r.action] || r.action}</span>
                  <StatePill state={r.state} />
                  <span className="ml-auto text-xs text-faint">{agoMs(r.started_at || r.created_at)}</span>
                  <Icon name="chevron" className="h-3.5 w-3.5 shrink-0 text-faint" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      <p className="mt-4 text-[11px] leading-relaxed text-faint">
        Runs execute in the dashboard service's own workspace. Tear down with a Destroy run.
      </p>
    </Card>
  )
}


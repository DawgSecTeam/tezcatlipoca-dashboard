// Overview's deploy panel: same form as the old job-based panel, but every action
// submits a run to POST /api/runs and watches it through the run log API.
import { useEffect, useRef, useState } from 'react'
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

export default function DeployPanel({ comp }) {
  const [nodes, setNodes] = useState(null)
  const [form, setForm] = useState({ engine_ref: 'main', teams: 2, scoring_vmid: '', engine_node: '', team_node: '' })
  const [runs, setRuns] = useState([])
  const [viewing, setViewing] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const [err, setErr] = useState(null)
  const toast = useToast()
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const viewingLog = useRunLog(viewing?.id)
  const prevRunning = useRef(null)

  const refreshRuns = () => api.runs().then((rs) => setRuns(rs.filter((r) => r.comp_id === comp.id))).catch(() => {})
  useEffect(() => { api.nodes().then(setNodes).catch(() => {}); refreshRuns() }, [comp.id]) // eslint-disable-line react-hooks/exhaustive-deps

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
  const submit = async (action) => {
    setErr(null)
    try {
      const run = await api.submitRun({
        comp_id: comp.id,
        action,
        engine_ref: form.engine_ref || 'main',
        teams: Number(form.teams) || 0,
        scoring_vmid: form.scoring_vmid ? Number(form.scoring_vmid) : null,
        engine_node: form.engine_node,
        team_node: form.team_node,
      })
      setViewing(run)
      refreshRuns()
    } catch (e) { setErr(e) }
  }

  return (
    <Card className="rise rise-2 p-6">
      <div className="mb-5 flex items-center justify-between">
        <h2 className="display text-2xl">Deploy</h2>
        {comp.deployed && <span className="chip bg-accent-soft px-2.5 py-1 text-accent" title=".deploy_state.json exists for this competition"><span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-accent" />has a deploy</span>}
      </div>
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="space-y-5">
        <Field label="Engine ref" hint="The tezcatlipoca ref the run pins (branch, SHA or tag)">
          <input className={`${inputCls} font-mono`} value={form.engine_ref} onChange={set('engine_ref')} placeholder="main" />
        </Field>
        <Field label="Teams"><Stepper value={form.teams} onChange={(v) => setForm({ ...form, teams: v })} /></Field>
        <Footprint comp={comp} teams={form.teams} />
        <Field label="Proxmox nodes" hint={nodes?.multi ? 'Unpinned teams are placed by capacity-fill (nodes.json).' : 'Single node from .env. Add nodes.json to spread teams across hosts.'}>
          {nodes?.multi ? (
            <div className="space-y-2">
              <select className={inputCls} value={form.engine_node} onChange={set('engine_node')}>
                <option value="">Engine: auto (node with most teams)</option>
                {nodes.nodes.map((n) => <option key={n.name} value={n.name}>Engine on {n.name}</option>)}
              </select>
              <input className={`${inputCls} font-mono`} placeholder="Team pins, e.g. 101=zfs-193,102=hdd-150" value={form.team_node} onChange={set('team_node')} />
            </div>
          ) : (
            <div className="field flex items-center gap-2 font-mono text-xs text-muted"><Icon name="net" className="h-3.5 w-3.5" />{nodes?.nodes?.[0]?.name || '…'}</div>
          )}
        </Field>
        <Field label="Engine VM ID" hint="A free Proxmox VMID for the scoring engine. Preflight refuses collisions.">
          <input inputMode="numeric" className={`${inputCls} font-mono`} value={form.scoring_vmid} onChange={(e) => setForm({ ...form, scoring_vmid: e.target.value.replace(/\D/g, '') })} placeholder="auto" />
        </Field>
        <div className="space-y-3 pt-1">
          <Button variant="primary" className="w-full !py-2.5" disabled={noBoxes} onClick={() => setConfirm(true)}>
            <Icon name="rocket" />Deploy {form.teams || 0} team{Number(form.teams) === 1 ? '' : 's'}
          </Button>
          <div className="grid grid-cols-2 gap-3">
            <Button disabled={noBoxes} onClick={() => submit('plan')} title="create-competition.py --plan-only: prints the plan, touches nothing">
              <Icon name="play" />Dry run
            </Button>
            <Button onClick={() => submit('verify')} title="verify-competition.py against the running range">
              <Icon name="check" />Verify
            </Button>
          </div>
          {noBoxes && <p className="text-center text-xs text-faint">Add a box before deploying.</p>}
        </div>
      </div>

      <div className="mt-6">
        <h3 className="label">Recent runs</h3>
        {runs.length === 0
          ? <p className="text-xs text-faint">No runs for this competition yet.</p>
          : (
            <ul className="space-y-2">
              {runs.slice(0, 6).map((r) => (
                <li key={r.id}>
                  <button onClick={() => setViewing(r)} className="tile tile-hover flex w-full items-center gap-3 px-4 py-2 text-left text-sm">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${r.state === 'running' || r.state === 'queued' ? 'animate-pulse bg-accent' : r.state === 'succeeded' ? 'bg-accent' : r.state === 'failed' ? 'bg-danger' : 'bg-line'}`} />
                    <span className="font-medium">{ACTION_LABEL[r.action] || r.action}</span>
                    <StatePill state={r.state} />
                    <span className="ml-auto text-xs text-faint">{agoMs(r.started_at || r.created_at)}</span>
                    <Icon name="terminal" className="h-3.5 w-3.5 text-faint" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        <p className="mt-4 text-[11px] leading-relaxed text-faint">
          Runs execute in the dashboard service's own workspace. Tear down with a Destroy run.
        </p>
      </div>

      <Modal open={confirm} onClose={() => setConfirm(false)} title={`Deploy ${comp.name}?`}>
        <p className="text-sm leading-relaxed text-muted">
          This builds real VMs on Proxmox: <b className="text-fg">{form.teams} team{Number(form.teams) === 1 ? '' : 's'}</b> × {comp.boxes.length} boxes, plus the scoring engine
          {form.scoring_vmid && <> (VMID <span className="font-mono">{form.scoring_vmid}</span>)</>}.
        </p>
        <div className="mt-4"><Footprint comp={comp} teams={form.teams} /></div>
        <code className="mt-4 block rounded-xl bg-sunken p-3 font-mono text-[11px] text-muted shadow-inset">
          create-competition.py --competition {comp.id} --teams {form.teams} --yes{form.scoring_vmid && ` --scoring-vmid ${form.scoring_vmid}`}
        </code>
        <div className="mt-6 flex justify-end gap-3">
          <Button onClick={() => setConfirm(false)}>Cancel</Button>
          <Button variant="primary" autoFocus onClick={() => { setConfirm(false); submit('deploy') }}><Icon name="rocket" />Deploy</Button>
        </div>
      </Modal>
      <RunLogModal open={!!viewing} onClose={() => setViewing(null)} log={viewingLog}
        title={viewing ? `${ACTION_LABEL[viewing.action] || viewing.action} · ${comp.name}` : ''} />
    </Card>
  )
}

// Teams tab of the run page: the range's teams and their deployed boxes, the reset
// requests the teams filed through the portal, plus the machines that live outside the
// teams. Props: run (run record), comp (competition detail),
// inventory (GET /api/runs/{id}/inventory payload, or null while loading).
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, kindOf } from '../api'
import { Button, Card, PlatformBadge, inputCls } from './ui'
import { EmptyState, Icon, fmtMem, useConfirm, useToast } from './kit'
import { REDEPLOY_MODES, ResetBoxModal } from './runui'

const specMap = (comp) => new Map((comp?.boxes || []).map((b) => [b.name, b]))

// `started` is the only liveness fact the inventory carries. Absent means unknown, never
// "stopped": a status invented here would be read as an observation.
const Status = ({ started }) => (typeof started !== 'boolean'
  ? <span className="text-faint">—</span>
  : <span className={`chip ${started ? 'bg-accent-soft text-accent' : 'bg-sunken text-muted'}`}>{started ? 'running' : 'stopped'}</span>)

const Spec = ({ box }) => (box ? (
  <span className="text-xs text-muted">
    {[box.cpu && `${box.cpu} vCPU`, box.memory_mb && fmtMem(box.memory_mb), box.disk_gb && `${box.disk_gb} GB`].filter(Boolean).join(' · ') || '—'}
  </span>
) : <span className="text-faint">—</span>)

const REQUEST_CLS = {
  pending: 'bg-accent-soft text-accent',
  approved: 'bg-accent-soft text-accent',
  done: 'bg-sunken text-muted',
  denied: 'bg-sunken text-faint',
  failed: 'bg-danger-soft text-danger',
}

function TeamCard({ team, specs, onRedeploy, onDelete }) {
  const confirm = useConfirm()
  const toast = useToast()
  const boxes = team.boxes || []
  // The driver's --teams takes either form ('1' or 'team1'); prefer the identifier it prints.
  const target = team.identifier || team.key
  const del = async () => {
    const ok = await confirm({
      title: `Delete team ${team.key}?`,
      body: "Destroys this team's clones, deactivates it in the scoring engine and revokes its scoreboard + portal logins. Other teams, the engine and the templates are untouched. Quotient has no hard delete, so the team stays listed as inactive there.",
      confirm: 'Delete team', danger: true,
    })
    if (!ok) return
    try { await onDelete(team.key); toast('Team removal submitted') } catch (e) { toast(e.message, 'error') }
  }
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 pt-4 pb-3">
        <h3 className="font-mono text-base font-semibold">{team.key}</h3>
        {team.identifier && <span className="chip bg-sunken text-muted">id {team.identifier}</span>}
        {team.subnet && (
          <span className="flex items-center gap-1.5 font-mono text-xs text-muted">
            <Icon name="net" className="h-3.5 w-3.5 text-faint" />{team.subnet}
          </span>
        )}
        {team.engine_ip && (
          <span className="flex items-center gap-1.5 text-xs text-muted" title="Engine-side gateway NIC on this team's subnet">
            <Icon name="cpu" className="h-3.5 w-3.5 text-faint" />engine <span className="font-mono">{team.engine_ip}</span>
          </span>
        )}
        {/* Bulk actions leave the box empty: team-only is the driver's "every box of this team". */}
        <div className="ml-auto flex flex-wrap gap-2">
          <Button variant="ghost" className="!py-1 text-xs" disabled={!boxes.length}
            onClick={() => onRedeploy('', target, 'reset')}>
            <Icon name="box" className="h-3.5 w-3.5" />Reset all boxes
          </Button>
          <Button variant="ghost" className="!py-1 text-xs" disabled={!boxes.length}
            onClick={() => onRedeploy('', target, 'rebuild')}>
            <Icon name="rocket" className="h-3.5 w-3.5" />Rebuild all
          </Button>
          <Button variant="ghost" className="!py-1 text-xs" disabled={!boxes.length}
            onClick={() => onRedeploy('', target, 'rollback-ready')}>
            <Icon name="back" className="h-3.5 w-3.5" />Rollback ready
          </Button>
          <Button variant="ghost" className="!py-1 text-xs text-danger" onClick={del}>
            <Icon name="trash" className="h-3.5 w-3.5" />Delete team
          </Button>
        </div>
      </div>

      {boxes.length === 0 ? (
        <p className="px-5 pb-4 text-sm text-faint">The inventory lists no boxes for this team.</p>
      ) : (
        <table className="w-full text-left">
          <thead>
            <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
              <th className="px-4 py-2">Box</th>
              <th className="px-4 py-2">Platform</th>
              <th className="px-4 py-2">Template</th>
              <th className="px-4 py-2">VMID</th>
              <th className="px-4 py-2">IP</th>
              <th className="px-4 py-2">Node</th>
              <th className="px-4 py-2">Spec</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2 text-right">Redeploy</th>
            </tr>
          </thead>
          <tbody className="divide-y-2 divide-line">
            {boxes.map((b) => {
              const spec = specs.get(b.name)
              return (
                <tr key={b.name} className="text-sm">
                  <td className="px-4 py-2 font-mono">{b.name}</td>
                  <td className="px-4 py-2">{spec ? <PlatformBadge platform={kindOf(spec)} /> : <span className="text-faint">—</span>}</td>
                  <td className="px-4 py-2 font-mono text-xs text-muted">{spec?.template || '—'}</td>
                  <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{b.vmid ?? '—'}</td>
                  <td className="px-4 py-2 font-mono text-xs text-muted">{b.ip || '—'}</td>
                  <td className="px-4 py-2 font-mono text-xs text-muted">{b.node || '—'}</td>
                  <td className="px-4 py-2"><Spec box={spec} /></td>
                  <td className="px-4 py-2"><Status started={b.started} /></td>
                  <td className="px-4 py-2 text-right">
                    <select className={`${inputCls} !w-auto !py-1 text-xs`} value="" aria-label={`Redeploy ${b.name}`}
                      onChange={(e) => onRedeploy(b.name, target, e.target.value)}>
                      <option value="" disabled>Redeploy…</option>
                      {REDEPLOY_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Card>
  )
}

// No inventory: the run's teams and the comp's boxes are a definition, not an observation.
function Definition({ run, comp }) {
  const n = Number(run?.params?.teams) || 0
  const boxes = comp?.boxes || []
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 px-5 pt-4 pb-3">
        <span className="chip bg-sunken text-faint">competition definition</span>
        <p className="text-xs text-faint">
          No deployed inventory is available for this run, so this is what {comp?.name || run?.comp_id || 'the competition'} defines — not what is on the range.
        </p>
      </div>
      {!!n && <p className="px-5 pb-3 text-sm text-muted">This run requested <b className="tabular-nums">{n}</b> team{n === 1 ? '' : 's'}.</p>}
      {boxes.length === 0 ? (
        <p className="px-5 pb-4 text-sm text-faint">The competition defines no boxes.</p>
      ) : (
        <table className="w-full text-left">
          <thead>
            <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
              <th className="px-4 py-2">Box</th>
              <th className="px-4 py-2">Platform</th>
              <th className="px-4 py-2">Template</th>
              <th className="px-4 py-2">Address</th>
              <th className="px-4 py-2">Spec</th>
            </tr>
          </thead>
          <tbody className="divide-y-2 divide-line">
            {boxes.map((b) => (
              <tr key={b.name} className="text-sm">
                <td className="px-4 py-2 font-mono">{b.name}</td>
                <td className="px-4 py-2"><PlatformBadge platform={kindOf(b)} /></td>
                <td className="px-4 py-2 font-mono text-xs text-muted">{b.template || '—'}</td>
                <td className="px-4 py-2 font-mono text-xs text-muted">192.168.&lt;team&gt;.{b.last_octet ?? '—'}</td>
                <td className="px-4 py-2"><Spec box={b} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

function Machines({ machines }) {
  const eng = machines?.scoring_engine
  const red = machines?.red01
  const tpl = machines?.engine_template
  const rows = [
    eng && { role: 'Scoring engine', name: eng.name, vmid: eng.vmid, node: eng.node, ips: [eng.mgmt_ip, ...(eng.team_ips || [])].filter(Boolean) },
    red && { role: 'red01', vmid: red.vmid, node: red.node, ips: [red.ip].filter(Boolean) },
    tpl && { role: 'Engine template', vmid: tpl.vmid, node: tpl.node, ips: [] },
    ...(machines?.goldens || []).map((g) => ({ role: 'Golden', name: g.name, vmid: g.vmid, node: g.node, ips: [] })),
  ].filter(Boolean)
  if (!rows.length) return null
  // Columns the inventory actually knows about: a workspace projection without node or
  // address facts renders a wall of "—" otherwise.
  const showNode = rows.some((r) => r.node)
  const showIps = rows.some((r) => r.ips.length)
  return (
    <Card className="overflow-hidden">
      <div className="px-5 pt-4 pb-3">
        <h3 className="text-sm font-semibold">Machines outside the teams</h3>
        <p className="mt-1 text-xs text-faint">The range's engine, red box, template and golden images — none of them on a team subnet.</p>
      </div>
      <table className="w-full text-left">
        <thead>
          <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
            <th className="px-4 py-2">Role</th>
            <th className="px-4 py-2">Name</th>
            <th className="px-4 py-2">VMID</th>
            {showNode && <th className="px-4 py-2">Node</th>}
            {showIps && <th className="px-4 py-2">Addresses</th>}
          </tr>
        </thead>
        <tbody className="divide-y-2 divide-line">
          {rows.map((r, i) => (
            <tr key={`${r.role}-${r.vmid}-${i}`} className="text-sm">
              <td className="px-4 py-2 text-muted">{r.role}</td>
              <td className="px-4 py-2 font-mono">{r.name || '—'}</td>
              <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{r.vmid ?? '—'}</td>
              {showNode && <td className="px-4 py-2 font-mono text-xs text-muted">{r.node || '—'}</td>}
              {showIps && <td className="px-4 py-2 font-mono text-xs text-muted">{r.ips.join(', ') || '—'}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

// The reset requests the range's teams filed through the portal. Approve submits a
// redeploy-reset run against that box; the run's exit resolves the request (done/failed)
// and the portal chip follows on the next sync.
function ResetRequests({ rangeKey, compId }) {
  const [rows, setRows] = useState(null)
  const toast = useToast()
  const reload = useCallback(() => api.requests()
    .then((rs) => setRows(rs.filter((r) => r.range_key === rangeKey)))
    .catch(() => setRows([])), [rangeKey])
  useEffect(() => {
    if (!rangeKey) return undefined
    reload()
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [rangeKey, reload]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!rangeKey || !rows?.length) return null
  const act = (id, fn, label) => async () => {
    try { await fn(id); toast(label); reload() } catch (e) { toast(e.message, 'error') }
  }
  return (
    <Card className="overflow-hidden">
      <div className="px-5 pt-4 pb-3">
        <h3 className="text-sm font-semibold">Reset requests</h3>
        <p className="mt-1 text-xs text-faint">Teams asked for a box reset through the portal. Approve runs a redeploy reset against that box; deny tells the team no.</p>
      </div>
      <table className="w-full text-left">
        <thead>
          <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
            <th className="px-4 py-2">Team</th>
            <th className="px-4 py-2">Box</th>
            <th className="px-4 py-2">By</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2 text-right">Resolve</th>
          </tr>
        </thead>
        <tbody className="divide-y-2 divide-line">
          {rows.map((r) => (
            <tr key={r.id} className="text-sm">
              <td className="px-4 py-2 font-mono">{r.team}</td>
              <td className="px-4 py-2 font-mono">{r.box}</td>
              <td className="px-4 py-2 text-xs text-muted">{r.requested_by || '—'}<span className="text-faint"> · {r.requested_at || ''}</span></td>
              <td className="px-4 py-2">
                <span className={`chip ${REQUEST_CLS[r.status] || 'bg-sunken text-muted'}`}>
                  {r.status === 'approved' && <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
                  {r.status}
                </span>
              </td>
              <td className="px-4 py-2 text-right">
                {(r.status === 'pending' || r.status === 'failed') && (
                  <span className="inline-flex gap-2">
                    <Button variant="ghost" className="!py-1 text-xs" onClick={act(r.id, api.approveRequest, 'Reset approved')}>
                      Approve
                    </Button>
                    <Button variant="ghost" className="!py-1 text-xs" onClick={act(r.id, api.denyRequest, 'Reset denied')}>
                      Deny
                    </Button>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

export default function RunTeams({ run, comp, inventory }) {
  const [reset, setReset] = useState(null)
  const toast = useToast()
  const specs = specMap(comp)
  const teams = inventory?.teams || []
  const redeploy = (box, team, mode) => setReset({ box, team, mode })
  const deployedFor = Number(run?.params?.teams) || rangeTeamCount(inventory)
  // This run's range workspace: the same key the competition panel and the reset
  // requests address.
  const rangeKey = run?.params?._destroy_comp_dir || (run?.run_dir ? `${run.run_dir}/comp` : '')
  const deleteTeam = (teamKey) => api.submitRun({
    comp_id: run.comp_id, action: 'delete_team',
    params: { team: teamKey, comp_dir: rangeKey },
  })
  return (
    <div className="space-y-5">
      <ResetRequests rangeKey={rangeKey} />
      {!inventory ? <Definition run={run} comp={comp} /> : teams.length ? (
        teams.map((t) => <TeamCard key={t.key} team={t} specs={specs} onRedeploy={redeploy} onDelete={deleteTeam} />)
      ) : (
        <Card className="p-2">
          <EmptyState icon="boxes" title="No teams in the inventory">
            The range's inventory lists no live team networks right now
            {deployedFor ? <> — it was deployed for <b className="tabular-nums">{deployedFor}</b> team{deployedFor === 1 ? '' : 's'}, so the range is most likely torn down or still building</> : ''}
            {inventory?.range?.deploy_run_id
              ? <> — the deploying run <Link to={`/c/${inventory.range.comp_id}/runs/${inventory.range.deploy_run_id}`} className="font-mono underline">{inventory.range.deploy_run_id}</Link> has the build record.</>
              : '.'}
          </EmptyState>
        </Card>
      )}

      {!!inventory && <Machines machines={inventory.machines} />}

      {run && (
        <ResetBoxModal open={!!reset} onClose={() => setReset(null)} run={run} teams={inventory?.teams} initial={reset || {}}
          onSubmitted={() => toast('Redeploy submitted')} />
      )}
    </div>
  )
}

// inventory.range.teams is the team count the range was deployed with — the same number the
// Overview panel shows, so the empty state can reconcile the two views.
const rangeTeamCount = (inventory) => inventory?.range?.teams ?? null

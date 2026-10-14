// Teams tab of the run page: the range's teams and their deployed boxes, plus the machines
// that live outside the teams. Props: run (run record), comp (competition detail),
// inventory (GET /api/runs/{id}/inventory payload, or null while loading).
import { useState } from 'react'
import { kindOf } from '../api'
import { Button, Card, PlatformBadge, inputCls } from './ui'
import { EmptyState, Icon, fmtMem, useToast } from './kit'
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

function TeamCard({ team, specs, onRedeploy }) {
  const boxes = team.boxes || []
  // The driver's --teams takes either form ('1' or 'team1'); prefer the identifier it prints.
  const target = team.identifier || team.key
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
            <th className="px-4 py-2">Node</th>
            <th className="px-4 py-2">Addresses</th>
          </tr>
        </thead>
        <tbody className="divide-y-2 divide-line">
          {rows.map((r, i) => (
            <tr key={`${r.role}-${r.vmid}-${i}`} className="text-sm">
              <td className="px-4 py-2 text-muted">{r.role}</td>
              <td className="px-4 py-2 font-mono">{r.name || '—'}</td>
              <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{r.vmid ?? '—'}</td>
              <td className="px-4 py-2 font-mono text-xs text-muted">{r.node || '—'}</td>
              <td className="px-4 py-2 font-mono text-xs text-muted">{r.ips.join(', ') || '—'}</td>
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
  return (
    <div className="space-y-5">
      {!inventory ? <Definition run={run} comp={comp} /> : teams.length ? (
        teams.map((t) => <TeamCard key={t.key} team={t} specs={specs} onRedeploy={redeploy} />)
      ) : (
        <Card className="p-2">
          <EmptyState icon="boxes" title="No teams in the inventory">
            The range's inventory lists no teams — nothing is deployed under it, or this is not a team-based competition.
          </EmptyState>
        </Card>
      )}

      {!!inventory && <Machines machines={inventory.machines} />}

      {run && (
        <ResetBoxModal open={!!reset} onClose={() => setReset(null)} run={run} initial={reset || {}}
          onSubmitted={() => toast('Redeploy submitted')} />
      )}
    </div>
  )
}

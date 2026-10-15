// Infrastructure tab of the run page: what the range actually deployed — networks, boxes,
// engine, red01, goldens, and the deploy's own facts. Props: run (run record),
// comp (competition detail), inventory (GET /api/runs/{id}/inventory payload, or null while
// loading).
import { Link } from 'react-router-dom'
import { kindOf } from '../api'
import { Card, PlatformBadge } from './ui'
import { EmptyState, Icon } from './kit'
import { DEPLOY_STAGES } from './runui'

const NET_KIND = { team: 'Team', transit: 'Transit', satellite: 'Satellite' }

// The deploy state stores the last phase as a number; a bare "8" told the operator nothing.
const phaseName = (n) => {
  const i = Number(n)
  return Number.isInteger(i) && i >= 1 && i <= DEPLOY_STAGES.length ? `${i}/8 · ${DEPLOY_STAGES[i - 1]}` : String(n ?? '—')
}

// The range's state, not the run's: a live range outlives the deploy run that built it.
// teardown_failed means the estate's records are gone but a VM survived (ranges.py) — a
// state that must not read as calm.
const RANGE_STATE = {
  live: 'bg-accent-soft text-accent',
  building: 'bg-sunken text-muted',
  retired: 'bg-sunken text-faint',
  failed: 'bg-danger-soft text-danger',
  teardown_failed: 'bg-danger-soft text-danger',
}

const TH = ({ children }) => <th className="px-4 py-2">{children}</th>

function Stat({ k, v }) {
  return (
    <div className="min-w-16 rounded-2xl bg-sunken px-4 py-2 text-center shadow-inset">
      <div className="text-lg font-semibold tabular-nums">{v}</div>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">{k}</div>
    </div>
  )
}

const Fact = ({ k, children }) => (
  <div>
    <div className="label !mb-0.5">{k}</div>
    <div className="text-sm">{children}</div>
  </div>
)

const Bool = ({ v }) => (v == null
  ? <span className="text-faint">—</span>
  : <span className={`chip ${v ? 'bg-accent-soft text-accent' : 'bg-sunken text-muted'}`}>{v ? 'yes' : 'no'}</span>)

function DeployFacts({ deploy }) {
  if (!deploy) return null
  const degradations = deploy.degradations || []
  return (
    <Card className="p-5">
      <h3 className="label">Deploy</h3>
      <div className="flex flex-wrap items-start gap-x-10 gap-y-3">
        <Fact k="Last phase"><span className="font-mono tabular-nums">{deploy.last_phase != null ? phaseName(deploy.last_phase) : '—'}</span></Fact>
        <Fact k="Seeded"><Bool v={deploy.seeded} /></Fact>
        <Fact k="Multi-node"><Bool v={deploy.multi_node} /></Fact>
        {deploy.endpoint && (
          <Fact k="Endpoint">
            <a href={deploy.endpoint} target="_blank" rel="noreferrer" className="break-all font-mono text-xs text-accent hover:underline">{deploy.endpoint}</a>
          </Fact>
        )}
      </div>
      {degradations.length > 0 && (
        <div className="mt-4 space-y-2">
          {degradations.map((d, i) => (
            <div key={i} className="flex items-start gap-2 rounded-2xl bg-danger-soft px-4 py-2.5 text-xs text-danger">
              <Icon name="warn" className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{typeof d === 'string' ? d : JSON.stringify(d)}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function Networks({ networks }) {
  return (
    <Card className="overflow-hidden">
      <h3 className="px-5 pt-4 pb-3 text-sm font-semibold">Networks <span className="font-normal text-faint">{networks.length}</span></h3>
      <table className="w-full text-left">
        <thead>
          <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
            <TH>Name</TH><TH>Kind</TH><TH>Node</TH><TH>Teams</TH><TH>Comment</TH>
          </tr>
        </thead>
        <tbody className="divide-y-2 divide-line">
          {networks.map((n, i) => (
            <tr key={`${n.node}-${n.name}-${i}`} className="text-sm">
              <td className="px-4 py-2 font-mono">{n.name}</td>
              <td className="px-4 py-2"><span className="chip bg-sunken text-muted">{NET_KIND[n.kind] || n.kind || '—'}</span></td>
              <td className="px-4 py-2 font-mono text-xs text-muted">{n.node || '—'}</td>
              <td className="px-4 py-2 font-mono text-xs text-muted">{(n.teams || []).join(', ') || '—'}</td>
              <td className="px-4 py-2 text-xs text-muted" title={n.comment || ''}>{n.comment || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function Boxes({ teams, specs }) {
  const rows = teams.flatMap((t) => (t.boxes || []).map((b) => ({ ...b, team: t.key })))
  return (
    <Card className="overflow-hidden">
      <h3 className="px-5 pt-4 pb-3 text-sm font-semibold">Boxes <span className="font-normal text-faint">{rows.length}</span></h3>
      <table className="w-full text-left">
        <thead>
          <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
            <TH>Team</TH><TH>Box</TH><TH>Platform / template</TH><TH>VMID</TH><TH>IP</TH><TH>Node</TH>
          </tr>
        </thead>
        <tbody className="divide-y-2 divide-line">
          {rows.map((b, i) => {
            const spec = specs.get(b.name)
            return (
              <tr key={`${b.team}-${b.name}-${i}`} className="text-sm">
                <td className="px-4 py-2 font-mono text-xs text-muted">{b.team}</td>
                <td className="px-4 py-2 font-mono">{b.name}</td>
                <td className="px-4 py-2">
                  <span className="flex flex-wrap items-center gap-2">
                    {spec ? <PlatformBadge platform={kindOf(spec)} /> : <span className="text-faint">—</span>}
                    <span className="font-mono text-xs text-muted">{spec?.template || '—'}</span>
                  </span>
                </td>
                <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{b.vmid ?? '—'}</td>
                <td className="px-4 py-2 font-mono text-xs text-muted">{b.ip || '—'}</td>
                <td className="px-4 py-2 font-mono text-xs text-muted">{b.node || '—'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
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
  // Columns the inventory actually knows about — a projection without node/address facts
  // otherwise renders a wall of "—".
  const showNode = rows.some((r) => r.node)
  const showIps = rows.some((r) => r.ips.length)
  return (
    <Card className="overflow-hidden">
      <h3 className="px-5 pt-4 pb-3 text-sm font-semibold">Machines <span className="font-normal text-faint">outside the teams</span></h3>
      <table className="w-full text-left">
        <thead>
          <tr className="border-t-2 border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
            <TH>Role</TH><TH>Name</TH><TH>VMID</TH>
            {showNode && <TH>Node</TH>}
            {showIps && <TH>Addresses</TH>}
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

export default function RunInfra({ comp, inventory }) {
  if (!inventory) {
    return (
      <Card className="p-2">
        <EmptyState icon="boxes" title="No deployment inventory">
          Nothing to list yet — the inventory appears once the run has a workspace, and a range deployed by hand has none.
        </EmptyState>
      </Card>
    )
  }

  const range = inventory.range
  const teams = inventory.teams || []
  const networks = inventory.networks || []
  const counts = inventory.counts || {}
  const specs = new Map((comp?.boxes || []).map((b) => [b.name, b]))
  const boxCount = teams.reduce((n, t) => n + (t.boxes || []).length, 0)
  const retired = range?.state === 'retired'
  const failed = range?.state === 'teardown_failed'
  const empty = !teams.length && !networks.length && !boxCount

  return (
    <div className="space-y-5">
      <Card className="flex flex-wrap items-center gap-x-6 gap-y-3 p-5">
        <Fact k="Range">
          <span className="flex items-center gap-2">
            <span className={`chip ${RANGE_STATE[range?.state] || 'bg-sunken text-muted'}`}>{range?.state || 'unknown'}</span>
            {range?.key && <span className="font-mono text-xs text-faint">{range.key}</span>}
          </span>
        </Fact>
        {range?.deploy_run_id && (
          <Fact k="Deployed by">
            <Link to={`/c/${range.comp_id}/runs/${range.deploy_run_id}`} className="font-mono text-sm text-accent hover:underline">{range.deploy_run_id}</Link>
          </Fact>
        )}
        <div className="ml-auto flex gap-3">
          <Stat k="Teams" v={counts.teams ?? teams.length} />
          <Stat k="Boxes" v={counts.boxes ?? boxCount} />
          <Stat k="Networks" v={counts.networks ?? networks.length} />
        </div>
      </Card>

      {empty ? (
        <Card className="p-2">
          <EmptyState icon={failed ? 'warn' : retired ? 'trash' : 'boxes'}
            title={failed ? "The range's teardown failed" : retired ? 'This range has been retired' : 'The inventory is empty'}>
            {failed
              ? 'Terraform no longer tracks this range, but a machine survived the teardown — anything listed below is still out there.'
              : retired
                ? <>Its machines were torn down{range?.retired_by ? <> by <Link to={`/c/${range.comp_id}/runs/${range.retired_by}`} className="font-mono underline">{range.retired_by}</Link></> : null}.</>
                : 'This run owns no deployed teams, boxes or networks — nothing is on the range.'}
          </EmptyState>
        </Card>
      ) : (retired || failed) && (
        <div className="flex items-start gap-2 rounded-2xl bg-danger-soft px-4 py-2.5 text-sm text-danger">
          <Icon name={failed ? 'warn' : 'trash'} className="mt-0.5 h-4 w-4 shrink-0" />
          {failed
            ? <span>This range's teardown failed — the estate's own machines are gone but something survived it.</span>
            : <span>This range has been retired{range?.retired_by ? <> by run <Link to={`/c/${range.comp_id}/runs/${range.retired_by}`} className="font-mono underline">{range.retired_by}</Link></> : null} — below is its last known state.</span>}
        </div>
      )}

      <DeployFacts deploy={inventory.deploy} />
      {networks.length > 0 && <Networks networks={networks} />}
      {boxCount > 0 && <Boxes teams={teams} specs={specs} />}
      {inventory.machines && <Machines machines={inventory.machines} />}
    </div>
  )
}

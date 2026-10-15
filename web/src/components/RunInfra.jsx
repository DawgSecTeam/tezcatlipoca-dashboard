// Infrastructure tab: what the range recorded beyond its teams — teardown journal, deploy
// facts, shared machines and networks. Team boxes live on the Teams tab. Props: run (run record),
// comp (competition detail), inventory (GET /api/runs/{id}/inventory payload, or null while
// loading).
import { Link } from 'react-router-dom'
import { Card } from './ui'
import { EmptyState, Icon } from './kit'
import { DEPLOY_STAGES } from './runui'
import MachineInventory from './MachineInventory'

const NET_KIND = { team: 'Team', transit: 'Transit', satellite: 'Satellite' }

// The deploy state stores the last phase as a number; a bare "8" told the operator nothing.
const phaseName = (n) => {
  const i = Number(n)
  return Number.isInteger(i) && i >= 1 && i <= DEPLOY_STAGES.length ? `${i}/8 · ${DEPLOY_STAGES[i - 1]}` : String(n ?? '—')
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
              <span>{typeof d === 'string' ? d : d.message || d.reason || d.detail || JSON.stringify(d)}</span>
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
      <div className="overflow-x-auto"><table className="w-full text-left">
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
      </table></div>
    </Card>
  )
}

const TEARDOWN_STEP = {
  terraform: 'Terraform-managed resources', red01: 'Red box (red01)',
  engine: 'Scoring engine', scoring_engine: 'Scoring engine',
  team_boxes: 'Team boxes', networks: 'Networks', goldens: 'Golden images',
  engine_template: 'Engine template',
}

function TeardownJournal({ teardown, opHref }) {
  const done = Array.isArray(teardown?.done) ? teardown.done : Object.keys(teardown?.done || {})
  const recorded = !!teardown?.run_id || !!done.length
  return (
    <Card className="p-5">
      <h3 className="text-sm font-semibold">Teardown journal</h3>
      {!recorded ? <p className="mt-2 text-xs text-muted">No teardown steps have been recorded for this workspace.</p> : <>
        {teardown.run_id && <p className="mt-2 text-xs text-muted">Operation · {opHref
          ? <Link to={opHref(teardown.run_id)} className="font-mono text-accent hover:underline">{teardown.run_id}</Link>
          : <span className="font-mono">{teardown.run_id}</span>}</p>}
        {done.length > 0 ? <ul className="mt-3 flex flex-wrap gap-2">{done.map((step) => (
          <li key={step} className="chip gap-1.5 bg-sunken px-2.5 py-1 text-xs text-muted" title={step}><Icon name="check" className="h-3.5 w-3.5" />{TEARDOWN_STEP[step] || step.replace(/[_-]+/g, ' ')} · completed</li>
        ))}</ul> : <p className="mt-2 text-xs text-muted">No completed steps recorded.</p>}
        <p className={`mt-3 text-xs ${teardown.red01_done ? 'text-muted' : 'text-danger'}`}>
          {teardown.red01_done ? 'Red box teardown is recorded as complete.' : 'Red box teardown has not been recorded as complete. Check the operation log before assuming it or its credentials are gone.'}
        </p>
        <p className="mt-2 text-xs text-faint">A journal records completed steps, not a live scan or a guarantee that every resource was removed.</p>
      </>}
    </Card>
  )
}

export default function RunInfra({ run, comp, inventory }) {
  if (!inventory) {
    return (
      <Card className="p-2">
        <EmptyState icon="boxes" title="No deployment inventory">
          No workspace inventory is available. Competition definitions are not a substitute for recorded deployment inventory.
        </EmptyState>
      </Card>
    )
  }

  const range = inventory.range
  const teams = inventory.teams || []
  const networks = inventory.networks || []
  const counts = inventory.counts || {}
  const boxCount = teams.reduce((n, t) => n + (t.boxes || []).length, 0)
  const retired = range?.state === 'retired'
  const failed = range?.state === 'teardown_failed'
  const empty = !teams.length && !networks.length && !boxCount
  const compId = range?.comp_id || run?.comp_id
  const opHref = (id) => (range?.id
    ? `/ranges/${range.id}/activity?operation=${encodeURIComponent(id)}`
    : compId ? `/c/${encodeURIComponent(compId)}/runs/${encodeURIComponent(id)}` : null)
  const teardown = inventory.teardown
  const journal = !!teardown?.run_id || !!(Array.isArray(teardown?.done) ? teardown.done.length : Object.keys(teardown?.done || {}).length)

  return (
    <div className="space-y-5">
      {empty ? (
        <Card className="p-2">
          <EmptyState icon={failed ? 'warn' : retired ? 'trash' : 'boxes'}
            title={failed ? "The range's teardown failed" : retired ? 'This range has been retired' : 'The inventory is empty'}>
            {failed
              ? 'The last teardown did not complete successfully. Empty team and network records do not prove that every machine was removed; inspect the journal and operation log.'
              : retired
                ? <>A successful teardown was recorded{range?.retired_by ? <> by <Link to={opHref(range.retired_by)} className="font-mono underline">{range.retired_by}</Link></> : null}. Shared machine records below may be historical.</>
                : 'No teams, boxes or networks are recorded. A partial deployment or teardown can leave incomplete records; absence here is not a live observation.'}
          </EmptyState>
        </Card>
      ) : (retired || failed) && (
        <div className="flex items-start gap-2 rounded-2xl bg-danger-soft px-4 py-2.5 text-sm text-danger">
          <Icon name={failed ? 'warn' : 'trash'} className="mt-0.5 h-4 w-4 shrink-0" />
          {failed
            ? <span>The last teardown did not complete successfully. Review the journal and operation log; the records below do not establish which resources remain.</span>
            : <span>This range has been retired{range?.retired_by ? <> by run <Link to={opHref(range.retired_by)} className="font-mono underline">{range.retired_by}</Link></> : null} — below is its last known state.</span>}
        </div>
      )}

      {(journal || retired || failed) && <TeardownJournal teardown={teardown} opHref={opHref} />}
      <DeployFacts deploy={inventory.deploy} />
      <MachineInventory machines={inventory.machines} />
      {networks.length > 0 && <Networks networks={networks} />}

      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="min-w-0 flex-1">
            <div className="label !mb-1">Workspace</div>
            <p className="break-all font-mono text-xs text-muted">{range?.key || 'Unknown workspace'}</p>
          </div>
          <div className="flex gap-3">
            <Stat k="Teams" v={counts.teams ?? teams.length} />
            <Stat k="Boxes" v={counts.boxes ?? boxCount} />
            <Stat k="Networks" v={counts.networks ?? networks.length} />
          </div>
        </div>
        {boxCount > 0 && range?.id && <p className="mt-3 text-xs"><Link to={`/ranges/${range.id}/teams`} className="text-accent hover:underline">Team boxes, addresses and recovery are on the Teams tab</Link></p>}
        {!journal && !retired && !failed && <p className="mt-3 text-xs text-muted">No teardown steps have been recorded for this workspace.</p>}
        <p className="mt-2 text-xs text-faint">Counts and addresses come from recorded workspace state, not a live hypervisor scan. Power and reachability are not checked; leftover records may survive teardown.</p>
      </Card>
    </div>
  )
}

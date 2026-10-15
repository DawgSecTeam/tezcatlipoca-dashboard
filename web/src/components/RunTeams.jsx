// Recorded team/box inventory for a range, not a projection of its competition definition.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, kindOf } from '../api'
import { Button, Card, ErrorBanner, PlatformBadge, inputCls } from './ui'
import { EmptyState, Icon, fmtMem, useConfirm, useToast } from './kit'
import { REDEPLOY_MODES, ResetBoxModal } from './runui'
import MachineInventory from './MachineInventory'

const REQUEST_CLS = {
  // A pending request is waiting on the operator, the same thing the Teams badge counts:
  // the two must not colour the same state differently on one screen.
  pending: 'bg-danger-soft text-danger', approved: 'bg-accent-soft text-accent',
  done: 'bg-sunken text-muted', denied: 'bg-sunken text-faint', failed: 'bg-danger-soft text-danger',
}
const MODE_LABEL = {
  rebuild: 'Rebuild from template', 'rollback-ready': 'Roll back to ready snapshot',
  'rollback-base': 'Roll back to base snapshot', reconfigure: 'Reapply configuration',
  resync: 'Resync recorded state',
}
const recoveryModes = REDEPLOY_MODES.filter((mode) => mode !== 'reset' && mode !== 'engine-recovery')
const matches = (values, query) => values.filter((value) => value != null).join(' ').toLowerCase().includes(query)

function RecoveryMenu({ label, disabled, onSelect, children }) {
  return (
    <details className="relative text-xs">
      <summary className="cursor-pointer rounded-lg px-2 py-1 text-muted hover:bg-sunken hover:text-fg" aria-label={label}>
        Advanced
      </summary>
      <div className="mt-1 flex min-w-48 flex-col gap-1 rounded-xl border border-line bg-sunken p-2">
        <p className="px-2 py-1 text-[11px] text-faint">{label}</p>
        {recoveryModes.map((mode) => (
          <button key={mode} type="button" disabled={disabled} className="rounded-lg px-2 py-1.5 text-left text-muted hover:bg-bg disabled:opacity-40"
            onClick={(event) => { event.currentTarget.closest('details').open = false; onSelect(mode) }}>
            {MODE_LABEL[mode] || mode}
          </button>
        ))}
        {children}
      </div>
    </details>
  )
}

function TeamCard({ team, boxes, specs, range, compId, onRedeploy, onDelete, disabled }) {
  const confirm = useConfirm()
  const toast = useToast()
  const [pending, setPending] = useState(false)
  const busy = useRef(false)
  const allBoxes = team.boxes || []
  const showDefinitions = specs.size > 0
  const target = team.identifier || team.key
  const remove = async () => {
    if (disabled || busy.current) return
    const ok = await confirm({
      title: `Remove ${team.key} from this range?`,
      body: <>
        <p>Destroys this team's clones, deactivates its scoring entry and revokes its scoreboard and portal logins. Other teams and shared machines are untouched.</p>
        <dl className="mt-3 space-y-1 text-xs">
          <div><dt className="inline font-semibold">Competition </dt><dd className="inline font-mono">{compId}</dd></div>
          <div><dt className="inline font-semibold">Range </dt><dd className="inline font-mono">{range.id || range.deploy_run_id || range.key}</dd></div>
          <div><dt className="font-semibold">Workspace</dt><dd className="break-all font-mono">{range.key}</dd></div>
        </dl>
        <p className="mt-3">Quotient retains the team as inactive; it does not support hard deletion.</p>
      </>,
      confirm: 'Remove team', danger: true,
    })
    if (!ok || busy.current) return
    busy.current = true; setPending(true)
    try { await onDelete(team.key); toast('Team removal submitted') }
    catch (error) { toast(error.message, 'error') }
    finally { busy.current = false; setPending(false) }
  }
  return (
    <Card className="min-w-0">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
        <h3 className="font-mono text-sm font-semibold">{team.key}</h3>
        {team.identifier && <span className="text-xs text-faint">id <span className="font-mono">{team.identifier}</span></span>}
        {team.subnet && <span className="font-mono text-xs text-muted">{team.subnet}</span>}
        {team.engine_ip && <span className="text-xs text-muted" title="Recorded engine-side gateway address">gateway <span className="font-mono">{team.engine_ip}</span></span>}
        <span className="text-xs text-faint">{boxes.length === allBoxes.length ? allBoxes.length : `${boxes.length}/${allBoxes.length}`} boxes</span>
        <div className="ml-auto flex items-start gap-2">
          <Button variant="ghost" className="!py-1 text-xs" disabled={disabled || pending || !allBoxes.length}
            onClick={() => onRedeploy('', target, 'reset')}>Reset team boxes</Button>
          <RecoveryMenu label={`Recovery for all boxes in ${team.key}`} disabled={disabled || pending || !allBoxes.length}
            onSelect={(mode) => onRedeploy('', target, mode)}>
            <button type="button" className="mt-1 rounded-lg border-t border-line px-2 py-2 text-left text-danger disabled:opacity-40"
              disabled={disabled || pending} onClick={remove}>{pending ? 'Submitting removal…' : 'Remove team…'}</button>
          </RecoveryMenu>
        </div>
      </div>
      {!boxes.length ? <p className="px-4 pb-3 text-xs text-faint">No boxes are recorded for this team.</p> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[30rem] text-left">
            <thead>
              <tr className="border-t border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
                <th className="px-4 py-2">Box</th><th className="px-4 py-2">VMID</th>
                <th className="px-4 py-2">Recorded IP</th><th className="px-4 py-2">Node</th>
                {showDefinitions && <th className="px-4 py-2">Definition details</th>}<th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {boxes.map((box, index) => {
                const spec = specs.get(box.name)
                const resources = spec && [spec.cpu && `${spec.cpu} vCPU`, spec.memory_mb && fmtMem(spec.memory_mb), spec.disk_gb && `${spec.disk_gb} GB`].filter(Boolean).join(' · ')
                return (
                  <tr key={`${box.name}-${box.vmid}-${index}`} className="text-sm">
                    <td className="px-4 py-2 font-mono">{box.name}</td>
                    <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{box.vmid ?? '—'}</td>
                    <td className="px-4 py-2 font-mono text-xs text-muted">{box.ip || '—'}</td>
                    <td className="px-4 py-2 font-mono text-xs text-muted">{box.node || '—'}</td>
                    {showDefinitions && <td className="px-4 py-2">
                      {spec ? <details className="text-xs text-muted"><summary className="cursor-pointer">{spec.template || 'Box definition'}</summary>
                        <div className="mt-2 flex flex-wrap items-center gap-2"><PlatformBadge platform={kindOf(spec)} />{resources}</div>
                        <p className="mt-1 text-faint">Current definition; not verified deployed configuration.</p>
                      </details> : <span className="text-xs text-faint">Unavailable</span>}
                    </td>}
                    <td className="px-4 py-2 text-right">
                      <div className="flex items-start justify-end gap-2">
                        <Button variant="ghost" className="!py-1 text-xs" disabled={disabled || pending}
                          aria-label={`Reset ${team.key} ${box.name}`} onClick={() => onRedeploy(box.name, target, 'reset')}>Reset</Button>
                        <RecoveryMenu label={`Recovery for ${team.key} / ${box.name}`} disabled={disabled || pending}
                          onSelect={(mode) => onRedeploy(box.name, target, mode)} />
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

export function ResetRequests({ rangeKey, rangeId, disabled, onSubmitted, pendingOnly = false }) {
  const [data, setData] = useState({ key: rangeKey, rows: null, error: null })
  const [pending, setPending] = useState(null)
  const busy = useRef(false)
  const active = useRef(null)
  const toast = useToast()
  const reload = useCallback(() => {
    const generation = active.current
    if (!generation) return Promise.resolve()
    return api.requests()
      .then((rows) => { if (active.current === generation) setData({ key: rangeKey, rows: rows.filter((row) => row.range_key === rangeKey), error: null }) })
      .catch((error) => { if (active.current === generation) setData((old) => ({ key: rangeKey, rows: old.key === rangeKey ? old.rows : null, error })) })
  }, [rangeKey])
  useEffect(() => {
    if (!rangeKey) return undefined
    active.current = {}
    let stopped = false, timer
    const tick = async () => { await reload(); if (!stopped) timer = setTimeout(tick, 10000) }
    tick()
    return () => { stopped = true; active.current = null; clearTimeout(timer) }
  }, [rangeKey, reload])
  const allRows = data.key === rangeKey ? data.rows : null
  const rows = pendingOnly ? allRows?.filter((row) => row.status === 'pending' || row.status === 'failed') : allRows
  const error = data.key === rangeKey ? data.error : null
  if (!rangeKey || (!rows?.length && !error)) return null
  const act = (request, approve) => async () => {
    if (busy.current || (approve && disabled)) return
    busy.current = true; setPending(request.id)
    try {
      const created = await (approve ? api.approveRequest : api.denyRequest)(request.id)
      toast(approve ? 'Reset approved' : 'Reset denied')
      if (approve && created?.approved_run) onSubmitted?.({ id: created.approved_run, comp_id: created.comp_id || request.comp_id, action: 'redeploy' })
      reload()
    } catch (e) { toast(e.message, 'error') }
    finally { busy.current = false; if (active.current) setPending(null) }
  }
  return (
    <Card className="overflow-hidden">
      <div className="px-4 py-3"><h3 className="text-sm font-semibold">Reset requests</h3>
        <p className="mt-1 text-xs text-muted">Requests filed by teams in this workspace. Approval submits a reset operation.</p></div>
      {error && <div className="px-4"><ErrorBanner error={error} onRetry={reload} /></div>}
      {!!rows?.length && <div className="overflow-x-auto"><table className="w-full min-w-[34rem] text-left">
        <thead><tr className="border-t border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
          <th className="px-4 py-2">Team / box</th><th className="px-4 py-2">Requested by</th><th className="px-4 py-2">Status</th><th className="px-4 py-2 text-right">Resolve</th>
        </tr></thead>
        <tbody className="divide-y divide-line">{rows.map((request) => (
          <tr key={request.id} className="text-xs">
            <td className="px-4 py-2 font-mono">{request.team} / {request.box}</td>
            <td className="px-4 py-2 text-muted">{request.requested_by || '—'}<div className="text-faint">{request.requested_at || ''}</div></td>
            <td className="px-4 py-2"><span className={`chip ${REQUEST_CLS[request.status] || 'bg-sunken text-muted'}`}>{request.status}</span>
              {request.approved_run && <Link to={rangeId ? `/ranges/${rangeId}/activity?operation=${encodeURIComponent(request.approved_run)}` : `/c/${encodeURIComponent(request.comp_id)}/runs/${encodeURIComponent(request.approved_run)}`} className="mt-1 block text-accent hover:underline">Reset operation</Link>}</td>
            <td className="px-4 py-2 text-right">{(request.status === 'pending' || request.status === 'failed') && <span className="inline-flex gap-2">
              <Button variant="ghost" className="!py-1 text-xs" disabled={disabled || pending != null} onClick={act(request, true)}>{pending === request.id ? 'Submitting…' : 'Approve'}</Button>
              <Button variant="ghost" className="!py-1 text-xs" disabled={pending != null} onClick={act(request, false)}>Deny</Button>
            </span>}</td>
          </tr>
        ))}</tbody>
      </table></div>}
    </Card>
  )
}

export default function RunTeams({ run, comp, inventory, disabled = false, onSubmitted, showMachines = true }) {
  const [reset, setReset] = useState(null)
  const [query, setQuery] = useState('')
  const toast = useToast()
  const range = inventory?.range
  // Never derive a workspace from the currently selected operation: it may be a follow-up.
  const rangeKey = range?.key || ''
  const compId = range?.comp_id || run?.comp_id || comp?.id
  const engineRef = range?.engine_ref ?? run?.engine_ref
  const actionDisabled = disabled || range?.state === 'retired' || !rangeKey || !compId || !engineRef
  const specs = new Map((comp?.boxes || []).map((box) => [box.name, box]))
  const teams = inventory?.teams || []
  const boxCount = teams.reduce((total, team) => total + (team.boxes || []).length, 0)
  const needle = query.trim().toLowerCase()
  const filtered = teams.map((team) => {
    const teamMatch = matches([team.key, team.identifier, team.subnet, team.engine_ip], needle)
    const boxes = teamMatch ? team.boxes || [] : (team.boxes || []).filter((box) => matches([box.name, box.vmid, box.ip, box.node, ...(box.tags || [])], needle))
    return { team, boxes, visible: teamMatch || boxes.length > 0 }
  }).filter((entry) => entry.visible)
  const resetRun = useMemo(() => ({
    id: range?.deploy_run_id || run?.id, comp_id: compId, engine_ref: engineRef,
    params: { teams: range?.teams ?? run?.params?.teams, comp_dir: rangeKey, _destroy_comp_dir: rangeKey },
  }), [range?.deploy_run_id, run?.id, compId, engineRef, range?.teams, run?.params?.teams, rangeKey])
  const redeploy = (box, team, mode) => { if (!actionDisabled) setReset({ box, team, mode }) }
  const submitted = (created) => { toast('Redeploy submitted'); onSubmitted?.(created) }
  const deleteTeam = async (teamKey) => {
    if (actionDisabled) throw new Error('This range is not available for recovery actions.')
    const created = await api.submitRun({ comp_id: compId, engine_ref: engineRef, action: 'delete_team', params: { team: teamKey, comp_dir: rangeKey } })
    onSubmitted?.(created)
    return created
  }
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-semibold">Recorded teams & boxes</h2>
          {inventory && <span className="text-xs text-muted">{teams.length} teams · {boxCount} boxes</span>}
          {!!teams.length && <label className="ml-auto w-full sm:w-80"><span className="sr-only">Search recorded teams and boxes</span>
            <input type="search" className={`${inputCls} text-sm`} placeholder="Search team, box, VMID, IP or node…" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>}
        </div>
        <p className="mt-2 text-xs text-muted">Workspace records, not live presence or power checks. Missing records do not prove that a machine is gone.</p>
        {range?.state === 'retired' && <p className="mt-2 text-xs text-muted">This range is retired. Historical records are shown; recovery actions are disabled.</p>}
        {inventory && range?.state !== 'retired' && (!rangeKey || !compId || !engineRef) && <p className="mt-2 text-xs text-danger">Recovery is unavailable until the range workspace, competition and originating engine ref are known.</p>}
      </Card>
      <ResetRequests key={rangeKey} rangeKey={rangeKey} rangeId={range?.id} disabled={actionDisabled} onSubmitted={onSubmitted} />
      {!inventory ? <Card><EmptyState icon="boxes" title="No recorded inventory available">Competition definitions are not deployed inventory. Load this range's workspace inventory to inspect its teams and boxes.</EmptyState></Card>
        : !teams.length ? <Card><EmptyState icon="boxes" title="No teams recorded">This workspace has no team inventory. A partial deployment or teardown can leave incomplete records; this is not a live scan.</EmptyState></Card>
          : !filtered.length ? <Card><EmptyState icon="search" title="No matching teams or boxes">Try a team name, box, VMID, IP address or node.<button type="button" className="mt-2 block w-full text-accent" onClick={() => setQuery('')}>Clear search</button></EmptyState></Card>
            : filtered.map(({ team, boxes }) => <TeamCard key={team.key} team={team} boxes={boxes} specs={specs} range={range} compId={compId} onRedeploy={redeploy} onDelete={deleteTeam} disabled={actionDisabled} />)}
      {inventory && showMachines && <MachineInventory machines={inventory.machines} />}
      {!!rangeKey && !!compId && !actionDisabled && <ResetBoxModal open={!!reset} onClose={() => setReset(null)} run={resetRun} range={range} teams={teams} initial={reset || {}} onSubmitted={submitted} />}
    </div>
  )
}

// Shared, recorded workspace machines. These records are not a hypervisor power check.
import { Card } from './ui'

export default function MachineInventory({ machines }) {
  const engine = machines?.scoring_engine
  const red = machines?.red01
  const template = machines?.engine_template
  const rows = [
    engine && { ...engine, role: 'Scoring engine', ips: [engine.mgmt_ip, ...(engine.team_ips || [])].filter(Boolean) },
    red && { ...red, role: 'red01', ips: [red.ip].filter(Boolean) },
    template && { ...template, role: 'Engine template', ips: [] },
    ...(machines?.goldens || []).map((golden) => ({ ...golden, role: 'Golden image', ips: [] })),
  ].filter(Boolean)
  if (!rows.length) return null
  const showNode = rows.some((row) => row.node)
  const showIps = rows.some((row) => row.ips.length)
  return (
    <Card className="overflow-hidden">
      <div className="px-4 py-3">
        <h3 className="text-sm font-semibold">Shared machines <span className="font-normal text-faint">{rows.length}</span></h3>
        <p className="mt-1 text-xs text-muted">Engine, red box, templates and golden images recorded for this workspace. Records may remain after teardown; presence and power have not been checked live.</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="border-t border-line text-[11px] font-semibold uppercase tracking-wider text-faint">
              <th className="px-4 py-2">Role</th>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">VMID</th>
              {showNode && <th className="px-4 py-2">Node</th>}
              {showIps && <th className="px-4 py-2">Recorded addresses</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((row, index) => (
              <tr key={`${row.role}-${row.vmid}-${index}`} className="text-sm">
                <td className="whitespace-nowrap px-4 py-2 text-muted">{row.role}</td>
                <td className="px-4 py-2 font-mono text-xs">{row.name || '—'}</td>
                <td className="px-4 py-2 font-mono text-xs tabular-nums text-muted">{row.vmid ?? '—'}</td>
                {showNode && <td className="px-4 py-2 font-mono text-xs text-muted">{row.node || '—'}</td>}
                {showIps && <td className="px-4 py-2 font-mono text-xs text-muted"><div className="flex flex-wrap gap-x-3 gap-y-1">{row.ips.length ? row.ips.map((ip, i) => <span key={`${ip}-${i}`}>{ip}</span>) : '—'}</div></td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

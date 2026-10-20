const IN_FLIGHT = new Set(['queued', 'running', 'pausing', 'paused'])

export const isOperating = (run) => !!run && IN_FLIGHT.has(run.state)
export const rangeTitle = (range) => range.name || range.comp_id
export const shortRangeId = (range) => range.deploy_run_id || range.id

export function infrastructureLabel(range) {
  if (range.state === 'retired') return 'Retired'
  if (range.has_recorded_vms) return 'Infrastructure recorded'
  if (range.state === 'teardown_failed') return 'Teardown incomplete'
  if ((range.active_runs || range.runs || []).some(isOperating)) return 'Provisioning / operating'
  return 'No infrastructure recorded'
}

export function rangeIssues(range) {
  const issues = []
  if (range.pending_requests) issues.push({ kind: 'requests', message: `${range.pending_requests} reset request${range.pending_requests === 1 ? '' : 's'} awaiting review` })
  for (const item of range.degradations || []) {
    const message = typeof item === 'string' ? item : item.message || item.reason || item.detail || JSON.stringify(item)
    issues.push({ kind: 'degradation', message })
  }
  const destroy = range.latest_destroy || range.runs?.find((run) => run.action === 'destroy')
  if (destroy && ['failed', 'cancelled'].includes(destroy.state) && range.state !== 'retired') {
    issues.push({ kind: 'teardown', message: 'Teardown did not complete. Review its output before retrying.', run_id: destroy.id })
  } else if (range.state === 'teardown_failed') {
    issues.push({ kind: 'teardown', message: 'Teardown is incomplete; resources may remain.' })
  }
  const latest = range.latest_run || range.runs?.[0]
  if (latest?.state === 'failed' && latest.id !== destroy?.id) {
    issues.push({ kind: 'operation', message: `${latest.action} failed${latest.returncode != null ? ` (exit ${latest.returncode})` : ''}.`, run_id: latest.id })
  }
  return issues
}

const ACTION_NAME = { deploy: 'Deploying', scrim: 'Scrim running', verify: 'Verifying', destroy: 'Tearing down', redeploy: 'Resetting boxes', delete_team: 'Removing a team', start: 'Starting competition', freeze: 'Pausing scoring', resume: 'Resuming scoring', end: 'Ending competition' }

// The board's cached engine observation (scoring_cache): what a card's scoring
// chip says. Null when there is nothing to show — no poll, or the range is retired.
export function scoringChip(range) {
  const scoring = range.scoring
  if (!scoring || range.state === 'retired') return null
  if (scoring.state === 'running') return { tone: 'active', label: scoring.round > 0 ? `Scoring · round ${scoring.round}` : 'Scoring' }
  if (scoring.state === 'paused') return { tone: 'muted', label: 'Scoring paused' }
  if (scoring.state === 'ready') return { tone: 'ok', label: 'Ready to start' }
  if (scoring.state === 'ended') return { tone: 'muted', label: 'Competition ended' }
  return { tone: 'danger', label: 'Engine unreachable' }
}

// The one line a card leads with: a problem, else the live operation, else cached
// scoring, else pending requests, else the recorded state. Those recorded flags are
// workspace records, not an engine observation — scoring above them is the poll.
export function heroStatus(range) {
  const base = `/ranges/${range.id}`
  if (range.state === 'retired') return { kind: 'state', tone: 'muted', label: 'Retired', to: base }
  const issue = rangeIssues(range).find((item) => item.kind === 'teardown' || item.kind === 'operation')
  if (issue) return { kind: 'danger', tone: 'danger', label: issue.kind === 'teardown' ? 'Teardown incomplete' : 'Operation failed', issue, to: issue.run_id ? `${base}/activity?operation=${issue.run_id}` : `${base}/infrastructure` }
  const run = (range.active_runs || [])[0]
  if (run) return { kind: 'operation', tone: 'active', run, label: ACTION_NAME[run.action] || run.action, to: `${base}/activity?operation=${run.id}` }
  const scoring = scoringChip(range)
  if (scoring) return { kind: 'scoring', tone: scoring.tone, label: scoring.label, to: base }
  if (range.pending_requests) return { kind: 'attention', tone: 'danger', label: `${range.pending_requests} reset request${range.pending_requests === 1 ? '' : 's'} pending`, to: `${base}/teams` }
  if (range.state === 'failed') return { kind: 'danger', tone: 'danger', label: 'Deploy failed', to: base }
  const event = range.event || {}
  if (event.ended) return { kind: 'state', tone: 'muted', label: 'Competition ended', to: base }
  if (range.has_recorded_vms) return { kind: 'state', tone: 'ok', label: 'Deployed', detail: event.hold_start ? 'Deployed with the start held for an operator' : '', to: base }
  return { kind: 'state', tone: 'muted', label: infrastructureLabel(range), to: base }
}

// The one-line text status (comp page, deep links): the hero plus the run's held /
// queued qualifier and stage, which a card shows through its progress bar instead.
export function rangeStatus(range) {
  const hero = heroStatus(range)
  if (hero.kind !== 'operation') return hero
  const run = hero.run
  const held = run.state === 'paused' || run.state === 'pausing'
  return { ...hero, label: `${hero.label}${held ? ' · held' : run.state === 'queued' ? ' · queued' : ''}`, detail: run.stage || '' }
}

export function filterCounts(ranges) {
  const live = ranges.filter((range) => range.live)
  return {
    all: live.length,
    attention: live.filter((range) => rangeIssues(range).length).length,
    operating: live.filter((range) => (range.active_runs || []).length).length,
  }
}

export function filterRanges(ranges, query = '', filter = 'all') {
  const needle = query.trim().toLowerCase()
  return ranges.filter((range) => {
    if (!range.live) return false
    if (filter === 'attention' && !rangeIssues(range).length) return false
    if (filter === 'operating' && !(range.active_runs || []).length) return false
    return !needle || [range.name, range.comp_id, range.id, range.deploy_run_id, range.engine_ip, ...(range.nodes || [])].filter(Boolean).join(' ').toLowerCase().includes(needle)
  }).sort((a, b) => Number(!!rangeIssues(b).length) - Number(!!rangeIssues(a).length)
    || Number(!!b.active_runs?.length) - Number(!!a.active_runs?.length)
    || new Date(b.latest_run?.created_at || 0) - new Date(a.latest_run?.created_at || 0))
}

export function detailSummary(range) {
  const inventory = range.inventory || {}
  const runs = range.runs || []
  const nodes = new Set()
  for (const team of inventory.teams || []) for (const box of team.boxes || []) if (box.node) nodes.add(box.node)
  for (const machine of Object.values(inventory.machines || {}).flat()) if (machine?.node) nodes.add(machine.node)
  return {
    ...range,
    has_recorded_vms: range.has_recorded_vms ?? ((inventory.counts?.boxes || 0) > 0 || !!inventory.machines?.scoring_engine?.vmid),
    counts: inventory.counts || {},
    nodes: range.nodes || [...nodes].sort(),
    engine_ip: range.engine_ip || inventory.machines?.scoring_engine?.mgmt_ip,
    degradations: inventory.deploy?.degradations || [],
    pending_requests: (range.requests || []).filter((request) => request.status === 'pending').length,
    active_runs: runs.filter(isOperating),
    latest_run: runs[0],
    latest_destroy: runs.find((run) => run.action === 'destroy'),
  }
}

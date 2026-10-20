import { describe, expect, it } from 'vitest'
import { detailSummary, filterRanges, heroStatus, infrastructureLabel, rangeIssues, rangeStatus } from './rangeView'

const base = { id: 'range-a', comp_id: 'same', live: true, state: 'live', has_recorded_vms: true, active_runs: [], latest_run: { id: 'deploy-a', action: 'deploy', state: 'succeeded' } }

describe('range presentation', () => {
  it('does not call verify or paused operations a building range', () => {
    expect(infrastructureLabel({ ...base, state: 'building', active_runs: [{ action: 'verify', state: 'running' }] })).toBe('Infrastructure recorded')
  })
  it('prioritizes failed teardown even while recorded infrastructure remains', () => {
    const failed = { ...base, latest_destroy: { id: 'destroy-a', action: 'destroy', state: 'failed' } }
    expect(rangeIssues(failed)[0].kind).toBe('teardown')
    expect(filterRanges([base, { ...failed, id: 'range-b' }]).map((r) => r.id)).toEqual(['range-b', 'range-a'])
  })
  it('searches stable identity and placement, retaining same-competition ranges', () => {
    const other = { ...base, id: 'range-b', nodes: ['pmx2'], engine_ip: '10.0.0.8' }
    expect(filterRanges([base, other])).toHaveLength(2)
    expect(filterRanges([base, other], 'pmx2')).toEqual([other])
    expect(filterRanges([base, other], '10.0.0.8')).toEqual([other])
    expect(filterRanges([base], '', 'operating')).toEqual([])
  })
  it('uses recorded counts instead of operation selector/default teams', () => {
    const summary = detailSummary({ ...base, teams: 10, runs: [{ params: { teams: 2 }, state: 'succeeded' }], inventory: { counts: { teams: 1, boxes: 4 }, deploy: { degradations: ['partial seed'] }, teams: [] }, requests: [{ status: 'pending' }, { status: 'denied' }] })
    expect(summary.counts.teams).toBe(1)
    expect(summary.pending_requests).toBe(1)
    expect(rangeIssues(summary).map((item) => item.kind)).toEqual(['requests', 'degradation'])
  })
  it('never claims recorded VMs the inventory does not show', () => {
    const { has_recorded_vms, ...withoutFlag } = base
    expect(detailSummary({ ...withoutFlag, state: 'live', inventory: {} }).has_recorded_vms).toBe(false)
    expect(detailSummary({ ...withoutFlag, state: 'live', inventory: { counts: { boxes: 2 } } }).has_recorded_vms).toBe(true)
    expect(detailSummary({ ...withoutFlag, state: 'live', inventory: { machines: { scoring_engine: { vmid: 900 } } } }).has_recorded_vms).toBe(true)
  })
  it('picks one status: failures, then operations, then requests, then recorded event state', () => {
    const failed = { ...base, latest_destroy: { id: 'destroy-a', action: 'destroy', state: 'failed' }, pending_requests: 2, active_runs: [{ id: 'v', action: 'verify', state: 'running' }] }
    expect(rangeStatus(failed)).toMatchObject({ tone: 'danger', label: 'Teardown incomplete', to: '/ranges/range-a/activity?operation=destroy-a' })
    expect(rangeStatus({ ...base, pending_requests: 2, active_runs: [{ id: 'd', action: 'deploy', state: 'paused', stage: 'phase 4' }] })).toMatchObject({ label: 'Deploying · held', detail: 'phase 4' })
    expect(rangeStatus({ ...base, pending_requests: 2 })).toMatchObject({ label: '2 reset requests pending', to: '/ranges/range-a/teams' })
    expect(rangeStatus({ ...base, event: { ended: true } }).label).toBe('Competition ended')
    expect(rangeStatus(base).label).toBe('Deployed')
  })
  it('shows cached scoring above requests and recorded state, but below a live operation', () => {
    expect(heroStatus({ ...base, scoring: { state: 'running', round: 3, reachable: true }, pending_requests: 2 }).label).toBe('Scoring · round 3')
    expect(heroStatus({ ...base, scoring: { state: 'paused', round: 3, reachable: true } }).label).toBe('Scoring paused')
    expect(heroStatus({ ...base, scoring: { state: 'ready', reachable: true } }).label).toBe('Ready to start')
    expect(heroStatus({ ...base, scoring: { state: 'unknown', round: 0, reachable: false } }).label).toBe('Engine unreachable')
    expect(heroStatus(base).label).toBe('Deployed') // no cached observation: recorded state
    expect(heroStatus({ ...base, scoring: { state: 'running', round: 1, reachable: true }, active_runs: [{ id: 'r', action: 'redeploy', state: 'running' }] }).kind).toBe('operation')
  })
})

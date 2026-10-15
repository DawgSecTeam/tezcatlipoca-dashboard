import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Range from './Range'
import { api } from '../api'
import { useCompetitionData, useRangeData } from '../useRangeData'
vi.mock('../api', () => ({ api: { submitRun: vi.fn(), runEvents: vi.fn(), requests: vi.fn().mockResolvedValue([]), ranges: vi.fn().mockResolvedValue([]) } }))
vi.mock('../useRangeData', () => ({ useRangeData: vi.fn(), useCompetitionData: vi.fn() }))
vi.mock('../components/CompetitionPanel', () => ({ default: ({ section }) => <section><h2>{section === 'access' ? 'Access' : 'Competition'}</h2></section> }))
vi.mock('../components/RunTeams', () => ({ default: () => <div>Team inventory</div>, ResetRequests: () => <section><h3>Reset requests</h3></section> }))
vi.mock('../components/RunInfra', () => ({ default: () => <div>Technical inventory</div> }))
vi.mock('../components/runui', () => ({ ACTION_LABEL: {}, ResetBoxModal: () => null, StageStrip: () => null, StatePill: ({ state }) => <span>{state}</span>, useRunActions: () => ({}), useRunEvents: () => ({ live: false }), useRunLog: () => ({}) }))
vi.mock('./Run', () => ({ default: () => null, Evidence: () => <div>Range evidence</div>, Output: () => <div>Raw output viewer</div>, Timeline: () => null }))

const fixture = {
  id: 'range-one', key: '/synthetic/one/comp', name: 'One competition', comp_id: 'one', state: 'live', live: true,
  deploy_run_id: 'deploy-one', engine_ref: 'origin-ref', has_recorded_vms: true,
  requests: [{ status: 'pending' }],
  runs: [{ id: 'old-reset', action: 'redeploy', state: 'succeeded', engine_ref: 'other-ref', params: { comp_dir: '/synthetic/other/comp' } }, { id: 'deploy-one', action: 'deploy', state: 'succeeded', engine_ref: 'origin-ref' }],
  inventory: { counts: { teams: 1, boxes: 3 }, teams: [{ key: 'team1', subnet: '10.1.0.0/24', boxes: [{}, {}, {}] }], deploy: { degradations: ['web01 lagged'] } },
}
function open(path = '/ranges/range-one') {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/ranges/:rangeId/*" element={<Range />} /></Routes></MemoryRouter>)
}
afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
  useRangeData.mockReturnValue({ data: fixture, error: null, updated: Date.now(), refresh: vi.fn() })
  useCompetitionData.mockReturnValue({ view: { state: 'ready' }, error: null })
  api.runEvents.mockResolvedValue({ events: [] })
  api.submitRun.mockResolvedValue({ id: 'verify-new', action: 'verify' })
})

it('orders attention, pending requests and competition first, with access and facts beside them', () => {
  open()
  const headings = screen.getAllByRole('heading').map((element) => element.textContent)
  expect(headings.indexOf('Attention needed')).toBeLessThan(headings.indexOf('Reset requests'))
  expect(headings.indexOf('Reset requests')).toBeLessThan(headings.indexOf('Competition'))
  expect(headings).toEqual(expect.arrayContaining(['Access', 'Range', 'Recent activity']))
  expect(headings.find((h) => h.startsWith('Teams'))).toBeTruthy()
  expect(screen.getByText('1 pending')).toBeTruthy()
  expect(screen.getByText('Ready to start')).toBeTruthy()
  expect(screen.queryByText('Raw output viewer')).toBeNull()
})

it('surfaces an in-flight operation with its controls on the overview', () => {
  useRangeData.mockReturnValue({ data: { ...fixture, state: 'building', runs: [{ id: 'deploy-one', action: 'deploy', state: 'paused', stage: 'phase 4/8' }] }, error: null, refresh: vi.fn() })
  open()
  expect(screen.getByRole('heading', { name: 'deploy in progress' })).toBeTruthy()
  expect(screen.getAllByRole('link', { name: 'View output' }).some((link) => link.getAttribute('href').endsWith('operation=deploy-one'))).toBe(true)
})

it('keeps controls tied to range origin even when an old operation is selected', async () => {
  open('/ranges/range-one/activity?operation=old-reset')
  await userEvent.click(screen.getByRole('button', { name: 'Verify range' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledWith({ comp_id: 'one', engine_ref: 'origin-ref', action: 'verify', params: { comp_dir: '/synthetic/one/comp' } }))
  expect(screen.getByText('verify-new')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'View operation' }).getAttribute('href')).toContain('operation=verify-new')
})

it('does not offer enabled range controls for retired ranges', () => {
  useRangeData.mockReturnValue({ data: { ...fixture, state: 'retired' }, error: null, refresh: vi.fn() })
  open()
  expect(screen.getByRole('button', { name: 'Verify range' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Reset a box' }).disabled).toBe(true)
})

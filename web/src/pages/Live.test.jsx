import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import Live from './Live'
import { api } from '../api'
vi.mock('../api', () => ({ api: { rangeBoard: vi.fn(), comps: vi.fn(), requests: vi.fn().mockResolvedValue([]), ranges: vi.fn().mockResolvedValue([]) } }))
vi.mock('../components/runui', () => ({ ACTION_LABEL: { verify: 'Verify', deploy: 'Deploy' }, NewRunModal: () => null, stageProgress: (run) => ({ total: 1, idx: 0, label: run.stage || '' }) }))
afterEach(cleanup)

it('leads each card with its status: requests, live operation progress, then scoring', async () => {
  api.comps.mockResolvedValue([])
  api.rangeBoard.mockResolvedValue([
    { id: 'range-a', deploy_run_id: 'deploy-one', comp_id: 'same', name: 'Shared competition', live: true, state: 'live', has_recorded_vms: true, counts: { teams: 1, boxes: 3 }, nodes: ['node-a'], active_runs: [], pending_requests: 1, deployed_at: Date.now() / 1000 - 7200, links: { scoreboard: 'http://192.0.2.10' }, latest_run: { id: 'verify-one', action: 'verify', state: 'succeeded' } },
    { id: 'range-b', deploy_run_id: 'deploy-two', comp_id: 'same', name: 'Shared competition', live: true, state: 'building', has_recorded_vms: true, counts: { teams: 2, boxes: 6 }, nodes: ['node-b'], active_runs: [{ id: 'verify-two', action: 'verify', state: 'running', stage: 'Check services' }] },
    { id: 'range-c', deploy_run_id: 'deploy-three', comp_id: 'other', name: 'Drill', live: true, state: 'live', has_recorded_vms: true, counts: { teams: 4, boxes: 12 }, nodes: [], active_runs: [], scoring: { state: 'running', round: 12, reachable: true }, latest_run: { id: 'deploy-three', action: 'deploy', state: 'succeeded' } },
  ])
  render(<MemoryRouter><Live /></MemoryRouter>)
  const requests = await screen.findByRole('link', { name: '1 reset request pending' })
  expect(requests.getAttribute('href')).toBe('/ranges/range-a/teams')
  expect(screen.getByRole('link', { name: /^Verifying/ }).getAttribute('href')).toBe('/ranges/range-b/activity?operation=verify-two')
  expect(screen.getByText('Check services')).toBeTruthy()
  expect(screen.getByText('Scoring · round 12')).toBeTruthy()
  expect(screen.getByText('deployed 2h ago')).toBeTruthy()
  // The card's way in: tools and credentials live on the range page, so the board
  // offers the open button, not the external links themselves.
  expect(screen.getAllByRole('link', { name: /Open range/ }).map((link) => link.getAttribute('href'))).toContain('/ranges/range-b')
  expect(screen.queryByRole('link', { name: /Scoreboard/ })).toBeNull()
  expect(screen.getByRole('button', { name: 'Needs attention' }).textContent).toContain('1')
  await userEvent.click(screen.getByRole('button', { name: 'Operating' }))
  await waitFor(() => expect(screen.queryByRole('link', { name: '1 reset request pending' })).toBeNull())
  expect(screen.getByRole('link', { name: /^Verifying/ })).toBeTruthy()
})

it('identifies each range and links no engine that is not there', async () => {
  api.comps.mockResolvedValue([])
  api.rangeBoard.mockResolvedValue([
    { id: 'range-x', key: '/w/x', deploy_run_id: 'deploy-x', comp_id: 'same', name: 'Shared competition', live: true, state: 'teardown_failed', has_recorded_vms: false, counts: { teams: 2, boxes: 0 }, nodes: [], engine_ip: '192.0.2.37', active_runs: [], links: {}, deployed_at: Date.now() / 1000 - 300, latest_destroy: { id: 'down-x', action: 'destroy', state: 'failed', returncode: 1 }, latest_run: { id: 'down-x', action: 'destroy', state: 'failed' } },
    { id: 'range-y', deploy_run_id: 'deploy-y', comp_id: 'same2', name: 'Dark range', live: true, state: 'live', has_recorded_vms: true, counts: { teams: 1, boxes: 2 }, nodes: [], active_runs: [], scoring: { state: 'unknown', round: 0, reachable: false }, latest_run: { id: 'deploy-y', action: 'deploy', state: 'succeeded' } },
  ])
  render(<MemoryRouter><Live /></MemoryRouter>)
  // Two ranges of one competition share a Compfile name: the deploy run names the range.
  expect(await screen.findByText('deploy-x')).toBeTruthy()
  expect(screen.getByText('192.0.2.37')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Teardown incomplete' }).getAttribute('href')).toBe('/ranges/range-x/activity?operation=down-x')
  expect(screen.getByRole('link', { name: 'Engine unreachable' }).getAttribute('href')).toBe('/ranges/range-y')
  expect(screen.queryByRole('link', { name: /Scoreboard/ })).toBeNull()
})

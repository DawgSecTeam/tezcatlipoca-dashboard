import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import Live from './Live'
import { api } from '../api'
vi.mock('../api', () => ({ api: { rangeBoard: vi.fn(), comps: vi.fn(), requests: vi.fn().mockResolvedValue([]), ranges: vi.fn().mockResolvedValue([]) } }))
vi.mock('../components/runui', () => ({ ACTION_LABEL: { verify: 'Verify', deploy: 'Deploy' }, NewRunModal: () => null }))
afterEach(cleanup)

it('leads with one status per range, quick access links and counted filters', async () => {
  api.comps.mockResolvedValue([])
  api.rangeBoard.mockResolvedValue([
    { id: 'range-a', deploy_run_id: 'deploy-one', comp_id: 'same', name: 'Shared competition', live: true, state: 'live', has_recorded_vms: true, counts: { teams: 1, boxes: 3 }, nodes: ['node-a'], active_runs: [], pending_requests: 1, deployed_at: Date.now() / 1000 - 7200, links: { scoreboard: 'http://192.0.2.10', admin: 'http://192.0.2.10/admin', injects: 'http://192.0.2.10/injects' }, latest_run: { id: 'verify-one', action: 'verify', state: 'succeeded' } },
    { id: 'range-b', deploy_run_id: 'deploy-two', comp_id: 'same', name: 'Shared competition', live: true, state: 'building', has_recorded_vms: true, counts: { teams: 2, boxes: 6 }, nodes: ['node-b'], active_runs: [{ id: 'verify-two', action: 'verify', state: 'running', stage: 'Check services' }] },
  ])
  render(<MemoryRouter><Live /></MemoryRouter>)
  const requests = await screen.findByRole('link', { name: '1 reset request pending' })
  expect(requests.getAttribute('href')).toBe('/ranges/range-a/teams')
  expect(screen.getByRole('link', { name: 'Verifying' }).getAttribute('href')).toBe('/ranges/range-b/activity?operation=verify-two')
  expect(screen.getByText('Check services')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Scoreboard ↗' }).getAttribute('href')).toBe('http://192.0.2.10')
  expect(screen.queryByRole('link', { name: /Injects/ })).toBeNull()
  expect(screen.getByText('deployed 2h ago')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Needs attention' }).textContent).toContain('1')
  await userEvent.click(screen.getByRole('button', { name: 'Operating' }))
  await waitFor(() => expect(screen.queryByRole('link', { name: '1 reset request pending' })).toBeNull())
  expect(screen.getByRole('link', { name: 'Verifying' })).toBeTruthy()
})

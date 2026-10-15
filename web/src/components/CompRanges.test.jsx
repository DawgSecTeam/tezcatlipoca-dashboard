import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import CompRanges from './CompRanges'
import { api } from '../api'

vi.mock('../api', () => ({ api: { rangeBoard: vi.fn() } }))
afterEach(cleanup)

const live = (comp_id, id, name) => ({
  id, comp_id, name, state: 'live', live: true, has_recorded_vms: true,
  deploy_run_id: `run-${id}`, deployed_at: Date.now() / 1000 - 120,
  latest_run: { id: `run-${id}`, action: 'deploy', state: 'succeeded' },
})

it('lists only this competition’s ranges, with their own identity', async () => {
  api.rangeBoard.mockResolvedValue([live('comp-a', 'range-a1', 'Shared name'), live('comp-b', 'range-b1', 'Other comp')])
  render(<MemoryRouter><CompRanges compId="comp-a" /></MemoryRouter>)
  expect(await screen.findByText('1 range of this competition')).toBeTruthy()
  expect(screen.getByRole('link').getAttribute('href')).toBe('/ranges/range-a1')
  expect(screen.getByText('run-range-a1')).toBeTruthy()
  expect(screen.queryByText('Other comp')).toBeNull()
})

it('says when a competition has no range instead of implying one', async () => {
  api.rangeBoard.mockResolvedValue([{ ...live('comp-b', 'range-b1', 'Other comp'), state: 'retired', live: false }])
  render(<MemoryRouter><CompRanges compId="comp-a" /></MemoryRouter>)
  expect(await screen.findByText(/No range of this competition has been deployed/)).toBeTruthy()
})

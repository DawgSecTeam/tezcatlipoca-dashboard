import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Range from './Range'
import { ConfirmProvider } from '../components/kit'
import { api } from '../api'
import { useCompetitionData, useRangeData, useRangeDiagnosis } from '../useRangeData'
vi.mock('../api', () => ({ api: { submitRun: vi.fn(), run: vi.fn(), runEvents: vi.fn(), requests: vi.fn().mockResolvedValue([]), ranges: vi.fn().mockResolvedValue([]) } }))
vi.mock('../useRangeData', () => ({ useRangeData: vi.fn(), useCompetitionData: vi.fn(), useRangeDiagnosis: vi.fn() }))
vi.mock('../components/RunTeams', () => ({ default: () => <div>Team inventory</div>, ResetRequests: () => <section><h3>Reset requests</h3></section> }))
vi.mock('../components/RunInfra', () => ({ default: () => <div>Technical inventory</div> }))
vi.mock('../components/runui', () => ({ ACTION_LABEL: {}, ResetBoxModal: () => null, StageStrip: () => null, StatePill: ({ state }) => <span>{state}</span>, useRunActions: () => ({}), useRunEvents: () => ({ live: false }), useRunLog: () => ({}), LogTail: () => null }))
vi.mock('./Run', () => ({ default: () => null, Evidence: () => <div>Range evidence</div>, Output: () => <div>Raw output viewer</div>, Timeline: () => null }))

const fixture = {
  id: 'range-one', key: '/synthetic/one/comp', name: 'One competition', comp_id: 'one', state: 'live', live: true,
  deploy_run_id: 'deploy-one', engine_ref: 'origin-ref', has_recorded_vms: true,
  requests: [{ status: 'pending' }],
  runs: [{ id: 'old-reset', action: 'redeploy', state: 'succeeded', engine_ref: 'other-ref', params: { comp_dir: '/synthetic/other/comp' } }, { id: 'deploy-one', action: 'deploy', state: 'succeeded', engine_ref: 'origin-ref' }],
  inventory: { counts: { teams: 1, boxes: 3 }, teams: [{ key: 'team1', subnet: '10.1.0.0/24', boxes: [{}, {}, {}] }], deploy: { degradations: ['web01 lagged'] } },
}
function open(path = '/ranges/range-one') {
  return render(<ConfirmProvider><MemoryRouter initialEntries={[path]}><Routes><Route path="/ranges/:rangeId/*" element={<Range />} /></Routes></MemoryRouter></ConfirmProvider>)
}
afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
  useRangeData.mockReturnValue({ data: fixture, error: null, updated: Date.now(), refresh: vi.fn() })
  useCompetitionData.mockReturnValue({ view: { state: 'ready', links: { scoreboard: 'http://192.0.2.10' }, creds: { admin: { user: 'admin', password: 'ADMIN-SECRET' } } }, error: null })
  useRangeDiagnosis.mockReturnValue(null)
  api.runEvents.mockResolvedValue({ events: [] })
  api.run.mockResolvedValue({})
  api.submitRun.mockResolvedValue({ id: 'verify-new', action: 'verify' })
})

it('puts scoring state, controls, tools and credentials in the command bar, then attention and teams', async () => {
  open()
  expect(screen.getByRole('region', { name: 'Scoring' })).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Tools and credentials' })).toBeTruthy()
  expect(screen.getByText('Ready to start')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Start the competition' })).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Scoreboard ↗' }).getAttribute('href')).toBe('http://192.0.2.10')
  await userEvent.click(screen.getByRole('button', { name: 'Reveal admin password' }))
  expect(screen.getByText('ADMIN-SECRET')).toBeTruthy()
  const headings = screen.getAllByRole('heading').map((element) => element.textContent)
  expect(headings.indexOf('Attention needed')).toBeLessThan(headings.indexOf('Reset requests'))
  expect(headings.indexOf('Reset requests')).toBeLessThan(headings.findIndex((h) => h.startsWith('Teams')))
  expect(headings).toEqual(expect.arrayContaining(['Range', 'Recent activity']))
  expect(screen.getByText('1 pending')).toBeTruthy()
  expect(screen.queryByText('Raw output viewer')).toBeNull()
})

it('renders the engine round record as its round number', () => {
  useCompetitionData.mockReturnValue({ view: { state: 'running', engine: { last_round: { ID: 3, StartTime: '2026-10-18T00:00:00Z', Checks: null, SLAs: null } } }, error: null })
  open()
  expect(screen.getByText(/round 3/)).toBeTruthy()
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
  expect(screen.queryByRole('button', { name: 'More range actions' })).toBeNull()
  expect(screen.getByText(/Competition archived/)).toBeTruthy()
})

it('offers teardown-only on a retired range that still records kept templates', async () => {
  useRangeData.mockReturnValue({
    data: {
      ...fixture, state: 'retired', live: false, has_recorded_vms: false,
      reclaimable_templates: true,
      kept_templates: [{ kind: 'engine_template', vmid: 3140 }, { kind: 'golden', vmid: 3150 }],
    }, error: null, refresh: vi.fn(),
  })
  open()
  expect(screen.getByText(/still records this competition's kept templates/)).toBeTruthy()
  expect(screen.getByText(/3140, 3150/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Verify range' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Reset a box' }).disabled).toBe(true)
  await userEvent.click(screen.getByRole('button', { name: 'More range actions' }))
  await userEvent.click(screen.getByRole('button', { name: /Destroy this range/ }))
  await userEvent.click(screen.getByRole('button', { name: 'Destroy this range' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledWith({
    comp_id: 'one', action: 'destroy',
    params: { comp_dir: '/synthetic/one/comp' },
  }))
})

it('does not call a range with no infrastructure engine-unreachable', () => {
  useRangeData.mockReturnValue({
    data: { ...fixture, state: 'teardown_failed', has_recorded_vms: false, inventory: { ...fixture.inventory, counts: { teams: 1, boxes: 0 }, teams: [] } },
    error: null, refresh: vi.fn(),
  })
  useCompetitionData.mockReturnValue({ view: { state: 'unknown', reachable: false, links: {}, creds: {} }, error: null })
  open()
  expect(screen.getByText('No engine recorded')).toBeTruthy()
  expect(screen.queryByText('Engine unreachable')).toBeNull()
  expect(screen.getAllByText(/records no infrastructure/).length).toBeGreaterThan(0)
  expect(screen.queryByRole('button', { name: 'Start the competition' })).toBeNull()
})

it('shows the selected operation failure from the run detail endpoint', async () => {
  useRangeData.mockReturnValue({
    data: { ...fixture, runs: [{ id: 'bad-run', action: 'scrim', state: 'failed', created_at: Date.now() / 1000 }] },
    error: null, refresh: vi.fn(),
  })
  api.run.mockResolvedValue({ id: 'bad-run', error: 'RuntimeError: compose up failed' })
  open('/ranges/range-one/activity?operation=bad-run')
  expect(await screen.findByText(/RuntimeError: compose up failed/)).toBeTruthy()
})

it('closes the range actions menu on Escape instead of leaving it over the page', async () => {
  open()
  await userEvent.click(screen.getByRole('button', { name: 'More range actions' }))
  expect(screen.getByRole('button', { name: /Destroy this range/ })).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('button', { name: /Destroy this range/ })).toBeNull())
})

it('leads with the server diagnosis when the range has a real problem', () => {
  useRangeDiagnosis.mockReturnValue({
    subject: { kind: 'range', id: 'range-one', comp_id: 'one', state: 'failed' },
    severity: 'error',
    headline: 'one range failed to deploy: A Python dependency is missing in the service venv',
    findings: [{
      code: 'missing_dependency', severity: 'error',
      title: 'A Python dependency is missing in the service venv',
      detail: 'The drivers run under the service interpreter.',
      advice: 'Install requirements.txt in that venv, then submit again.',
      evidence: [{ source: 'run-log', line: 9, offset: 0, quote: "ModuleNotFoundError: No module named 'toml'" }],
    }],
    facts: { returncode: 1, duration_s: 30 },
    as_of: Date.now() / 1000,
  })
  open()
  expect(screen.getByText(/failed to deploy/)).toBeTruthy()
  expect(screen.getByText(/No module named 'toml'/)).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Attention needed' })).toBeNull()
})

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CompetitionPanel from './CompetitionPanel'
import RunTeams from './RunTeams'
import RunInfra from './RunInfra'
import Bell from './Bell'
import { api } from '../api'

const mocks = vi.hoisted(() => ({ confirm: vi.fn(), toast: vi.fn(), modal: vi.fn() }))
vi.mock('../api', () => ({
  api: { ranges: vi.fn(), competition: vi.fn(), requests: vi.fn(), submitRun: vi.fn(), approveRequest: vi.fn(), denyRequest: vi.fn() },
  kindOf: () => 'linux',
}))
vi.mock('./kit', async (original) => ({ ...await original(), useToast: () => mocks.toast, useConfirm: () => mocks.confirm }))
vi.mock('./runui', () => ({
  REDEPLOY_MODES: ['reset', 'rebuild', 'rollback-ready', 'rollback-base', 'reconfigure', 'resync', 'engine-recovery'],
  DEPLOY_STAGES: ['Cleanup'],
  ResetBoxModal: (props) => { mocks.modal(props); return props.open ? <div>Recovery modal</div> : null },
}))

const range = { id: 'stable-a', key: '/recorded/a/comp', comp_id: 'comp-a', engine_ref: 'origin-ref', deploy_run_id: 'deploy-a', state: 'live' }
const view = { range: range.key, comp_id: range.comp_id, state: 'ready', reachable: true, creds: { admin: { user: 'admin', password: 'super-secret-123' } } }
const inventory = {
  range,
  teams: [{ key: 'team1', identifier: '104', subnet: '192.168.104.0/24', boxes: [{ name: 'web01', vmid: 801, ip: '192.168.104.2', node: 'node-a', started: true }] }],
  machines: {},
}
const run = { id: 'followup-a', comp_id: 'wrong-comp', engine_ref: 'wrong-ref', run_dir: '/wrong-dir', params: {} }
const request = { id: 42, range_key: range.key, comp_id: range.comp_id, team: 'team1', box: 'web01', status: 'pending' }
const open = (node) => render(<MemoryRouter>{node}</MemoryRouter>)

beforeEach(() => {
  vi.clearAllMocks()
  api.ranges.mockResolvedValue([range])
  api.requests.mockResolvedValue([])
  api.submitRun.mockResolvedValue({ id: 'created-a', comp_id: range.comp_id })
  mocks.confirm.mockResolvedValue(true)
})
afterEach(cleanup)

it('submits exact competition/ref/workspace and forwards created operation', async () => {
  let finish
  api.submitRun.mockReturnValue(new Promise((resolve) => { finish = resolve }))
  const submitted = vi.fn(), refresh = vi.fn()
  open(<CompetitionPanel rangeKey={range.key} compId={range.comp_id} engineRef={range.engine_ref} view={view} onSubmitted={submitted} onRefresh={refresh} />)
  expect(api.competition).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Start the competition' }))
  expect(screen.getByRole('button', { name: 'Start the competition' }).disabled).toBe(true)
  expect(screen.getByText('Submitting start operation…')).toBeTruthy()
  expect(api.submitRun).toHaveBeenCalledWith({ action: 'start', comp_id: range.comp_id, engine_ref: 'origin-ref', params: { comp_dir: range.key } })
  await act(async () => finish({ id: 'created-a', comp_id: range.comp_id }))
  expect(submitted).toHaveBeenCalledWith({ id: 'created-a', comp_id: range.comp_id })
  expect(screen.getByRole('link', { name: 'created-a' }).getAttribute('href')).toBe('/c/comp-a/runs/created-a')
})

it('masks, reveals and hides credentials without showing workspace diagnostics', () => {
  open(<CompetitionPanel rangeKey={range.key} compId={range.comp_id} engineRef={range.engine_ref} view={view} />)
  expect(screen.queryByText('super-secret-123')).toBeNull()
  expect(screen.queryByText(range.key)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Reveal admin password' }))
  expect(screen.getByText('super-secret-123')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Hide admin password' }))
  expect(screen.queryByText('super-secret-123')).toBeNull()
})

it('keeps refresh errors visible with stale data and disables lifecycle controls', () => {
  open(<CompetitionPanel rangeKey={range.key} compId={range.comp_id} engineRef={range.engine_ref} view={view} error={new Error('Refresh unavailable')} />)
  expect(screen.getByText('Refresh unavailable')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Start the competition' }).disabled).toBe(true)
})

it('does not display another workspace credentials or actions', () => {
  open(<CompetitionPanel rangeKey={range.key} compId={range.comp_id} engineRef={range.engine_ref} view={{ ...view, range: '/other/comp' }} />)
  expect(screen.getByText(/belongs to another workspace/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Reveal admin password' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Start the competition' })).toBeNull()
})

it('legacy polling resolves origin identity from the exact range record', async () => {
  api.competition.mockResolvedValue(view)
  api.ranges.mockResolvedValue([{ ...range, engine_ref: undefined, runs: [{ id: 'followup', engine_ref: 'wrong-ref', action: 'verify' }, { id: 'deploy-a', action: 'deploy', engine_ref: 'origin-ref' }] }])
  open(<CompetitionPanel rangeKey={range.key} />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start the competition' }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Start the competition' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledWith(expect.objectContaining({ comp_id: range.comp_id, engine_ref: 'origin-ref', params: { comp_dir: range.key } })))
})

it('normalizes reset approval callback to its operation id, not request id', async () => {
  const submitted = vi.fn()
  api.requests.mockResolvedValue([request])
  api.approveRequest.mockResolvedValue({ ...request, approved_run: 'reset-run-a' })
  open(<RunTeams run={run} inventory={inventory} onSubmitted={submitted} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
  await waitFor(() => expect(submitted).toHaveBeenCalledWith({ id: 'reset-run-a', comp_id: range.comp_id, action: 'redeploy' }))
})

it('passes inventory range and synthetic run identity to reset modal', () => {
  open(<RunTeams run={run} inventory={inventory} />)
  fireEvent.click(screen.getByRole('button', { name: 'Reset team1 web01' }))
  const props = mocks.modal.mock.calls.at(-1)[0]
  expect(props.range).toEqual(range)
  expect(props.run).toMatchObject({ comp_id: range.comp_id, engine_ref: 'origin-ref', params: { comp_dir: range.key, _destroy_comp_dir: range.key } })
  expect(props.initial).toEqual({ box: 'web01', team: '104', mode: 'reset' })
  expect(screen.queryByText('running')).toBeNull()
  expect(screen.queryByText('Definition details')).toBeNull()
})

it('searches recorded box addresses without inventing definition inventory', () => {
  open(<RunTeams inventory={inventory} />)
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'missing' } })
  expect(screen.getByText('No matching teams or boxes')).toBeTruthy()
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '801' } })
  expect(screen.getByText('web01')).toBeTruthy()
})

it('disables retired inventory actions and removes definitions fallback', () => {
  const { unmount } = open(<RunTeams inventory={{ ...inventory, range: { ...range, state: 'retired' } }} />)
  expect(screen.getByRole('button', { name: 'Reset team1 web01' }).disabled).toBe(true)
  unmount()
  open(<RunTeams comp={{ boxes: [{ name: 'not-deployed' }] }} />)
  expect(screen.getByText('No recorded inventory available')).toBeTruthy()
  expect(screen.queryByText('not-deployed')).toBeNull()
})

it('submits removal against inventory identity after range confirmation', async () => {
  const submitted = vi.fn()
  open(<RunTeams run={run} inventory={inventory} onSubmitted={submitted} />)
  fireEvent.click(screen.getByRole('button', { name: 'Remove team…' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledWith({ comp_id: range.comp_id, engine_ref: 'origin-ref', action: 'delete_team', params: { team: 'team1', comp_dir: range.key } }))
  expect(mocks.confirm.mock.calls[0][0].body).toBeTruthy()
})

it('renders workspace identity and teardown completion without live-power claims', () => {
  open(<RunInfra inventory={{ ...inventory, teardown: { run_id: 'destroy-a', done: ['terraform'], red01_done: false } }} />)
  expect(screen.getByText(range.key)).toBeTruthy()
  expect(screen.getByText('Terraform-managed resources · completed')).toBeTruthy()
  expect(screen.getByText(/Red box teardown has not been recorded as complete/)).toBeTruthy()
  expect(screen.getByRole('link', { name: 'destroy-a' }).getAttribute('href')).toBe('/ranges/stable-a/activity?operation=destroy-a')
})

it('splits lifecycle and access sections from one view and links operations inside the range', async () => {
  const { unmount } = open(<CompetitionPanel rangeKey={range.key} rangeId={range.id} compId={range.comp_id} engineRef={range.engine_ref} section="lifecycle" view={{ ...view, links: { scoreboard: 'http://192.0.2.1' } }} />)
  expect(screen.getByRole('button', { name: 'Start the competition' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Reveal admin password' })).toBeNull()
  expect(screen.queryByRole('link', { name: /Scoreboard/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Start the competition' }))
  expect((await screen.findByRole('link', { name: 'created-a' })).getAttribute('href')).toBe('/ranges/stable-a/activity?operation=created-a')
  unmount()
  open(<CompetitionPanel rangeKey={range.key} compId={range.comp_id} engineRef={range.engine_ref} section="access" view={{ ...view, links: { scoreboard: 'http://192.0.2.1' } }} />)
  expect(screen.queryByRole('button', { name: 'Start the competition' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Reveal admin password' })).toBeTruthy()
  expect(screen.getByRole('link', { name: /Scoreboard/ }).getAttribute('href')).toBe('http://192.0.2.1')
})

it('keeps shared machines off the teams view when the range page asks', () => {
  open(<RunTeams inventory={{ ...inventory, machines: { scoring_engine: { name: 'engine', vmid: 900 } } }} showMachines={false} />)
  expect(screen.queryByText('Shared machines')).toBeNull()
})

it('resolves bell range links via workspace record id and shows approved operation', async () => {
  api.requests.mockResolvedValue([{ ...request, approved_run: 'reset-a' }])
  api.ranges.mockResolvedValue([{ ...range, id: 'proper-stable-id' }, { key: '/other/comp', comp_id: range.comp_id, id: 'other-stable-id' }])
  open(<Bell />)
  fireEvent.click(screen.getByRole('button', { name: 'Reset requests' }))
  const link = await screen.findByRole('link', { name: 'Range teams' })
  expect(link.getAttribute('href')).toBe('/ranges/proper-stable-id/teams')
  expect(screen.getByRole('link', { name: 'Reset operation' }).getAttribute('href')).toBe('/c/comp-a/runs/reset-a')
})

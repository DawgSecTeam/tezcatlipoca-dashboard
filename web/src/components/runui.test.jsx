import React from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, streamRunEvents } from '../api'
import { Evidence, Timeline } from '../pages/Run'
import { EventList, NewRunModal, ResetBoxModal, useRunActions, useRunEvents, useRunLog } from './runui'

const mocks = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn() }))
vi.mock('../api', () => ({
  api: { submitRun: vi.fn(), comp: vi.fn(), nodes: vi.fn(), runEvents: vi.fn(), runLog: vi.fn(), runArtifacts: vi.fn(), runArtifactFile: vi.fn() },
  streamRunEvents: vi.fn(),
}))
vi.mock('./kit', async (original) => ({
  ...await original(), useToast: () => mocks.toast, useConfirm: () => mocks.confirm,
}))

const run = { id: 'run-a', comp_id: 'comp-a', engine_ref: 'run-ref', action: 'deploy', run_dir: '/runs/a', params: {} }
const range = { key: '/selected/workspace', comp_id: 'comp-a', engine_ref: 'deploy-ref' }
const created = { id: 'created-followup', action: 'verify' }
const event = (seq, extra = {}) => ({ seq, kind: 'lifecycle', ts: 1800000000 + seq, state: 'running', ...extra })

beforeEach(() => {
  vi.resetAllMocks()
  api.submitRun.mockResolvedValue(created)
  api.comp.mockResolvedValue({ boxes: [{ name: 'web01' }] })
  api.runEvents.mockResolvedValue({ next: 0, events: [] })
  mocks.confirm.mockResolvedValue(true)
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('range-scoped run actions', () => {
  it('returns the created run, announces its ID and passes it to onChanged', async () => {
    const changed = vi.fn()
    const { result } = renderHook(() => useRunActions(run, changed, range))
    let submitted
    await act(async () => { submitted = await result.current.submit({ action: 'verify', params: { comp_dir: '/wrong' } }) })
    expect(api.submitRun).toHaveBeenCalledWith({
      comp_id: 'comp-a', engine_ref: 'deploy-ref', action: 'verify', params: { comp_dir: range.key },
    })
    expect(submitted).toBe(created)
    expect(changed).toHaveBeenCalledWith(created)
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining(created.id))
  })

  it.each([
    [{ comp_dir: '/synthetic/comp', _destroy_comp_dir: '/older/comp' }, '/synthetic/comp'],
    [{ _destroy_comp_dir: '/acted-on/comp' }, '/acted-on/comp'],
    [{}, '/runs/a/comp'],
  ])('honors synthetic and persisted workspace fallbacks', async (params, workspace) => {
    const { result } = renderHook(() => useRunActions({ ...run, params }, vi.fn()))
    await act(async () => { await result.current.submit({ action: 'destroy' }) })
    expect(api.submitRun.mock.calls[0][0].params.comp_dir).toBe(workspace)
  })

  it('never submits when the workspace cannot be identified', async () => {
    const changed = vi.fn()
    const { result } = renderHook(() => useRunActions({ ...run, run_dir: null }, changed))
    await act(async () => { await result.current.submit({ action: 'verify' }) })
    expect(api.submitRun).not.toHaveBeenCalled()
    expect(changed).not.toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('workspace'), 'error')
  })

  it('scopes reset submissions and returns created records through onSubmitted', async () => {
    const submitted = vi.fn(), close = vi.fn()
    render(<ResetBoxModal open run={run} range={range} initial={{ box: 'web01', team: '102' }} onSubmitted={submitted} onClose={close} />)
    await screen.findByText('web01', { selector: 'button' })
    fireEvent.click(screen.getByRole('button', { name: 'Submit redeploy' }))
    await waitFor(() => expect(submitted).toHaveBeenCalledWith(created))
    expect(api.submitRun).toHaveBeenCalledWith({
      comp_id: 'comp-a', engine_ref: 'deploy-ref', action: 'redeploy',
      params: { boxes: 'web01', teams: '102', mode: 'reset', comp_dir: range.key },
    })
    expect(close).toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining(created.id))
  })

  it('constrains deploy-only NewRunModal and forwards the created record', async () => {
    const submitted = vi.fn()
    api.nodes.mockResolvedValue({ nodes: [{ name: 'node-a', node: 'pve-a', datastore: 'local', has_token: true }] })
    render(<NewRunModal open comps={[{ id: 'comp-a', name: 'A' }]} initial={{ comp_id: 'comp-a', action: 'destroy' }} allowedActions={['deploy']} onSubmitted={submitted} onClose={vi.fn()} />)
    expect(screen.getByText('Deploy range')).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Destroy' })).toBeNull()
    await screen.findByRole('checkbox', { name: /node-a/ })
    fireEvent.click(screen.getByRole('button', { name: 'Deploy 2 teams' }))
    await waitFor(() => expect(submitted).toHaveBeenCalledWith(created))
    expect(api.submitRun).toHaveBeenCalledWith({
      comp_id: 'comp-a', action: 'deploy', engine_ref: 'estate-v0.3.0', teams: 2,
      scoring_vmid: null, engine_node: '', team_node: '', nodes: ['node-a'],
    })
  })

  it('carries start-after-deploy and engine placement choices from the deploy form', async () => {
    api.nodes.mockResolvedValue({ nodes: [{ name: 'node-a', node: 'pve-a', datastore: 'local', has_token: true }, { name: 'node-b', node: 'pve-b', datastore: 'local', has_token: false }] })
    render(<NewRunModal open comps={[{ id: 'comp-a', name: 'A' }]} initial={{ comp_id: 'comp-a' }} allowedActions={['deploy']} onClose={vi.fn()} />)
    await screen.findByRole('checkbox', { name: /node-b/ })
    fireEvent.click(screen.getByRole('checkbox', { name: /Start scoring right after the deploy/ }))
    fireEvent.change(screen.getByLabelText('Engine node'), { target: { value: 'node-a' } })
    fireEvent.click(screen.getByRole('button', { name: /Deploy/ }))
    await waitFor(() => expect(api.submitRun).toHaveBeenCalled())
    const body = api.submitRun.mock.calls[0][0]
    expect(body.params).toEqual({ start_after_deploy: true })
    expect(body.engine_node).toBe('node-a')
    expect(body.nodes).toEqual(['node-a', 'node-b'])
  })
})

describe('durable run event recovery', () => {
  it('replays initial history, catches missed events on reconnect and deduplicates SSE by seq', async () => {
    vi.useFakeTimers()
    const streams = [], received = vi.fn()
    streamRunEvents.mockImplementation((id, onEvent, options) => new Promise((resolve) => {
      streams.push({ id, onEvent, options, resolve })
      options.signal.addEventListener('abort', resolve)
    }))
    api.runEvents.mockResolvedValueOnce({ next: 1, events: [event(1)] })
      .mockResolvedValueOnce({ next: 3, events: [event(2), event(3)] })
      .mockResolvedValue({ next: 5, events: [event(4), event(5)] })
    const { unmount } = renderHook(() => useRunEvents('run-a', received))
    await act(async () => {})
    expect(received.mock.calls.map(([ev]) => ev.seq)).toEqual([1])
    await act(async () => { streams[0].onEvent(event(3)); streams[0].onEvent(event(3)); streams[0].resolve() })
    expect(api.runEvents).toHaveBeenNthCalledWith(2, 'run-a', 1)
    expect(received.mock.calls.map(([ev]) => ev.seq)).toEqual([1, 3, 2])
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(streams).toHaveLength(2)
    expect(api.runEvents).toHaveBeenNthCalledWith(3, 'run-a', 3)
    expect(received.mock.calls.map(([ev]) => ev.seq)).toEqual([1, 3, 2, 4, 5])
    unmount()
    await act(async () => { streams[1].onEvent(event(6)) })
    expect(received).toHaveBeenCalledTimes(5)
  })

  it('retains the durable cursor across failed recovery even after newer SSE arrives', async () => {
    vi.useFakeTimers()
    const streams = [], received = vi.fn()
    streamRunEvents.mockImplementation((id, onEvent, options) => new Promise((resolve) => {
      streams.push({ onEvent, resolve })
      options.signal.addEventListener('abort', resolve)
    }))
    api.runEvents.mockResolvedValueOnce({ next: 1, events: [event(1)] })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue({ next: 3, events: [event(2), event(3)] })
    renderHook(() => useRunEvents('run-a', received))
    await act(async () => {})
    await act(async () => { streams[0].onEvent(event(3)); streams[0].resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(api.runEvents).toHaveBeenNthCalledWith(3, 'run-a', 1)
    expect(received.mock.calls.map(([ev]) => ev.seq)).toEqual([1, 3, 2])
  })
})

describe('run output isolation', () => {
  it('ignores an old run log response after selecting another operation', async () => {
    let resolveOld
    api.runLog.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      .mockResolvedValue({ offset: 10, text: 'new output', running: false, returncode: 0 })
    const { result, rerender } = renderHook(({ id }) => useRunLog(id), { initialProps: { id: 'old-run' } })
    rerender({ id: 'new-run' })
    await act(async () => {})
    expect(result.current.text).toBe('new output')
    await act(async () => { resolveOld({ offset: 10, text: 'old output', running: false, returncode: 1 }) })
    expect(result.current.text).toBe('new output')
    expect(result.current.returncode).toBe(0)
  })
})

describe('reusable diagnostic views', () => {
  it('renders flattened event error, state and command details', () => {
    const events = [event(1, { state: 'failed', error: 'engine refused' }), event(2, { cmd: ['python', 'verify.py'] })]
    const { unmount } = render(<EventList events={events} />)
    expect(screen.getByText('engine refused')).toBeTruthy()
    expect(screen.getByText('python verify.py')).toBeTruthy()
    unmount()
    render(<Timeline events={events} />)
    expect(screen.getByText('failed')).toBeTruthy()
    expect(screen.getByText('engine refused')).toBeTruthy()
    expect(screen.getByText('python verify.py')).toBeTruthy()
  })

  it('shows arbitrary evidence directories in range mode, with visible downloads', async () => {
    api.runArtifacts.mockResolvedValue([
      { path: 'engine-generated-name/REPORT.md', bytes: 100 },
      { path: 'collection.json', bytes: 200 },
    ])
    render(<Evidence runId="run-a" range={range} />)
    await screen.findByText('Range evidence')
    expect(screen.getByText('engine-generated-name · 1 files')).toBeTruthy()
    expect(screen.getByText('Workspace root · 1 files')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download engine-generated-name/REPORT.md' }).className).not.toContain('opacity-0')
    expect(screen.queryByText(/No evidence directory named/)).toBeNull()
  })
})

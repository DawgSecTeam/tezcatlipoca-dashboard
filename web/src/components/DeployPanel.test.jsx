import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DeployPanel, { derivedBoxVmids, engineVmidConflict } from './DeployPanel'
import { ToastProvider } from './kit'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    nodes: vi.fn().mockResolvedValue({ nodes: [] }),
    runs: vi.fn().mockResolvedValue([]),
    submitRun: vi.fn(),
    putNodes: vi.fn(),
    runLog: vi.fn().mockResolvedValue({ text: '', offset: 0, running: false, returncode: 0 }),
  },
  onTokenChanged: () => () => {},
}))

afterEach(cleanup)
beforeEach(() => {
  vi.clearAllMocks()
  api.nodes.mockResolvedValue({ nodes: [] })
  api.runs.mockResolvedValue([])
})

// Five boxes per team: identifiers 120/121 derive vmids 1400-1404 and 1410-1414.
const comp = {
  id: 'crimson-relay', name: 'Crimson Relay', deployed: false,
  boxes: [
    { name: 'web01', cpu: 1, memory_mb: 1024 },
    { name: 'db01', cpu: 1, memory_mb: 1024 },
    { name: 'ad01', cpu: 2, memory_mb: 2048 },
    { name: 'file01', cpu: 1, memory_mb: 1024 },
    { name: 'jump01', cpu: 1, memory_mb: 1024 },
  ],
}

function open() {
  return render(<MemoryRouter><ToastProvider><DeployPanel comp={comp} /></ToastProvider></MemoryRouter>)
}

describe('derived engine-vmid collision check', () => {
  it('derives box vmids as 200 + identifier*10 + boxIndex for the first `teams` identifiers', () => {
    expect(derivedBoxVmids('120,121', 2, 5)).toEqual([
      { identifier: 120, vmids: [1400, 1401, 1402, 1403, 1404] },
      { identifier: 121, vmids: [1410, 1411, 1412, 1413, 1414] },
    ])
    // More identifiers than teams: only the first `teams` are used, exactly like the engine.
    expect(derivedBoxVmids('120,121,122', 1, 2).map((t) => t.identifier)).toEqual([120])
    expect(derivedBoxVmids('', 2, 5)).toEqual([])
  })

  it('names the collision the arithmetic already knows (121 puts box 0 on 1410)', () => {
    expect(engineVmidConflict('120,121', 2, 5, '1410')).toEqual({ vmid: 1410, identifier: 121, boxIndex: 0 })
    expect(engineVmidConflict('120,121', 2, 5, '1404')).toEqual({ vmid: 1404, identifier: 120, boxIndex: 4 })
    expect(engineVmidConflict('120,121', 2, 5, '1405')).toBeNull()
    expect(engineVmidConflict('120,121', 2, 5, '')).toBeNull()
  })

  it('shows the derived box vmids so the arithmetic is visible', () => {
    open()
    fireEvent.change(screen.getByLabelText(/^Team identifiers/), { target: { value: '120,121' } })
    expect(screen.getByText(/team 120 → 1400–1404/)).toBeTruthy()
    expect(screen.getByText(/team 121 → 1410–1414/)).toBeTruthy()
  })

  it('refuses the submit inline when the Engine VM ID lands on one of this deploy’s boxes', () => {
    open()
    fireEvent.change(screen.getByLabelText(/^Team identifiers/), { target: { value: '120,121' } })
    fireEvent.change(screen.getByLabelText(/^Engine VM ID/), { target: { value: '1410' } })
    const alert = screen.getByRole('alert')
    // The error names the box vmid it collides with, the team and the box.
    expect(alert.textContent).toContain('1410')
    expect(alert.textContent).toContain('team 121')
    expect(alert.textContent).toContain('web01')
    expect(screen.getByRole('button', { name: /Deploy 2 teams/ }).disabled).toBe(true)
  })

  it('lets the submit through once the Engine VM ID is off the derived set', async () => {
    api.submitRun.mockResolvedValue({ id: 'run-ok', action: 'deploy', comp_id: comp.id, state: 'queued' })
    api.runLog.mockResolvedValue({ text: '', offset: 0, running: false, returncode: 0 })
    open()
    fireEvent.change(screen.getByLabelText(/^Team identifiers/), { target: { value: '120,121' } })
    fireEvent.change(screen.getByLabelText(/^Engine VM ID/), { target: { value: '1420' } })
    expect(screen.queryByRole('alert')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /Deploy 2 teams/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Deploy', selector: 'div[role="dialog"] button' }))
    await waitFor(() => expect(api.submitRun).toHaveBeenCalled())
  })
})

describe('deploy submit feedback', () => {
  it('toasts the submit with a link to the run page', async () => {
    api.submitRun.mockResolvedValue({ id: 'run-toast-1', action: 'deploy', comp_id: comp.id, state: 'queued' })
    api.runLog.mockResolvedValue({ text: '', offset: 0, running: false, returncode: 0 })
    open()
    await userEvent.click(screen.getByRole('button', { name: /Deploy 2 teams/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Deploy', selector: 'div[role="dialog"] button' }))
    // The toast carries the run id as a link to /runs?run=<id> (the audit's missing
    // follow-up: confirming closed the modal with nothing left of the deploy).
    const link = await screen.findByRole('link', { name: 'run-toast-1' })
    expect(link.getAttribute('href')).toBe('/runs?run=run-toast-1')
    expect(document.body.textContent).toContain('Deploy submitted')
  })

  it('sends the auto-reclaim opt-in as a run param', async () => {
    api.submitRun.mockResolvedValue({ id: 'run-reclaim', action: 'deploy', comp_id: comp.id, state: 'queued' })
    api.runLog.mockResolvedValue({ text: '', offset: 0, running: false, returncode: 0 })
    open()
    await userEvent.click(screen.getByRole('checkbox', { name: /Reclaim leftover VMs/ }))
    await userEvent.click(screen.getByRole('button', { name: /Deploy 2 teams/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Deploy', selector: 'div[role="dialog"] button' }))
    await waitFor(() => expect(api.submitRun).toHaveBeenCalled())
    expect(api.submitRun.mock.calls[0][0].params).toEqual({ auto_reclaim: true })
  })
})

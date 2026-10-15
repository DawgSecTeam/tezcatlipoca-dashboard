import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './api'
import { useRangeData } from './useRangeData'
vi.mock('./api', () => ({ api: { range: vi.fn() } }))
afterEach(() => { cleanup(); vi.useRealTimers() })
beforeEach(() => { vi.clearAllMocks() })

it('refreshes recorded inventory after an operation', async () => {
  api.range.mockResolvedValueOnce({ id: 'a', inventory: { counts: { boxes: 0 } } }).mockResolvedValueOnce({ id: 'a', inventory: { counts: { boxes: 3 } } })
  const { result } = renderHook(() => useRangeData('a'))
  await waitFor(() => expect(result.current.data?.inventory.counts.boxes).toBe(0))
  act(() => result.current.refresh())
  await waitFor(() => expect(result.current.data?.inventory.counts.boxes).toBe(3))
})

it('retains data but exposes refresh failures instead of showing empty inventory', async () => {
  api.range.mockResolvedValueOnce({ id: 'a' }).mockRejectedValueOnce(new Error('offline'))
  const { result } = renderHook(() => useRangeData('a'))
  await waitFor(() => expect(result.current.data?.id).toBe('a'))
  act(() => result.current.refresh())
  await waitFor(() => expect(result.current.error?.message).toBe('offline'))
  expect(result.current.data.id).toBe('a')
})

it('ignores late responses from the previously viewed range', async () => {
  let finishOld
  api.range.mockImplementation((id) => id === 'a' ? new Promise((resolve) => { finishOld = resolve }) : Promise.resolve({ id: 'b' }))
  const { result, rerender } = renderHook(({ id }) => useRangeData(id), { initialProps: { id: 'a' } })
  rerender({ id: 'b' })
  await waitFor(() => expect(result.current.data?.id).toBe('b'))
  await act(async () => finishOld({ id: 'a' }))
  expect(result.current.data.id).toBe('b')
})

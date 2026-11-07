import { afterEach, expect, it, vi } from 'vitest'
import { copyText } from './kit'

function setClipboard(value) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true })
}

afterEach(() => {
  delete navigator.clipboard
  delete document.execCommand
})

it('copies through the clipboard API when the page is a secure context', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  setClipboard({ writeText })
  await expect(copyText('ADMIN-SECRET')).resolves.toBe(true)
  expect(writeText).toHaveBeenCalledWith('ADMIN-SECRET')
})

it('falls back to a selection copy where navigator.clipboard is absent (plain-http LAN)', async () => {
  setClipboard(undefined)
  document.execCommand = vi.fn(() => true)
  await expect(copyText('ADMIN-SECRET')).resolves.toBe(true)
  expect(document.execCommand).toHaveBeenCalledWith('copy')
})

it('falls back when the clipboard API rejects the write', async () => {
  setClipboard({ writeText: vi.fn().mockRejectedValue(new Error('not allowed')) })
  document.execCommand = vi.fn(() => true)
  await expect(copyText('ADMIN-SECRET')).resolves.toBe(true)
  expect(document.execCommand).toHaveBeenCalledWith('copy')
})

it('reports failure instead of pretending a copy happened', async () => {
  setClipboard(undefined)
  document.execCommand = vi.fn(() => false)
  await expect(copyText('ADMIN-SECRET')).resolves.toBe(false)
})

it('does not touch the document for an empty value', async () => {
  setClipboard(undefined)
  document.execCommand = vi.fn(() => true)
  await expect(copyText('')).resolves.toBe(false)
  expect(document.execCommand).not.toHaveBeenCalled()
})

// ── timeAgo / agoMs: uninitialized timestamps ──────────────────────────────────────────
// The engine is Go: a zero-value time arrives over the wire as '0001-01-01T00:00:00Z',
// and an operator watched 'last round 1065493565 min ago' hunt a round that never
// happened (2026-11-06 audit). Anything pre-2000 is an unset field, not history.
import { agoMs, timeAgo } from './kit'

it('clamps an uninitialized (Go zero-value) timestamp to never', () => {
  expect(agoMs('0001-01-01T00:00:00Z')).toBe('never')
  expect(timeAgo(-62135596800)).toBe('never')
  expect(timeAgo(0)).toBe('')           // absent is still absent, not 'never'
})

it('still renders ordinary relative times', () => {
  const now = Date.now() / 1000
  expect(timeAgo(now - 30)).toBe('just now')
  expect(timeAgo(now - 120)).toBe('2 min ago')
  expect(timeAgo(now - 7200)).toBe('2h ago')
})

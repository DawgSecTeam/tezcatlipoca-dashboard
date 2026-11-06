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

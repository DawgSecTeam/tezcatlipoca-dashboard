import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it } from 'vitest'
import TokenGate from './TokenGate'
import { TOKEN_CHANGED_EVENT } from '../api'

// The token dialog's contract (2026-11-06 audit):
//  * Save stores the token AND tells the open pages to refetch — the 'unauthorized'
//    banner and blank counts used to survive a valid Save until a manual reload.
//  * A 401 (`tez-unauthorized`) may only OPEN the dialog. Deleting the stored token is
//    the explicit Clear button's alone.

let events = []
let track
beforeEach(() => {
  events = []
  track = (e) => events.push(e)
  window.addEventListener(TOKEN_CHANGED_EVENT, track)
})
afterEach(() => { cleanup(); localStorage.clear(); window.removeEventListener(TOKEN_CHANGED_EVENT, track) })

function open() {
  return render(<TokenGate><p>the page</p></TokenGate>)
}

it('starts closed when a token is already stored', () => {
  localStorage.setItem('tez-token', 'keep-me')
  open()
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('Save stores the token and dispatches tez-token-changed so pages refetch', async () => {
  localStorage.clear()
  open()
  fireEvent.change(screen.getByLabelText('Token'), { target: { value: '  tk-new-1  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
  expect(localStorage.getItem('tez-token')).toBe('tk-new-1')
  await waitFor(() => expect(events.length).toBe(1))
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('a 401 opens the dialog but never clears the stored token', async () => {
  localStorage.setItem('tez-token', 'keep-me')
  open()
  await act(async () => { window.dispatchEvent(new CustomEvent('tez-unauthorized')) })
  // The dialog appears to collect a better token...
  expect(screen.getByRole('dialog')).toBeTruthy()
  // ...but the stored token survives: a wrong-but-typed token is worth keeping, and no
  // 401 path may delete it.
  expect(localStorage.getItem('tez-token')).toBe('keep-me')
  fireEvent.click(screen.getByRole('button', { name: '✕' }))
  expect(localStorage.getItem('tez-token')).toBe('keep-me')
  expect(events.length).toBe(0) // merely seeing a 401 refetches nothing
})

it('the explicit Clear button is the only way to delete a token', async () => {
  localStorage.setItem('tez-token', 'keep-me')
  open()
  await act(async () => { window.dispatchEvent(new CustomEvent('tez-unauthorized')) })
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
  expect(localStorage.getItem('tez-token')).toBeNull()
})

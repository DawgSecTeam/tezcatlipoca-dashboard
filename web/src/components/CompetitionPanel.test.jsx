import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import CompetitionPanel from './CompetitionPanel'
import { ConfirmProvider, ToastProvider } from './kit'

afterEach(cleanup)

const view = (lastRound) => ({ state: 'running', reachable: true, engine: { last_round: lastRound }, links: {}, creds: {} })

function open(panelView, extra = {}) {
  return render(<MemoryRouter><ToastProvider><ConfirmProvider>
    <CompetitionPanel rangeKey="/synthetic/one/comp" rangeId="range-one" compId="one" engineRef="main"
      view={panelView} loading={false} error={null} onRefresh={() => {}} {...extra} />
  </ConfirmProvider></ToastProvider></MemoryRouter>)
}

it('renders the engine round record as its round number', () => {
  open(view({ ID: 4, StartTime: '2026-10-18T00:00:00Z', Checks: null, SLAs: null }))
  expect(screen.getByText('round 4')).toBeTruthy()
})

it('shows no round before the first one exists', () => {
  open(view({ ID: 0, StartTime: '0001-01-01T00:00:00Z', Checks: null, SLAs: null }))
  expect(screen.queryByText(/round/)).toBeNull()
})

it('holds the lifecycle and credentials back while the range is still deploying', () => {
  open({
    state: 'deploying', deploying: true, reachable: false, engine: {}, links: {},
    creds: { admin: { user: 'admin', password: 'ADMIN-SECRET' } },
  })
  expect(screen.getByText('deploying')).toBeTruthy()
  expect(screen.getByText(/still deploying/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start the competition' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Copy admin password' })).toBeNull()
})

it('holds the lifecycle controls while another range operation is in flight', () => {
  open({ state: 'ready', settled: false, reachable: true, engine: {}, links: {}, creds: {} })
  expect(screen.getByText(/operation is still running/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start the competition' })).toBeNull()
})

it('does not offer engine links while the engine is not answering', () => {
  open({
    state: 'unknown', reachable: false, engine: {},
    links: { scoreboard: 'http://dead', admin: 'http://dead/admin', portal: 'http://portal' },
    creds: {},
  })
  expect(screen.queryByRole('link', { name: /Scoreboard/ })).toBeNull()
  // The portal may live on another host: its own link survives.
  expect(screen.getByRole('link', { name: /Team portal/ })).toBeTruthy()
})

it('does not offer recorded passwords for a range with no VMs', () => {
  open({
    state: 'unknown', reachable: false, engine: {}, links: {},
    creds: { admin: { user: 'admin', password: 'ADMIN-SECRET' } },
  }, { recorded: false })
  expect(screen.getByText('No engine recorded')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Copy admin password' })).toBeNull()
  expect(screen.queryByText(/not answering/)).toBeNull()
})

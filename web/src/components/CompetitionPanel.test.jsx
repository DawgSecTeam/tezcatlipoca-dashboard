import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import CompetitionPanel from './CompetitionPanel'
import { ConfirmProvider, ToastProvider } from './kit'

afterEach(cleanup)

const view = (lastRound) => ({ state: 'running', reachable: true, engine: { last_round: lastRound }, links: {}, creds: {} })

function open(panelView) {
  return render(<MemoryRouter><ToastProvider><ConfirmProvider>
    <CompetitionPanel rangeKey="/synthetic/one/comp" rangeId="range-one" compId="one" engineRef="main"
      view={panelView} loading={false} error={null} onRefresh={() => {}} />
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

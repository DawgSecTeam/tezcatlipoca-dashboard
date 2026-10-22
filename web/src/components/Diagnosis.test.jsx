// The panel reads a server-computed verdict, so the test is about what a reader sees: the
// headline, the quoted log line, and the jump that lands on it.
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import Diagnosis from './Diagnosis'

afterEach(cleanup)

const failed = {
  subject: { kind: 'run', id: 'run-1', comp_id: 'c', action: 'deploy', state: 'failed' },
  severity: 'error',
  headline: 'Deploy failed in phase 5/8 (firewall bootstrap): A Python dependency is missing in the service venv',
  stage: { label: 'phase 5/8 firewall_bootstrap', index: 5, total: 8 },
  cause: { source: 'run-log', line: 212, offset: 9000, quote: "ModuleNotFoundError: No module named 'toml'" },
  findings: [{
    code: 'missing_dependency', severity: 'error',
    title: 'A Python dependency is missing in the service venv',
    detail: 'The drivers run under the service interpreter.',
    advice: 'Install requirements.txt in that venv, then submit again.',
    evidence: [{ source: 'run-log', line: 212, offset: 9000, quote: "ModuleNotFoundError: No module named 'toml'" }],
  }],
  facts: { returncode: 1, duration_s: 95, engine_head: 'abcdef0123456789', workspace_missing: false },
  as_of: Date.now() / 1000,
}

it('leads with the server headline and quotes the failing line', () => {
  render(<Diagnosis diagnosis={failed} />)
  expect(screen.getByText(/Deploy failed in phase 5\/8/)).toBeTruthy()
  expect(screen.getByText(/No module named 'toml'/)).toBeTruthy()
  expect(screen.getByText(/line 212/)).toBeTruthy()
  expect(screen.getByText(/Install requirements\.txt/)).toBeTruthy()
})

it('jumps to the quoted log line when asked', async () => {
  const onFocusLine = vi.fn()
  render(<Diagnosis diagnosis={failed} onFocusLine={onFocusLine} />)
  await userEvent.click(screen.getByText('Jump to log'))
  expect(onFocusLine).toHaveBeenCalledWith(212)
})

it('says nothing about causes when the run is healthy', () => {
  render(<Diagnosis diagnosis={{
    ...failed, severity: 'ok', findings: [], cause: null,
    headline: 'Deploy succeeded in phase 8/8 (seed)',
    stage: { label: 'phase 8/8 seed', index: 8, total: 8 },
    facts: { ...failed.facts, returncode: 0 },
  }} />)
  expect(screen.getByText(/succeeded in phase 8\/8/)).toBeTruthy()
  expect(screen.queryByText(/Jump to log/)).toBeNull()
  expect(screen.queryByText(/Needs attention/)).toBeNull()
  expect(screen.getByText('Healthy')).toBeTruthy()
})

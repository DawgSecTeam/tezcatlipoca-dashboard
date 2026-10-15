import { describe, expect, it } from 'vitest'
import { rangeActionBody } from './rangeModel'

const origin = { id: 'deploy-a', action: 'deploy', comp_id: 'comp-a', engine_ref: 'origin-ref' }
const latest = { id: 'verify-a', action: 'verify', comp_id: 'comp-a', engine_ref: 'wrong-new-ref' }
const range = { key: '/ranges/a/comp', comp_id: 'comp-a', deploy_run_id: origin.id, runs: [latest, origin] }

describe('rangeActionBody', () => {
  it('pins the selected workspace and deploy revision, preserving driver params', () => {
    expect(rangeActionBody(range, 'redeploy', { teams: '102', boxes: 'web01', comp_dir: '/ranges/b/comp' })).toEqual({
      comp_id: 'comp-a', engine_ref: 'origin-ref', action: 'redeploy',
      params: { teams: '102', boxes: 'web01', comp_dir: '/ranges/a/comp' },
    })
  })

  it('prefers the resolved range identity over historical run fields', () => {
    expect(rangeActionBody({ ...range, comp_id: 'resolved-comp', engine_ref: 'resolved-origin-ref' }, 'destroy')).toEqual({
      comp_id: 'resolved-comp', engine_ref: 'resolved-origin-ref', action: 'destroy',
      params: { comp_dir: range.key },
    })
  })

  it('can infer identity from the deploy history without mutating caller params', () => {
    const params = { team: 'team2' }
    expect(rangeActionBody({ ...range, comp_id: undefined }, 'delete_team', params).comp_id).toBe('comp-a')
    expect(params).toEqual({ team: 'team2' })
  })

  it.each([
    [{ ...range, key: '' }, 'workspace'],
    [{ key: range.key, engine_ref: 'ref' }, 'competition ID'],
    [{ key: range.key, comp_id: 'comp-a' }, 'engine revision'],
  ])('rejects incomplete range identity instead of submitting an unscoped action', (value, message) => {
    expect(() => rangeActionBody(value, 'verify')).toThrow(message)
  })
})

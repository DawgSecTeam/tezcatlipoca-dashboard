// A follow-up action always names the workspace of its range; never let the server choose
// the newest workspace of a competition implicitly. Verify/redeploy must also run the engine
// revision that built the range, but a teardown must not: its fix may postdate a SHA-pinned
// deploy, and the state document is versioned so current code reads an older document.
export function rangeActionBody(range, action, params = {}) {
  const runs = range?.runs || []
  const deploy = runs.find((run) => run.id === range?.deploy_run_id)
    || runs.find((run) => run.action === 'deploy' || run.action === 'scrim')
  const compId = range?.comp_id || deploy?.comp_id || runs.find((run) => run.comp_id)?.comp_id
  const engineRef = range?.engine_ref || deploy?.engine_ref || runs.find((run) => run.engine_ref)?.engine_ref
  if (!range?.key) throw new Error('Cannot submit a range action without an explicit workspace.')
  if (!compId) throw new Error('Cannot submit a range action without a competition ID.')
  if (action !== 'destroy' && !engineRef) throw new Error('Cannot submit a range action without its engine revision.')
  if (!action) throw new Error('Choose a range action to submit.')
  return {
    comp_id: compId,
    // Omitted for a destroy: the service's default ref is what carries teardown fixes to a
    // range that was cut from an older SHA. `engine_head` records what actually ran.
    ...(action === 'destroy' ? {} : { engine_ref: engineRef }),
    action,
    params: { ...params, comp_dir: range.key },
  }
}

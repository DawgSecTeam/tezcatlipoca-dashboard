// A follow-up action always names the workspace and the engine revision of its range.
// Never let the server choose the newest workspace of a competition implicitly.
export function rangeActionBody(range, action, params = {}) {
  const runs = range?.runs || []
  const deploy = runs.find((run) => run.id === range?.deploy_run_id)
    || runs.find((run) => run.action === 'deploy' || run.action === 'scrim')
  const compId = range?.comp_id || deploy?.comp_id || runs.find((run) => run.comp_id)?.comp_id
  const engineRef = range?.engine_ref || deploy?.engine_ref || runs.find((run) => run.engine_ref)?.engine_ref
  if (!range?.key) throw new Error('Cannot submit a range action without an explicit workspace.')
  if (!compId) throw new Error('Cannot submit a range action without a competition ID.')
  if (!engineRef) throw new Error('Cannot submit a range action without its engine revision.')
  if (!action) throw new Error('Choose a range action to submit.')
  return {
    comp_id: compId,
    engine_ref: engineRef,
    action,
    params: { ...params, comp_dir: range.key },
  }
}

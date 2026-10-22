import { useCallback, useEffect, useState } from 'react'
import { api } from './api'

export function useRangeData(id) {
  const [state, setState] = useState({ id, data: null, error: null, updated: null })
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision((value) => value + 1), [])
  useEffect(() => {
    let active = true
    let busy = false
    const load = async () => {
      if (busy) return
      busy = true
      try {
        const data = await api.range(id)
        if (active) setState({ id, data, error: null, updated: Date.now() })
      } catch (error) {
        if (active) setState((old) => ({ id, data: old.id === id ? old.data : null, updated: old.id === id ? old.updated : null, error }))
      } finally { busy = false }
    }
    load()
    const timer = setInterval(load, 10000)
    return () => { active = false; clearInterval(timer) }
  }, [id, revision])
  return { ...(state.id === id ? state : { data: null, error: null, updated: null }), refresh }
}

export function useCompetitionData(key, revision) {
  const [state, setState] = useState({ key, view: null, error: null, updated: null })
  useEffect(() => {
    if (!key) return
    let active = true
    let busy = false
    const load = async () => {
      if (busy) return
      busy = true
      try {
        const view = await api.competition(key)
        if (active) setState({ key, view, error: null, updated: Date.now() })
      } catch (error) {
        if (active) setState((old) => ({ key, view: old.key === key ? old.view : null, updated: old.key === key ? old.updated : null, error }))
      } finally { busy = false }
    }
    load()
    const timer = setInterval(load, 10000)
    return () => { active = false; clearInterval(timer) }
  }, [key, revision])
  return state.key === key ? state : { view: null, error: null, updated: null }
}

// The range's server-computed triage answer, on the same 10s cadence as the range itself.
// A diagnosis is advisory chrome: if it cannot be fetched the page still works, so the
// error is swallowed and the next tick retries.
export function useRangeDiagnosis(id) {
  const [diagnosis, setDiagnosis] = useState(null)
  useEffect(() => {
    if (!id) return
    let active = true
    let busy = false
    setDiagnosis(null)
    const load = async () => {
      if (busy) return
      busy = true
      try {
        const data = await api.rangeDiagnosis(id)
        if (active) setDiagnosis(data)
      } catch { /* the page's own error banner covers a dead service */ }
      finally { busy = false }
    }
    load()
    const timer = setInterval(load, 10000)
    return () => { active = false; clearInterval(timer) }
  }, [id])
  return diagnosis
}

// Thin fetch wrapper: every call throws an Error carrying the server's `detail` message.
// Every request sends the bearer token; a 401 dispatches `tez-unauthorized` so the token
// dialog can collect one.

const TOKEN_KEY = 'tez-token'

export const getToken = () => {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}
export const setToken = (t) => {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch { /* storage blocked */ }
}

async function request(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      Authorization: `Bearer ${getToken()}`,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent('tez-unauthorized'))
    const d = data?.detail
    const msg = Array.isArray(d) ? d.map((e) => `${(e.loc || []).join('.')}: ${e.msg}`).join('\n') : d
    throw new Error(msg ? String(msg) : `${res.status} ${res.statusText}`)
  }
  return data
}

const enc = encodeURIComponent

export const api = {
  comps: () => request('GET', '/comps'),
  createComp: (body) => request('POST', '/comps', body),
  comp: (id) => request('GET', `/comps/${enc(id)}`),
  putCompfile: (id, items) => request('PUT', `/comps/${enc(id)}/compfile`, { items }),

  addBox: (id, box) => request('POST', `/comps/${enc(id)}/boxes`, box),
  updateBox: (id, name, box) => request('PUT', `/comps/${enc(id)}/boxes/${enc(name)}`, box),
  deleteBox: (id, name) => request('DELETE', `/comps/${enc(id)}/boxes/${enc(name)}`),
  putPins: (id, name, kind, pins) =>
    request('PUT', `/comps/${enc(id)}/boxes/${enc(name)}/${kind}`, pins),

  inject: (id, slug) => request('GET', `/comps/${enc(id)}/injects/${enc(slug)}`),
  putInject: (id, slug, meta, body) =>
    request('PUT', `/comps/${enc(id)}/injects/${enc(slug)}`, { meta, body }),
  deleteInject: (id, slug) => request('DELETE', `/comps/${enc(id)}/injects/${enc(slug)}`),
  packet: (id) => request('GET', `/comps/${enc(id)}/packet`),
  putPacket: (id, body) => request('PUT', `/comps/${enc(id)}/packet`, { body }),

  catalog: (params) => request('GET', `/catalog?${new URLSearchParams(params)}`),
  catalogEntry: (name) => request('GET', `/catalog/${enc(name)}`),
  templates: () => request('GET', '/templates'),
  nodes: () => request('GET', '/nodes'),
  putNodes: (body) => request('PUT', '/nodes', body),

  // runs: POST /api/runs {comp_id, action, engine_ref, teams, scoring_vmid, engine_node,
  // team_node, params}; action ∈ deploy|plan|verify|destroy|scrim|redeploy.
  submitRun: (body) => request('POST', '/runs', body),
  runs: (state) => request('GET', `/runs${state && state !== 'all' ? `?state=${enc(state)}` : ''}`),
  run: (id) => request('GET', `/runs/${enc(id)}`),
  cancelRun: (id) => request('POST', `/runs/${enc(id)}/cancel`),
  runLog: (id, since = 0) => request('GET', `/runs/${enc(id)}/log?since=${since || 0}`),
  runEvents: (id, since = 0) => request('GET', `/runs/${enc(id)}/events?since=${since || 0}`),
  runArtifacts: (id) => request('GET', `/runs/${enc(id)}/artifacts`),
  // A single artifact file as a Blob (request() parses JSON, so this fetches raw).
  runArtifactFile: async (id, path) => {
    const res = await fetch(`/api/runs/${enc(id)}/artifacts/file?path=${enc(path)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    })
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new CustomEvent('tez-unauthorized'))
      const d = await res.json().catch(() => null)
      throw new Error(d?.detail ? String(d.detail) : `${res.status} ${res.statusText}`)
    }
    return res.blob()
  },
}

// SSE of run events, with the bearer header intact (EventSource cannot send one).
// Calls onEvent per server event and resolves when the stream ends.
export async function streamRunEvents(id, onEvent, { since = 0, signal } = {}) {
  const res = await fetch(`/api/runs/${enc(id)}/events/stream?since=${since}`, {
    headers: { Accept: 'text/event-stream', Authorization: `Bearer ${getToken()}` },
    signal,
  })
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent('tez-unauthorized'))
    throw new Error(`${res.status} ${res.statusText}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const data = frame.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trim()).join('\n')
      if (data) {
        try { onEvent(JSON.parse(data)) } catch { /* non-JSON keepalive */ }
      }
    }
  }
}

export const platformOf = (template) => (/win/i.test(template || '') ? 'windows' : 'linux')

// What a box IS for rendering: an in-path firewall (unmanaged + in_path — it owns the
// team gateway .1 and terraform wires it between the engine and the team switch), or a
// plain platform. The single classifier for topology, sidebar dots and badges — never
// sniff template or host names (that's how firewalls got drawn as Linux hosts).
export const kindOf = (box) =>
  box?.in_path ? 'firewall' : platformOf(box?.template)

// Box pins are either a bare catalog name or {name, vars, ...}. The UI always works on objects.
export const asPin = (p) => (typeof p === 'string' ? { name: p } : p)

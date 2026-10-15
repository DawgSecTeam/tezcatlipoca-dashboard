import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useMatch, useNavigate, useParams } from 'react-router-dom'
import { api, kindOf } from '../api'
import { ErrorBanner, Spinner } from '../components/ui'
import { Icon, Logo } from '../components/kit'
import Bell from '../components/Bell'

const CompCtx = createContext(null)
export const useComp = () => useContext(CompCtx)

function CompSwitcher({ current }) {
  const [comps, setComps] = useState([])
  const navigate = useNavigate()
  useEffect(() => { api.comps().then(setComps).catch(() => {}) }, [])
  return (
    <div className="relative">
      <select value={current} onChange={(e) => navigate(`/c/${e.target.value}`)} aria-label="Switch competition"
        className="field cursor-pointer appearance-none truncate rounded-2xl py-2.5 pr-9 font-semibold">
        {comps.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        {!comps.some((c) => c.id === current) && <option value={current}>{current}</option>}
      </select>
      <svg className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M7 8l3-3 3 3M7 12l3 3 3-3" /></svg>
    </div>
  )
}

const navCls = ({ isActive }) =>
  `group flex items-center gap-3 rounded-full px-4 py-2 text-sm transition ${isActive ? 'bg-accent-soft font-semibold text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`

const miniCls = ({ isActive }) =>
  `rounded-full px-2.5 py-1 font-semibold transition ${isActive ? 'bg-accent-soft text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`

function Count({ n }) {
  return <span className="ml-auto text-[11px] font-semibold tabular-nums text-faint">{n}</span>
}

const DOT = { windows: 'var(--win)', linux: 'var(--lin)', firewall: 'var(--fw)' }
const dot = (b) => DOT[kindOf(b)]

function Sidebar({ comp }) {
  const inBox = useMatch('/c/:id/boxes/:box/*')
  const boxName = inBox?.params.box
  const inRun = useMatch('/c/:id/runs/:run/*')
  const runId = inRun?.params.run
  const [boxesOpen, setBoxesOpen] = useState(true)
  const base = `/c/${comp.id}`
  const box = comp.boxes.find((b) => b.name === boxName)

  return (
    <aside className="panel flex h-full w-64 flex-col gap-5 p-4">
      <Link to="/" className="group flex items-center gap-2 px-2 pt-1" title="Live">
        <Logo className="h-5 w-5" />
        <span className="text-[13px] font-semibold tracking-tight">tezcatlipoca</span>
        <span className="ml-auto text-[11px] text-faint opacity-0 transition group-hover:opacity-100">live</span>
      </Link>
      <div className="flex items-center gap-1 px-1">
        <nav className="flex flex-1 gap-1 text-xs" aria-label="Sections">
          <NavLink to="/" end className={miniCls}>Live</NavLink>
          <NavLink to="/runs" className={miniCls}>Runs</NavLink>
          <NavLink to="/comps" className={miniCls}>Comps</NavLink>
        </nav>
        <Bell />
      </div>
      <CompSwitcher current={comp.id} />
      {boxName ? (
        <nav className="space-y-1" aria-label="Box">
          <Link to={`${base}/boxes`} className="mb-2 flex items-center gap-2 px-4 py-1.5 text-sm text-muted transition hover:text-accent">
            <Icon name="back" className="h-3.5 w-3.5" /> All boxes
          </Link>
          <div className="flex items-center gap-2 px-4 pb-1 pt-1">
            {box && <span className="h-2 w-2 rounded-full" style={{ background: dot(box) }} />}
            <span className="font-mono text-sm font-semibold">{boxName}</span>
          </div>
          <NavLink end to={`${base}/boxes/${boxName}`} className={navCls}><Icon name="box" />Summary</NavLink>
          <NavLink to={`${base}/boxes/${boxName}/services`} className={navCls}><Icon name="service" />Services<Count n={(comp.box_services[boxName] || []).length} /></NavLink>
          <NavLink to={`${base}/boxes/${boxName}/misconfigs`} className={navCls}><Icon name="shield" />Misconfigs<Count n={(comp.box_vulns[boxName] || []).length} /></NavLink>
        </nav>
      ) : runId ? (
        <nav className="space-y-1" aria-label="Run">
          <Link to={base} className="mb-2 flex items-center gap-2 px-4 py-1.5 text-sm text-muted transition hover:text-accent">
            <Icon name="back" className="h-3.5 w-3.5" /> {comp.name}
          </Link>
          <div className="truncate px-4 pb-1 pt-1 font-mono text-sm font-semibold" title={runId}>{runId}</div>
          <NavLink end to={`${base}/runs/${runId}`} className={navCls}><Icon name="overview" />Overview</NavLink>
          <NavLink to={`${base}/runs/${runId}/teams`} className={navCls}><Icon name="boxes" />Teams</NavLink>
          <NavLink to={`${base}/runs/${runId}/infrastructure`} className={navCls}><Icon name="cpu" />Infrastructure</NavLink>
          <NavLink to={`${base}/runs/${runId}/evidence`} className={navCls}><Icon name="packet" />Evidence</NavLink>
        </nav>
      ) : (
        <nav className="min-h-0 flex-1 space-y-1 overflow-auto" aria-label="Competition">
          <NavLink end to={base} className={navCls}><Icon name="overview" />Overview</NavLink>
          <NavLink to={`${base}/boxes`} end className={navCls}>
            <Icon name="boxes" />Boxes
            <span className="ml-auto flex items-center gap-1">
              <span className="text-[11px] font-semibold tabular-nums text-faint">{comp.boxes.length}</span>
              <button onClick={(e) => { e.preventDefault(); setBoxesOpen(!boxesOpen) }} aria-label={boxesOpen ? 'Collapse boxes' : 'Expand boxes'}
                className="rounded-full p-0.5 text-faint hover:text-fg">
                <Icon name="chevron" className={`h-3.5 w-3.5 transition ${boxesOpen ? 'rotate-90' : ''}`} />
              </button>
            </span>
          </NavLink>
          {boxesOpen && (
            <div className="ml-6 space-y-0.5 border-l-2 border-line py-1 pl-3">
              {[...comp.boxes].sort((a, b) => a.last_octet - b.last_octet).map((b) => (
                <NavLink key={b.name} to={`${base}/boxes/${b.name}`} className={({ isActive }) => `flex items-center gap-2 rounded-full px-3 py-1 font-mono text-[13px] transition ${isActive ? 'text-accent' : 'text-muted hover:text-fg'}`}>
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: dot(b) }} />{b.name}
                </NavLink>
              ))}
              {!comp.boxes.length && <Link to={`${base}/boxes`} className="block px-3 py-1 text-xs text-faint hover:text-accent">+ add the first box</Link>}
            </div>
          )}
          <NavLink to={`${base}/injects`} className={navCls}><Icon name="injects" />Injects<Count n={comp.injects} /></NavLink>
          <NavLink to={`${base}/packet`} className={navCls}><Icon name="packet" />Packet</NavLink>
        </nav>
      )}
      <div className="mt-auto px-1">
        <span className="truncate font-mono text-[11px] text-faint" title={`competitions/${comp.id}/`}>{comp.id}/</span>
      </div>
    </aside>
  )
}

export default function CompLayout() {
  const { id } = useParams()
  const [comp, setComp] = useState(null)
  const [err, setErr] = useState(null)
  const reload = useCallback(() => api.comp(id).then(setComp).catch(setErr), [id])
  useEffect(() => { setComp(null); setErr(null); reload() }, [reload])
  useEffect(() => { document.title = comp ? `${comp.name} · tezcatlipoca` : 'tezcatlipoca' }, [comp])

  if (err) return <div className="p-10"><ErrorBanner error={err} /><Link className="text-accent" to="/">← All competitions</Link></div>
  if (!comp) return <div className="flex h-screen items-center justify-center"><Spinner /></div>

  return (
    <CompCtx.Provider value={{ comp, reload, setComp }}>
      <div className="flex h-screen">
        <div className="shrink-0 p-6 pr-0"><Sidebar comp={comp} /></div>
        {/* Padding lives inside the scroller so the floating panels' halos are never clipped. */}
        <main className="min-w-0 flex-1 overflow-auto p-6 pl-9">
          <Outlet />
        </main>
      </div>
    </CompCtx.Provider>
  )
}

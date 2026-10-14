import { NavLink } from 'react-router-dom'
import { Logo, Icon } from './kit'
import { ThemeToggle } from './ui'
import { useAuth } from './TokenGate'

const tab = ({ isActive }) =>
  `rounded-full px-3.5 py-1.5 text-sm font-semibold transition ${isActive ? 'bg-accent-soft text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`

export default function TopNav({ actions }) {
  const { openTokenDialog } = useAuth()
  return (
    <div className="rise mb-12 flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-5">
        <NavLink to="/" className="flex items-center gap-2.5">
          <Logo className="h-7 w-7" />
          <span className="text-sm font-semibold tracking-tight">tezcatlipoca</span>
        </NavLink>
        <nav className="flex gap-1 rounded-full bg-sunken p-1 shadow-inset" aria-label="Sections">
          <NavLink to="/" end className={tab}>Live</NavLink>
          <NavLink to="/runs" className={tab}>Runs</NavLink>
          <NavLink to="/comps" className={tab}>Comps</NavLink>
        </nav>
      </div>
      <div className="flex items-center gap-3">
        {actions}
        <button onClick={openTokenDialog} title="API token" aria-label="API token" className="btn h-9 w-9 !p-0"><Icon name="gear" /></button>
        <ThemeToggle />
      </div>
    </div>
  )
}

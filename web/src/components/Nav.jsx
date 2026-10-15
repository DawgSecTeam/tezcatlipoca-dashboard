import { NavLink } from 'react-router-dom'
import { Logo } from './kit'
import Bell from './Bell'

const tab = ({ isActive }) =>
  `rounded-full px-3.5 py-1.5 text-sm font-semibold transition ${isActive ? 'bg-accent-soft text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`

// Page header for the standalone pages. The token dialog lives with the 401 handler that
// opens it (TokenGate) and the theme follows the OS — neither belongs in a page header.
// The reset-request bell rides the header everywhere it exists, instead of floating in a
// corner of the viewport.
export default function TopNav({ actions }) {
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
      {/* ml-auto keeps the bell at the right edge when it wraps to its own row on a phone. */}
      <div className="ml-auto flex items-center gap-3">
        {actions}
        <Bell />
      </div>
    </div>
  )
}

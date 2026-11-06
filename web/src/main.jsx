import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import './index.css'
import { initTheme } from './components/ui'
import { ConfirmProvider, ToastProvider } from './components/kit'
import TokenGate from './components/TokenGate'
import Live from './pages/Live'
import Runs from './pages/Runs'
import Comps from './pages/Comps'
import CompLayout from './pages/CompLayout'
import { ScrimRedirect } from './pages/Run'
import Range, { LegacyRun } from './pages/Range'
import Overview from './pages/Overview'
import Injects from './pages/Injects'
import Packet from './pages/Packet'
import Boxes from './pages/Boxes'
import BoxEdit from './pages/BoxEdit'
import PinPage from './pages/PinPage'

// An unmatched URL: say so, and give the operator the way back. Rendering nothing (the old
// behaviour) showed only the token panel, which reads as "logged out" (2026-11-06 audit).
function NotFound() {
  return (
    <div className="mx-auto max-w-xl p-10 text-center">
      <h1 className="display text-3xl">Not found</h1>
      <p className="mt-3 text-sm leading-relaxed text-muted">
        There is no page at <span className="font-mono text-xs">{window.location.pathname}</span>.
      </p>
      <p className="mt-4 flex justify-center gap-3">
        <a className="btn" href="/">Active ranges</a>
        <a className="btn" href="/runs">Runs</a>
        <a className="btn" href="/comps">Competitions</a>
      </p>
    </div>
  )
}

initTheme()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
      <ConfirmProvider>
      <TokenGate>
      <Routes>
        <Route path="/" element={<Live />} />
        <Route path="/runs" element={<Runs />} />
        <Route path="/ranges/:rangeId/*" element={<Range />} />
        <Route path="/scrims/:id" element={<ScrimRedirect />} />
        <Route path="/comps" element={<Comps />} />
        <Route path="/c/:id" element={<CompLayout />}>
          <Route index element={<Overview />} />
          <Route path="runs/:run/*" element={<LegacyRun />} />
          <Route path="injects" element={<Injects />} />
          <Route path="injects/:slug" element={<Injects />} />
          <Route path="packet" element={<Packet />} />
          <Route path="boxes" element={<Boxes />} />
          <Route path="boxes/:box" element={<BoxEdit />} />
          <Route path="boxes/:box/services" element={<PinPage kind="services" />} />
          <Route path="boxes/:box/misconfigs" element={<PinPage kind="misconfigs" />} />
        </Route>
        {/* An unmatched URL used to render the layout with an empty outlet — the token panel and
            nothing else, which reads as "logged out" rather than "wrong address" (2026-11-06). */}
        <Route path="*" element={<NotFound />} />
      </Routes>
      </TokenGate>
      </ConfirmProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)

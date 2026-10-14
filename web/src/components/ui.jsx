import { useEffect } from 'react'

export function Button({ variant = 'default', className = '', ...props }) {
  const v = { default: '', primary: 'btn-primary', danger: 'btn-danger', ghost: 'btn-ghost' }[variant]
  return <button className={`btn ${v} ${className}`} {...props} />
}

export function SearchInput({ value, onChange, placeholder = 'Search…', autoFocus, className = '', inputRef, hotkey }) {
  return (
    <div className={`relative ${className}`}>
      <svg className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
        <circle cx="8.5" cy="8.5" r="5.5" />
        <path d="M13 13l4 4" strokeLinecap="round" />
      </svg>
      <input ref={inputRef} autoFocus={autoFocus} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        onKeyDown={(e) => { if (e.key === 'Escape' && value) { e.preventDefault(); onChange('') } }}
        className="field rounded-full pl-11 pr-10" />
      {value
        ? <button type="button" onClick={() => onChange('')} className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full px-1.5 text-faint hover:text-fg" aria-label="Clear">✕</button>
        : hotkey && <kbd className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2">{hotkey}</kbd>}
    </div>
  )
}

export function Field({ label, children, hint }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-faint">{hint}</span>}
    </label>
  )
}

export const inputCls = 'field'

export function Card({ className = '', ...props }) {
  return <div className={`panel ${className}`} {...props} />
}

export function ErrorBanner({ error, onClose }) {
  if (!error) return null
  return (
    <div className="mb-4 flex items-start justify-between gap-3 rounded-2xl bg-danger-soft px-4 py-2.5 text-sm text-danger">
      <span className="whitespace-pre-wrap">{String(error.message || error)}</span>
      {onClose && <button onClick={onClose} className="opacity-70 hover:opacity-100">✕</button>}
    </div>
  )
}

export function Modal({ open, onClose, title, children, wide }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && !e.defaultPrevented && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-bg/60 p-4 pt-[9vh] backdrop-blur-sm" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`panel rise w-full ${wide ? 'max-w-3xl' : 'max-w-md'}`} role="dialog" aria-modal="true">
        <div className="flex items-center justify-between px-6 pt-5 pb-2">
          <h2 className="display text-2xl">{title}</h2>
          <button onClick={onClose} className="btn btn-ghost h-8 w-8 !p-0 text-faint">✕</button>
        </div>
        <div className="px-6 pb-6 pt-2">{children}</div>
      </div>
    </div>
  )
}

export function PlatformBadge({ platform }) {
  const c = platform === 'windows' ? 'var(--win)' : platform === 'linux' ? 'var(--lin)'
    : platform === 'firewall' ? 'var(--fw)' : 'var(--muted)'
  return (
    <span className="chip gap-1.5 bg-sunken text-muted">
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: c }} />{platform}
    </span>
  )
}

export function Spinner() {
  return <div className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-accent" />
}

// ── theme ────────────────────────────────────────────────────────────────────────────────

// The theme follows the OS. The override outlived the toggle that set it, so a stored
// preference is gone by design — an ops board has no business spending a header control on
// a preference.
function systemTheme() {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}
export function initTheme() {
  document.documentElement.dataset.theme = systemTheme()
}

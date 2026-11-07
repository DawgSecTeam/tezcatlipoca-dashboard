import { createContext, useContext, useEffect, useState } from 'react'
import { TOKEN_CHANGED_EVENT, getToken, setToken } from '../api'
import { Button, Field, Modal, inputCls } from './ui'

const AuthCtx = createContext({})
export const useAuth = () => useContext(AuthCtx)

// Wraps the app: shows the token dialog when no token is stored, and again whenever a
// request comes back 401 (`tez-unauthorized` from api.js). The 401 handler only opens the
// dialog — deleting the stored token is the Clear button's alone.
export default function TokenGate({ children }) {
  const [open, setOpen] = useState(!getToken())
  const [value, setValue] = useState('')
  useEffect(() => {
    const onUnauthorized = () => setOpen(true)
    window.addEventListener('tez-unauthorized', onUnauthorized)
    return () => window.removeEventListener('tez-unauthorized', onUnauthorized)
  }, [])
  const show = () => { setValue(getToken()); setOpen(true) }
  const save = (e) => {
    e.preventDefault()
    setToken(value.trim())
    setOpen(false)
    // Whatever page is under this dialog fetched with the old (or missing) token: tell it
    // to re-run its loader, so a saved token clears the unauthorized banner and fills the
    // counts without a manual reload (2026-11-06 audit).
    window.dispatchEvent(new CustomEvent(TOKEN_CHANGED_EVENT))
  }
  return (
    <AuthCtx.Provider value={{ openTokenDialog: show }}>
      {children}
      <Modal open={open} onClose={() => setOpen(false)} title="API token">
        <form className="space-y-4" onSubmit={save}>
          <p className="text-sm leading-relaxed text-muted">
            Every dashboard request carries <code className="font-mono text-xs">Authorization: Bearer …</code>.
          </p>
          <Field label="Token">
            <input className={`${inputCls} font-mono`} type="password" autoFocus placeholder="paste the bearer token"
              value={value} onChange={(e) => setValue(e.target.value)} />
          </Field>
          <div className="flex justify-end gap-3">
            <Button type="button" onClick={() => { setToken(''); setValue('') }}>Clear</Button>
            <Button variant="primary">Save token</Button>
          </div>
        </form>
      </Modal>
    </AuthCtx.Provider>
  )
}

import { createContext, useContext, useEffect, useState } from 'react'
import { getToken, setToken } from '../api'
import { Button, Field, Modal, inputCls } from './ui'

const AuthCtx = createContext({})
export const useAuth = () => useContext(AuthCtx)

// Wraps the app: shows the token dialog when no token is stored, and again whenever a
// request comes back 401 (`tez-unauthorized` from api.js).
export default function TokenGate({ children }) {
  const [open, setOpen] = useState(!getToken())
  const [value, setValue] = useState('')
  useEffect(() => {
    const onUnauthorized = () => setOpen(true)
    window.addEventListener('tez-unauthorized', onUnauthorized)
    return () => window.removeEventListener('tez-unauthorized', onUnauthorized)
  }, [])
  const show = () => { setValue(getToken()); setOpen(true) }
  return (
    <AuthCtx.Provider value={{ openTokenDialog: show }}>
      {children}
      <Modal open={open} onClose={() => setOpen(false)} title="API token">
        <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); setToken(value.trim()); setOpen(false) }}>
          <p className="text-sm leading-relaxed text-muted">
            Every dashboard request carries <code className="font-mono text-xs">Authorization: Bearer …</code>.
          </p>
          <Field label="Token">
            <input className={`${inputCls} font-mono`} type="password" autoFocus placeholder="tez-…"
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

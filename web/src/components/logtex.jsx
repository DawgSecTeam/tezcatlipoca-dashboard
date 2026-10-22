// Log text rendering shared by the run page's Output panel and the Runs drawer's log view.
// The engine log is raw terminal output: SGR color escapes arrive as bytes and used to render
// as literal "Ø[0m" garbage, and the urllib3 warning block repeats unchanged for hours. This
// module parses the escapes into styled spans and folds long identical repeats into one block
// with an ×N marker, so the lines a human needs stay visible.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Spinner } from './ui'

// One tokenizer pass for everything: SGR sequences carry their payload (m[1]); every other
// escape (cursor moves, OSC titles, two-byte introducers) is dropped.
const ESC_RE = /\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b./g

const FG = { 30: 'text-faint', 31: 'text-danger', 32: 'text-accent', 33: 'text-lin', 34: 'text-win', 35: 'text-eng', 36: 'text-accent', 91: 'text-danger', 92: 'text-accent', 93: 'text-lin', 94: 'text-win', 95: 'text-eng', 96: 'text-accent' }

// Turn one raw line into [{t, cls}] spans. SGR state is a tiny flag bag; class strings are
// merged so consecutive segments under one style become one span.
function parseLine(raw) {
  const spans = []
  const st = { fg: '', bold: false, dim: false, italic: false, ul: false }
  const cls = () => `${st.fg} ${st.bold ? 'font-semibold' : ''} ${st.dim ? 'opacity-60' : ''} ${st.italic ? 'italic' : ''} ${st.ul ? 'underline' : ''}`.trim()
  let buf = '', last = 0, m
  ESC_RE.lastIndex = 0
  while ((m = ESC_RE.exec(raw))) {
    buf += raw.slice(last, m.index)
    last = ESC_RE.lastIndex
    if (m[1] === undefined) continue // a non-color escape: drop, emit nothing
    for (const code of (m[1] || '0').split(';').map(Number)) {
      if (code === 0) { st.fg = ''; st.bold = st.dim = st.italic = st.ul = false }
      else if (code === 1) st.bold = true
      else if (code === 2) st.dim = true
      else if (code === 3) st.italic = true
      else if (code === 4) st.ul = true
      else if (code === 22) { st.bold = st.dim = false }
      else if (code === 23) st.italic = false
      else if (code === 24) st.ul = false
      else if (FG[code]) st.fg = FG[code]
      else if (code === 39) st.fg = ''
      // backgrounds (40-47/100-107) are rare in engine output and left unstyled on purpose.
    }
    if (buf) { spans.push({ t: buf, cls: cls() }); buf = '' }
  }
  buf += raw.slice(last)
  if (buf) spans.push({ t: buf, cls: cls() })
  return spans.length ? spans : [{ t: '', cls: '' }]
}

// The engine stamps its own lines "[18:39:44] [templates] ok" — dim the clock, mute the module.
const TS = /^(\[\d{1,2}:\d{2}:\d{2}\])(\s*\[([^\]]{1,40})\])?/
function decorate(spans) {
  const first = spans[0]
  if (!first || !first.t) return spans
  const m = TS.exec(first.t)
  if (!m) return spans
  const out = [{ t: m[1], cls: 'text-faint' }]
  if (m[2]) out.push({ t: m[2], cls: 'text-muted' })
  const rest = first.t.slice(m[0].length)
  if (rest) out.push({ t: rest, cls: first.cls })
  return [...out, ...spans.slice(1)]
}

const plainOf = (spans) => spans.map((s) => s.t).join('')

// Fold consecutive identical blocks (a single line, or a short repeating multi-line block like
// the urllib3 warning) into one instance. Blank lines never anchor a fold.
function collapse(lines) {
  const out = []
  let i = 0
  while (i < lines.length) {
    let folded = false
    for (let k = 1; k <= 8 && i + 2 * k <= lines.length; k++) {
      const block = lines.slice(i, i + k).map((l) => l.plain)
      if (!block.some((s) => s.trim())) break
      const sig = block.join('\n')
      let n = 1
      while (lines.slice(i + n * k, i + (n + 1) * k).map((l) => l.plain).join('\n') === sig) n++
      if (n < 3) continue
      const kept = lines.slice(i, i + k)
      kept[k - 1].repeats = n - 1
      out.push(...kept)
      i += n * k
      folded = true
      break
    }
    if (!folded) { out.push(lines[i]); i++ }
  }
  return out
}

// text → [{spans, plain, repeats}] — memoize at the call site; this scans the whole log.
export function parseLog(text) {
  const lines = (text || '').split('\n').map((raw) => {
    const spans = decorate(parseLine(raw))
    return { spans, plain: plainOf(spans) }
  })
  return collapse(lines)
}

// "phase 6/8 repair_sweep" reads like an internal marker; the display form is "6/8 · Repair sweep".
const PHASE_STR = /^phase\s+(\d+)\/(\d+)\s+(.+)$/i
export function prettyStage(s) {
  const m = PHASE_STR.exec(s || '')
  if (!m) return s || ''
  const name = m[3].replace(/_/g, ' ')
  return `${m[1]}/${m[2]} · ${name.charAt(0).toUpperCase()}${name.slice(1)}`
}

// Deploy phases announce themselves as "[3/8] …" or "phase 3/8"; both become exact jump
// targets because every processed line is its own element.
const PHASE_RE = /\[(\d+)\/(\d+)\]|phase\s+(\d+)\s*\/\s*(\d+)/i
export function phaseMarks(lines) {
  const out = [], seen = new Set()
  lines.forEach((l, i) => {
    const m = PHASE_RE.exec(l.plain)
    if (!m) return
    const n = m[1] ?? m[3]
    if (seen.has(n)) return
    seen.add(n)
    out.push({ n, total: m[2] ?? m[4], line: i })
  })
  return out
}

const stripEsc = (t) => t.replace(ESC_RE, '')

// The log body: one element per processed line, so jumps land exactly (the old ratio-based
// phase jump drifted whenever wrapped lines changed the page height). Follow, search with hit
// navigation and a jump-to-latest button live here; the drawer and the run page share it.
// `focus` ({line}, 1-based) is the diagnosis panel's jump target — a fresh object each click,
// so the same line asked for twice scrolls again.
export function LogView({ log, height = '60vh', showPhases = false, focus = null }) {
  const [follow, setFollow] = useState(true)
  const [q, setQ] = useState('')
  const [hit, setHit] = useState(0)
  const pre = useRef()
  const lines = useMemo(() => parseLog(log.text), [log.text])
  const marks = useMemo(() => (showPhases ? phaseMarks(lines) : []), [lines, showPhases])
  const hits = useMemo(() => {
    if (!q.trim()) return []
    const needle = q.toLowerCase()
    return lines.map((l, i) => (l.plain.toLowerCase().includes(needle) ? i : -1)).filter((i) => i >= 0)
  }, [lines, q])

  useEffect(() => { if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [log.text, follow])

  const scrollToLine = (i) => {
    const el = pre.current
    const child = el?.children[i]
    if (!el || !child) return
    setFollow(false)
    el.scrollTop = child.offsetTop - el.offsetTop - 8
  }

  // The server's line numbers are the raw log's, and parseLog yields one element per line,
  // so the mapping is the same index minus one. The log may still be streaming when the
  // click lands; the guard makes a not-yet-loaded line a no-op instead of a crash.
  useEffect(() => {
    if (focus?.line) scrollToLine(focus.line - 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus])

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        {log.running ? <><Spinner /><span className="text-muted">Running…</span></>
          : <span className={`chip px-3 py-1 ${log.returncode === 0 ? 'bg-accent-soft text-accent' : 'bg-danger-soft text-danger'}`}>
              {log.returncode === 0 ? 'Finished' : `Failed · exit ${log.returncode}`}
            </span>}
        {showPhases && marks.map((m) => (
          <button key={m.n} onClick={() => scrollToLine(m.line)} title={`Jump to phase ${m.n} of ${m.total}`}
            className="chip bg-sunken px-2 py-0.5 font-mono text-[11px] text-muted transition hover:text-accent">
            phase {m.n}/{m.total}
          </button>
        ))}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {q && <span className="text-[11px] tabular-nums text-faint">{hits.length ? `${hit + 1}/${hits.length}` : 'no hits'}</span>}
          <input value={q} onChange={(e) => { setQ(e.target.value); setHit(0) }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && hits.length) { e.preventDefault(); scrollToLine(hits[hit % hits.length]); setHit((h) => (h + 1) % hits.length) }
              if (e.key === 'Escape') { setQ(''); setHit(0) }
            }}
            placeholder="Search the log…" className="field !w-44 !rounded-full !py-1.5 text-xs" />
          {!follow && (
            <button onClick={() => { setFollow(true); if (pre.current) pre.current.scrollTop = pre.current.scrollHeight }}
              className="chip bg-sunken px-2.5 py-1 text-xs text-muted transition hover:text-accent" title="Jump to the end and follow">
              Latest ↓
            </button>
          )}
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-[var(--accent)]" /> Follow
          </label>
        </div>
      </div>
      <pre ref={pre} style={{ height }}
        className="relative overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">
        {lines.map((l, i) => (
          <div key={i} className={q && hits.includes(i) ? 'rounded bg-accent-soft/60' : undefined}>
            {l.spans.map((s, j) => <span key={j} className={s.cls}>{s.t}</span>)}
            {l.repeats > 1 && <span className="ml-2 italic text-faint" title={`${l.repeats} more identical lines folded`}>··· ×{l.repeats}</span>}
          </div>
        ))}
        {!lines.length && ' '}
      </pre>
    </div>
  )
}

// A cheap tail for card-sized views: same parser, no folding chrome.
export function LogText({ text, className = '', children }) {
  const lines = useMemo(() => parseLog(text), [text])
  return (
    <pre className={className}>
      {children}
      {lines.map((l, i) => (
        <div key={i}>
          {l.spans.map((s, j) => <span key={j} className={s.cls}>{s.t}</span>)}
          {l.repeats > 1 && <span className="ml-2 italic text-faint">··· ×{l.repeats}</span>}
        </div>
      ))}
    </pre>
  )
}

export { stripEsc }

// A run's triage answer, rendered as a panel: the sentence that says what broke, the log
// line it is based on, and what to do next. The verdict is computed server-side from local
// facts (dashboard/diagnose.py), so this panel is instant and reproducible — no model, no
// polling, and the same JSON over MCP when an agent asks the same question.
import { Card } from './ui'
import { Icon, agoMs, fmtDur } from './kit'

const TONE = {
  error: { chip: 'bg-danger-soft text-danger', dot: 'bg-danger', label: 'Needs attention' },
  warning: { chip: 'bg-sunken text-lin', dot: 'bg-lin', label: 'Check' },
  info: { chip: 'bg-sunken text-muted', dot: 'bg-muted', label: 'Info' },
  ok: { chip: 'bg-accent-soft text-accent', dot: 'bg-accent', label: 'Healthy' },
}

const SOURCE_LABEL = { 'run-log': 'run log', 'run-summary': 'runner summary',
  'run-record': 'run record', event: 'event' }

// One finding: what (title), why (detail) and the next move (advice). The primary finding
// repeats the headline on purpose — the panel is also read one finding at a time.
function Finding({ finding, onFocusLine }) {
  const tone = TONE[finding.severity] || TONE.info
  const quote = finding.evidence?.find((e) => e.quote)
  return (
    <li className="rounded-2xl border border-line/70 p-4">
      <div className="flex items-baseline gap-2">
        <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} />
        <h3 className="min-w-0 text-sm font-semibold text-fg">{finding.title}</h3>
      </div>
      {finding.detail && <p className="mt-1 text-xs leading-relaxed text-muted">{finding.detail}</p>}
      {quote && (
        <div className="mt-2 rounded-xl bg-sunken px-3 py-2 font-mono text-[11px] leading-relaxed text-fg">
          <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-faint">
            <span>{SOURCE_LABEL[quote.source] || quote.source}{quote.line ? ` · line ${quote.line}` : ''}</span>
            {quote.line != null && onFocusLine && (
              <button onClick={() => onFocusLine(quote.line)} className="ml-auto normal-case tracking-normal text-accent hover:underline">
                Jump to log
              </button>
            )}
          </div>
          <div className="whitespace-pre-wrap break-words">{quote.quote}</div>
        </div>
      )}
      {finding.advice && (
        <p className="mt-2 text-xs leading-relaxed text-muted">
          <span className="font-semibold uppercase tracking-wider text-faint">Next: </span>{finding.advice}
        </p>
      )}
    </li>
  )
}

export default function Diagnosis({ diagnosis, onFocusLine }) {
  if (!diagnosis) return null
  const tone = TONE[diagnosis.severity] || TONE.info
  const findings = diagnosis.findings || []
  const facts = diagnosis.facts || {}
  const factsLine = [
    facts.returncode != null ? `rc ${facts.returncode}` : null,
    facts.duration_s != null ? fmtDur(facts.duration_s) : null,
    diagnosis.stage?.index != null ? `phase ${diagnosis.stage.index}/${diagnosis.stage.total}` : null,
    facts.engine_head ? `engine ${String(facts.engine_head).slice(0, 12)}` : null,
    facts.workspace_missing ? 'workspace missing' : null,
  ].filter(Boolean)

  return (
    <Card className="p-6">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="label !mb-0">Diagnosis</h2>
        <span className={`chip px-2.5 py-1 ${tone.chip}`}>{tone.label}</span>
        {diagnosis.as_of != null && (
          <span className="ml-auto text-[11px] text-faint" title="Computed locally from the run record, its events and its log">
            computed {agoMs(diagnosis.as_of) || 'just now'}
          </span>
        )}
      </div>
      <p className="text-sm font-semibold leading-relaxed text-fg">{diagnosis.headline}</p>

      {findings.length > 0 && (
        <ul className="mt-3 space-y-2">
          {findings.map((finding, i) => (
            <Finding key={`${finding.code}-${finding.run_id || ''}-${i}`} finding={finding} onFocusLine={onFocusLine} />
          ))}
        </ul>
      )}

      {factsLine.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-x-1.5 gap-y-1 font-mono text-[11px] text-faint">
          <Icon name="terminal" className="mr-1.5 h-3.5 w-3.5" />
          {factsLine.map((fact, i) => (
            <span key={i}>
              {i > 0 && <span aria-hidden="true" className="text-line"> · </span>}
              {fact}
            </span>
          ))}
        </div>
      )}
    </Card>
  )
}

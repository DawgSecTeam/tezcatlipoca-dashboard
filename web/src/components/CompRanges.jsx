// The competition's ranges: a competition is normally deployed several times at once, one
// workspace per range, and nothing else on this page lists them. The Deploy panel's own
// `deployed` flag reads the definitions checkout, not the ranges the service has built.
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { Card } from './ui'
import { Icon, agoMs } from './kit'
import { rangeStatus, rangeTitle, shortRangeId } from '../rangeView'

const STATE_CLS = {
  live: 'bg-accent-soft text-accent', building: 'bg-accent-soft text-accent',
  teardown_failed: 'bg-danger-soft text-danger', failed: 'bg-danger-soft text-danger',
  retired: 'bg-sunken text-muted',
}
const STATE_TEXT = {
  live: 'deployed', building: 'building', teardown_failed: 'teardown incomplete',
  failed: 'deploy failed', retired: 'retired',
}

export default function CompRanges({ compId }) {
  const [ranges, setRanges] = useState(null)
  // The board payload, not /api/ranges: the same one-line status the Live board computes
  // (it carries the recorded counts, active runs and pending requests that status needs).
  useEffect(() => { api.rangeBoard().then(setRanges).catch(() => setRanges([])) }, [compId])
  const mine = (ranges || []).filter((range) => range.comp_id === compId)
  return (
    <Card className="rise rise-4 p-6">
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h3 className="label !mb-0">Ranges</h3>
        <span className="text-xs text-muted">{ranges === null ? 'Loading…' : `${mine.length} range${mine.length === 1 ? '' : 's'} of this competition`}</span>
      </div>
      {!mine.length
        ? <p className="text-xs text-faint">No range of this competition has been deployed through this dashboard.</p>
        : <ul className="space-y-2">{mine.map((range) => (
          <li key={range.id}>
            <Link to={`/ranges/${range.id}`} className="tile tile-hover flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-sm">
              <span className={`chip shrink-0 px-2.5 py-0.5 ${STATE_CLS[range.state] || 'bg-sunken text-muted'}`}>{STATE_TEXT[range.state] || range.state}</span>
              <span className="font-medium">{rangeTitle(range)}</span>
              <span className="font-mono text-[11px] text-faint">{shortRangeId(range)}</span>
              <span className="text-xs text-muted">{rangeStatus(range).label}</span>
              <span className="ml-auto text-xs text-faint">{range.deployed_at ? agoMs(range.deployed_at) : ''}</span>
              <Icon name="chevron" className="h-3.5 w-3.5 shrink-0 text-faint" />
            </Link>
          </li>
        ))}</ul>}
    </Card>
  )
}

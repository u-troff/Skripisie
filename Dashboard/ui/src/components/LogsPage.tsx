import { useEffect, useState } from 'react'
import { frameUrl } from '../frameUrl'
import { fetchLog, fetchLogs, fetchRunTrace, setRunFlag } from '../net'
import type { CheckRecord, RunFlag, RunReport, RunSummary, RunTrace } from '../types'
import { RoomMapView } from './RoverMap'

function fmtTime(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString()
}

function fmtNum(value: number | null | undefined, digits = 1): string {
  return typeof value === 'number' ? value.toFixed(digits) : '—'
}

function outcomeClass(outcome: string | null | undefined): string {
  if (outcome === 'completed') return 'outcome go'
  if (outcome === 'halted' || outcome === 'aborted') return 'outcome stop'
  return 'outcome'
}

type RoverFilter = 'all' | 'virtual' | 'pi'
const ROVER_FILTERS: RoverFilter[] = ['all', 'virtual', 'pi']
const RUN_FLAGS: RunFlag[] = ['good', 'bad', 'review']

export default function LogsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [roverFilter, setRoverFilter] = useState<RoverFilter>('all')
  const [flaggedOnly, setFlaggedOnly] = useState(false)
  const [loadingList, setLoadingList] = useState(true)
  const [listError, setListError] = useState<string | null>(null)

  const [selected, setSelected] = useState<string | null>(null)
  const [report, setReport] = useState<RunReport | null>(null)
  const [loadingReport, setLoadingReport] = useState(false)
  const [reportError, setReportError] = useState<string | null>(null)

  const [trace, setTrace] = useState<RunTrace | null>(null)
  const [loadingTrace, setLoadingTrace] = useState(false)

  const loadRuns = () => {
    setLoadingList(true)
    setListError(null)
    fetchLogs()
      .then(setRuns)
      .catch((err) => setListError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingList(false))
  }

  useEffect(loadRuns, [])

  useEffect(() => {
    if (!selected) return
    setLoadingReport(true)
    setReportError(null)
    setReport(null)
    fetchLog(selected)
      .then(setReport)
      .catch((err) => setReportError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingReport(false))
  }, [selected])

  useEffect(() => {
    if (!selected) return undefined
    let cancelled = false
    setLoadingTrace(true)
    setTrace(null)
    void fetchRunTrace(selected).then((result) => {
      if (cancelled) return
      setTrace(result)
      setLoadingTrace(false)
    })
    return () => {
      cancelled = true
    }
  }, [selected])

  const lastStep = trace?.steps[trace.steps.length - 1]
  const countFor = (filter: RoverFilter) =>
    filter === 'all' ? runs.length : runs.filter((run) => run.rover === filter).length
  const shownRuns = runs.filter(
    (run) => (roverFilter === 'all' || run.rover === roverFilter) && (!flaggedOnly || run.flag),
  )
  const selectedRun = runs.find((run) => run.session_id === selected)

  // The route actually driven: an approach follows its planned waypoints
  // (around obstacles), while move/turn go straight to where they ended. Joining
  // only each step's END pose would draw straight lines through furniture.
  const route: Array<[number, number]> = []
  const collisions: Array<[number, number]> = []
  if (trace) {
    route.push([trace.start.x, trace.start.y])
    for (const step of trace.steps) {
      if (step.planned_path && step.planned_path.length > 1) {
        route.push(...step.planned_path.slice(1))
      } else {
        route.push([step.pose.x, step.pose.y])
      }
      if (step.reason === 'collision') collisions.push([step.pose.x, step.pose.y])
    }
  }

  return (
    <main className="wrap wide">
      <header>
        <h1>Run logs</h1>
        <div className="row" style={{ marginBottom: 0 }}>
          <button className="ghost" onClick={loadRuns} disabled={loadingList}>
            {loadingList ? 'Refreshing…' : 'Refresh'}
          </button>
          <a className="navlink" href="#/">
            ← Live
          </a>
        </div>
      </header>

      <section className="panel">
        <h2>Run map</h2>
        {!selected && (
          <p className="note" style={{ marginTop: 0 }}>
            Select a run below to see the room and the route the rover took.
          </p>
        )}
        {selected && loadingTrace && (
          <p className="note" style={{ marginTop: 0 }}>
            Loading…
          </p>
        )}
        {selected && !loadingTrace && !trace && (
          <p className="note" style={{ marginTop: 0 }}>
            No recorded map for this run — only virtual-rover runs with a saved trace have one.
          </p>
        )}
        {trace && (
          <RoomMapView
            room={trace.room}
            robot={{
              length_m: trace.settings.robot_length_m,
              width_m: trace.settings.robot_width_m,
              clearance_m: trace.settings.clearance_m,
            }}
            travelled={route}
            planned={null}
            collisions={collisions}
            pose={lastStep?.pose ?? trace.start}
            footer={
              lastStep
                ? `${trace.room.name} · odometer ${lastStep.odometer_m.toFixed(2)}m · sim time ${lastStep.sim_time_s.toFixed(1)}s`
                : trace.room.name
            }
          />
        )}
      </section>

      <section className="panel">
        <h2>
          Runs ({shownRuns.length}
          {shownRuns.length !== runs.length ? ` of ${runs.length}` : ''})
        </h2>
        <div className="filterbar" role="group" aria-label="Filter runs by rover">
          {ROVER_FILTERS.map((filter) => (
            <button
              key={filter}
              className={`ghost ${roverFilter === filter ? 'active' : ''}`}
              onClick={() => setRoverFilter(filter)}
            >
              {filter} ({countFor(filter)})
            </button>
          ))}
          <button
            className={`ghost ${flaggedOnly ? 'active' : ''}`}
            onClick={() => setFlaggedOnly((on) => !on)}
          >
            flagged ({runs.filter((run) => run.flag).length})
          </button>
        </div>
        {listError && <pre className="error">{listError}</pre>}
        {!listError && runs.length > 0 && shownRuns.length === 0 && (
          <p className="note" style={{ marginTop: 0 }}>
            No {roverFilter} runs in the logs.
          </p>
        )}
        {!listError && runs.length === 0 && !loadingList && (
          <p className="note" style={{ marginTop: 0 }}>
            No run logs yet — Dashboard/brain/logs/ is empty.
          </p>
        )}
        <div className="runlist">
          {shownRuns.map((run) => (
            <button
              key={run.session_id}
              className={`runcard ${selected === run.session_id ? 'active' : ''}`}
              onClick={() => setSelected(run.session_id)}
            >
              <div className="runcard-top">
                <span className={outcomeClass(run.outcome)}>{run.outcome ?? 'unknown'}</span>
                {run.flag && <span className={`flagbadge ${run.flag}`}>{run.flag}</span>}
                <span className="sid">{run.session_id}</span>
              </div>
              <p className="runcard-cmd">{run.command || <em>(no command)</em>}</p>
              <dl className="runcard-stats">
                <div>
                  <dt>rover</dt>
                  <dd>{run.rover ?? '—'}</dd>
                </div>
                <div>
                  <dt>steps</dt>
                  <dd>{run.step_count}</dd>
                </div>
                <div>
                  <dt>ok/skip/fail</dt>
                  <dd>
                    {run.checks_completed ?? 0}/{run.checks_skipped ?? 0}/{run.checks_failed ?? 0}
                  </dd>
                </div>
                <div>
                  <dt>duration</dt>
                  <dd>{fmtNum(run.total_s)}s</dd>
                </div>
                <div>
                  <dt>revisions</dt>
                  <dd>{run.revision_count}</dd>
                </div>
                <div>
                  <dt>target</dt>
                  <dd>{run.target_confirmed ? 'found' : '—'}</dd>
                </div>
              </dl>
              <span className="runcard-time">{fmtTime(run.mtime)}</span>
            </button>
          ))}
        </div>
      </section>

      {selected && (
        <section className="panel">
          <h2>Run {selected}</h2>
          {selectedRun && <FlagControls run={selectedRun} onChanged={loadRuns} />}
          {loadingReport && (
            <p className="note" style={{ marginTop: 0 }}>
              Loading…
            </p>
          )}
          {reportError && <pre className="error">{reportError}</pre>}
          {report && <RunDetail report={report} />}
        </section>
      )}
    </main>
  )
}

/** Mark a run good / bad / review with a note, so it can be found again via the
 * "flagged" filter. Stored beside the logs (run_flags.json), not in them. */
function FlagControls({ run, onChanged }: { run: RunSummary; onChanged: () => void }) {
  const [note, setNote] = useState(run.flag_note ?? '')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setNote(run.flag_note ?? '')
  }, [run.session_id, run.flag_note])

  const save = (flag: RunFlag | null) => {
    setError(null)
    setRunFlag(run.session_id, flag, note)
      .then(onChanged)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div className="filterbar" role="group" aria-label="Flag this run">
        {RUN_FLAGS.map((flag) => (
          <button
            key={flag}
            className={`ghost ${run.flag === flag ? 'active' : ''}`}
            onClick={() => save(run.flag === flag ? null : flag)}
          >
            {flag}
          </button>
        ))}
        {run.flag && (
          <button className="ghost" onClick={() => save(run.flag as RunFlag)}>
            save note
          </button>
        )}
      </div>
      <textarea
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Why is this run worth coming back to?"
        rows={3}
        style={{ width: '100%' }}
      />
      {error && <pre className="error">{error}</pre>}
    </div>
  )
}

function FrameThumb({ path, size = 96 }: { path?: string | null; size?: number }) {
  const url = frameUrl(path)
  if (!url) return <span>—</span>
  return (
    <a href={url} target="_blank" rel="noreferrer">
      <img src={url} alt="" loading="lazy" style={{ width: size, height: 'auto', borderRadius: 4, display: 'block' }} />
    </a>
  )
}

function RunDetail({ report }: { report: RunReport }) {
  const timings = report.timings || {}
  const confirmedCheck = report.target_confirmed

  return (
    <>
      <p className="transcript">{report.command}</p>
      {report.resolved_command && report.resolved_command !== report.command && (
        <p className="note" style={{ marginTop: 0 }}>
          Resolved: {report.resolved_command}
        </p>
      )}

      <dl>
        <div>
          <dt>outcome</dt>
          <dd>{report.outcome}</dd>
        </div>
        <div>
          <dt>rover</dt>
          <dd>{report.rover}</dd>
        </div>
        <div>
          <dt>plan revised</dt>
          <dd>{report.plan_was_revised ? 'yes' : 'no'}</dd>
        </div>
        <div>
          <dt>total time</dt>
          <dd>{fmtNum(timings.total_s)}s</dd>
        </div>
        <div>
          <dt>vlm mean/max</dt>
          <dd>
            {fmtNum(timings.vlm_latency_mean_s)}s / {fmtNum(timings.vlm_latency_max_s)}s
          </dd>
        </div>
        <div>
          <dt>checks ok/skip/fail</dt>
          <dd>
            {timings.checks_completed ?? 0}/{timings.checks_skipped ?? 0}/{timings.checks_failed ?? 0}
          </dd>
        </div>
      </dl>

      {report.dialogue && (
        <>
          <h3>Clarification (RQ1)</h3>
          <dl>
            <div>
              <dt>turns used</dt>
              <dd>{report.dialogue.turn_count ?? '—'}</dd>
            </div>
            <div>
              <dt>capped</dt>
              <dd>{report.dialogue.capped ? 'yes — hit MAX_CLARIFYING_TURNS' : 'no'}</dd>
            </div>
            <div>
              <dt>verified</dt>
              <dd>
                {report.dialogue.verified === null || report.dialogue.verified === undefined
                  ? '—'
                  : report.dialogue.verified
                    ? 'yes'
                    : 'no'}
              </dd>
            </div>
          </dl>
          {report.dialogue.concerns && <p className="warn">{report.dialogue.concerns}</p>}
        </>
      )}

      <h3>Plan steps ({report.steps?.length ?? 0})</h3>
      <div className="logtable-wrap">
        <table className="logtable">
          <thead>
            <tr>
              <th>#</th>
              <th>action</th>
              <th>target</th>
              <th>status</th>
              <th>elapsed</th>
            </tr>
          </thead>
          <tbody>
            {(report.steps || []).map((step) => (
              <tr key={step.index}>
                <td>{step.index}</td>
                <td>{step.action}</td>
                <td className="tgt-cell">{step.target}</td>
                <td>{step.status}</td>
                <td>{fmtNum(step.elapsed_s)}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {confirmedCheck && (
        <>
          <h3>Target confirmed</h3>
          <p className="outcome go">
            "{confirmedCheck.target}" seen looking {confirmedCheck.aimed} at ~
            {fmtNum(confirmedCheck.est_distance_cm, 0)}cm ({fmtNum(confirmedCheck.t_rel_s)}s in) — rover
            halted.
          </p>
        </>
      )}

      <h3>Camera checks ({report.checks?.length ?? 0})</h3>
      <div className="logtable-wrap">
        <table className="logtable">
          <thead>
            <tr>
              <th>frame</th>
              <th>kind</th>
              <th>aimed</th>
              <th>t</th>
              <th>~cm</th>
              <th>latency</th>
              <th>target?</th>
              <th>path clear?</th>
              <th>unexpected</th>
            </tr>
          </thead>
          <tbody>
            {(report.checks || []).map((check: CheckRecord, i) => (
              <tr
                key={i}
                className={
                  confirmedCheck != null && check.frame_path === confirmedCheck.frame_path
                    ? 'hit'
                    : ''
                }
              >
                <td><FrameThumb path={check.frame_path} /></td>
                <td>{check.kind}</td>
                <td>{check.aimed ?? check.action ?? check.reason ?? '—'}{check.gate ? ` · ${check.gate}` : ''}</td>
                <td>{fmtNum(check.t_rel_s)}s</td>
                <td>{fmtNum(check.est_distance_cm, 0)}</td>
                <td>{fmtNum(check.latency_s)}s</td>
                <td>
                  {check.result?.target_visible === undefined
                    ? '—'
                    : check.result.target_visible
                      ? 'yes'
                      : 'no'}
                </td>
                <td>
                  {check.result?.path_clear === undefined
                    ? '—'
                    : check.result.path_clear
                      ? 'yes'
                      : 'no'}
                </td>
                <td>{(check.result?.unexpected || []).join(', ') || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {report.arrival && (
        <>
          <h3>Arrival check</h3>
          <p className="note" style={{ marginTop: 0 }}>
            {report.arrival.result?.description}
          </p>
          <FrameThumb path={report.arrival.frame_path} size={240} />
          <dl>
            <div>
              <dt>target visible</dt>
              <dd>{report.arrival.result?.target_visible ? 'yes' : 'no'}</dd>
            </div>
            <div>
              <dt>confidence</dt>
              <dd>{report.arrival.result?.confidence ?? '—'}</dd>
            </div>
            <div>
              <dt>missing</dt>
              <dd>{(report.arrival.result?.missing || []).join(', ') || '—'}</dd>
            </div>
          </dl>
        </>
      )}

      {report.revisions && report.revisions.length > 0 && (
        <>
          <h3>Revisions (RQ2)</h3>
          <ul className="digest">
            {report.revisions.map((rev, i) => (
              <li key={i}>
                <strong>{rev.kind}</strong> — {rev.reason} ({rev.applied ? 'applied' : 'not applied'})
              </li>
            ))}
          </ul>
        </>
      )}

      {report.bends && report.bends.length > 0 && (
        <>
          <h3>Bends taken</h3>
          <p className="note" style={{ marginTop: 0 }}>
            {report.route_summary}
          </p>
        </>
      )}

      {report.digest && report.digest.length > 0 && (
        <>
          <h3>Scene digest</h3>
          <ul className="digest">
            {report.digest.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}

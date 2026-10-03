import { useStore } from '../store'

function thumbUrl(framePath?: string | null): string | null {
    if (!framePath) return null
    // frame_path is like "logs/frames/<session_id>/ap_03_check.jpg"
    const idx = framePath.indexOf('logs/frames/')
    return idx === -1 ? null : '/' + framePath.slice(idx)
}

export default function ApproachDebugPanel() {
    const checks = useStore((state) => state.mission.liveChecks)
    if (checks.length === 0) return null

    return (
        <section className="panel">
            <h2>Live frame trace</h2>
            <div className="frametrace">
                {[...checks].reverse().map((check, index) => {
                    const url = thumbUrl(check.frame_path)
                    return (
                        <div key={index} className="frametrace-row">
                            {url && <img src={url} alt="" className="frametrace-thumb" />}
                            <div className="frametrace-meta">
                                <strong>{check.kind}</strong>{' '}
                                {'phase' in check && <span>{check.phase}</span>}{' '}
                                {'cycle' in check && <span>cycle {check.cycle}</span>}
                                <div>
                                    {'x_center' in check && check.x_center != null && (
                                        <span>x={check.x_center.toFixed(2)} </span>
                                    )}
                                    {'fill' in check && check.fill != null && (
                                        <span>fill={check.fill.toFixed(3)} </span>
                                    )}
                                    {'gate' in check && check.gate && <span>gate={check.gate} </span>}
                                    {'reject_reason' in check && check.reject_reason && (
                                        <span className="warn">reject={check.reject_reason}</span>
                                    )}
                                </div>
                                {'action' in check && <div className="note">{check.action}</div>}
                            </div>
                        </div>
                    )
                })}
            </div>
        </section>
    )
}

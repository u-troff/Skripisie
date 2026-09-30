import { useEffect, useRef, useState } from 'react'

const POLL_MS = 500
// Matches nudge_gimbal's default duration (0.15s) so held-down pulses don't
// stack faster than the servo move they trigger.
const REPEAT_MS = 180

type Direction = 'up' | 'down' | 'left' | 'right' | 'center'

async function nudge(direction: Direction): Promise<void> {
    try {
        await fetch(`/api/pi/gimbal/${direction}`, { method: 'POST' })
    } catch {
        // best-effort — this is a debug tool, not mission-critical
    }
}

export default function GimbalControl() {
    const [tick, setTick] = useState(0)
    const imgRef = useRef<HTMLImageElement | null>(null)
    const holdRef = useRef<number | null>(null)

    useEffect(() => {
        const id = window.setInterval(() => setTick((t) => t + 1), POLL_MS)
        return () => window.clearInterval(id)
    }, [])

    const stopHold = () => {
        if (holdRef.current !== null) {
            window.clearInterval(holdRef.current)
            holdRef.current = null
        }
    }

    // Global safety net: if the pointer is released off the button (dragged
    // away before letting go), the button's own onMouseUp never fires and
    // the repeat would otherwise run forever.
    useEffect(() => {
        window.addEventListener('mouseup', stopHold)
        window.addEventListener('touchend', stopHold)
        return () => {
            window.removeEventListener('mouseup', stopHold)
            window.removeEventListener('touchend', stopHold)
        }
    }, [])

    const startHold = (direction: Direction) => {
        stopHold()
        void nudge(direction)
        holdRef.current = window.setInterval(() => void nudge(direction), REPEAT_MS)
    }

    return (
        <section className="panel">
            <h2>Manual gimbal control (debug)</h2>
            <p className="note" style={{ marginTop: 0 }}>
                Raw teleop for aiming the camera — not part of the plan/execution pipeline.
                Arrows are logical directions: both servos are mounted reversed, and
                rover_pi._pwm() is the one place that is corrected for. Do not re-swap
                them here.
            </p>

            <img
                ref={imgRef}
                className="feed"
                src={`/api/pi/camera/snapshot?t=${tick}`}
                alt="TurboPi live camera"
            />

            <div className="dpad">
                <div />
                <button
                    onMouseDown={() => startHold('up')}
                    onMouseUp={stopHold}
                    onMouseLeave={stopHold}
                    onTouchStart={() => startHold('up')}
                    onTouchEnd={stopHold}
                >
                    ▲
                </button>
                <div />
                <button
                    onMouseDown={() => startHold('left')}
                    onMouseUp={stopHold}
                    onMouseLeave={stopHold}
                    onTouchStart={() => startHold('left')}
                    onTouchEnd={stopHold}
                >
                    ◀
                </button>
                <button onClick={() => void nudge('center')}>●</button>
                <button
                    onMouseDown={() => startHold('right')}
                    onMouseUp={stopHold}
                    onMouseLeave={stopHold}
                    onTouchStart={() => startHold('right')}
                    onTouchEnd={stopHold}
                >
                    ▶
                </button>
                <div />
                <button
                    onMouseDown={() => startHold('down')}
                    onMouseUp={stopHold}
                    onMouseLeave={stopHold}
                    onTouchStart={() => startHold('down')}
                    onTouchEnd={stopHold}
                >
                    ▼
                </button>
                <div />
            </div>
        </section>
    )
}

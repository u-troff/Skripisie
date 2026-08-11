import { useCallback, useEffect, useRef, useState } from 'react'
import useRecorder from './useRecorder'

const PHASE_LABEL = {
  clarifying: 'clarifying',
  planning: 'planning',
  verifying: 'verifying',
  awaiting_confirmation: 'awaiting confirmation',
  executing: 'executing',
  reporting: 'reporting',
  cancelled: 'cancelled',
}

function socketUrl() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/ws/dialogue`
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

export default function DialoguePanel({ language, image }) {
  const [status, setStatus] = useState('idle') // idle | connecting | open | closed
  const [sessionId, setSessionId] = useState(null)
  const [phase, setPhase] = useState(null)
  const [snapshot, setSnapshot] = useState(null)
  const [saying, setSaying] = useState(null) // latest {type: speak} text
  const [plan, setPlan] = useState(null)
  const [outcome, setOutcome] = useState(null) // 'execute' | 'cancelled'
  const [thinking, setThinking] = useState(false)
  const [error, setError] = useState(null)
  const [log, setLog] = useState([])
  const [showLog, setShowLog] = useState(false)
  const [speakAloud, setSpeakAloud] = useState(true)

  const socketRef = useRef(null)
  const sessionIdRef = useRef(null)
  const sentClipRef = useRef(null)
  const speakAloudRef = useRef(speakAloud)
  const languageRef = useRef(language)

  const recorder = useRecorder()
  const { clip, recording, elapsed } = recorder

  useEffect(() => {
    speakAloudRef.current = speakAloud
  }, [speakAloud])
  useEffect(() => {
    languageRef.current = language
  }, [language])

  const append = useCallback((frame) => {
    setLog((prev) => [...prev.slice(-49), { at: new Date().toLocaleTimeString(), frame }])
  }, [])

  // Stand-in for Piper on the Pi, so the loop can be driven end to end from a
  // laptop before any TTS exists on the robot.
  const say = useCallback((text) => {
    if (!speakAloudRef.current || typeof window.speechSynthesis === 'undefined') return
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.lang = languageRef.current === 'af' ? 'af-ZA' : 'en-ZA'
    window.speechSynthesis.cancel()
    window.speechSynthesis.speak(utterance)
  }, [])

  // The socket never echoes what Whisper heard, so pull the authoritative
  // turn history from the session snapshot after every exchange.
  const refresh = useCallback(async () => {
    const id = sessionIdRef.current
    if (!id) return
    try {
      const response = await fetch(`/api/dialogue/${id}`)
      const data = await response.json()
      if (!data.error) setSnapshot(data)
    } catch {
      /* the socket is the source of truth; a failed poll is cosmetic */
    }
  }, [])

  const disconnect = useCallback(() => {
    // StrictMode runs mount cleanups once before the real mount, so this must
    // be a no-op when there was never a socket.
    if (!socketRef.current) return
    socketRef.current.onclose = null
    socketRef.current.close()
    socketRef.current = null
    if (typeof window.speechSynthesis !== 'undefined') window.speechSynthesis.cancel()
    setStatus('closed')
  }, [])

  useEffect(() => disconnect, [disconnect])

  const connect = useCallback(() => {
    setError(null)
    setSnapshot(null)
    setSaying(null)
    setPlan(null)
    setOutcome(null)
    setLog([])
    setPhase(null)
    setSessionId(null)
    sessionIdRef.current = null
    recorder.reset()
    setStatus('connecting')

    const socket = new WebSocket(socketUrl())
    socketRef.current = socket

    socket.onopen = () => {
      setStatus('open')
      socket.send(JSON.stringify({ type: 'start', language: languageRef.current }))
    }

    socket.onerror = () => setError('WebSocket error — is the brain running on :8000?')
    socket.onclose = () => setStatus('closed')

    socket.onmessage = (event) => {
      const frame = JSON.parse(event.data)
      append(frame)
      setThinking(false)

      switch (frame.type) {
        case 'session':
          sessionIdRef.current = frame.session_id
          setSessionId(frame.session_id)
          setPhase(frame.phase)
          break
        case 'speak':
          setSaying(frame.text)
          if (frame.phase) setPhase(frame.phase)
          say(frame.text)
          refresh()
          break
        case 'plan_ready':
          setPlan(frame)
          setPhase('awaiting_confirmation')
          refresh()
          break
        case 'execute':
          setOutcome('execute')
          setPhase('executing')
          refresh()
          break
        case 'revise':
          setPlan(null)
          setPhase('clarifying')
          break
        case 'cancelled':
          setOutcome('cancelled')
          setPhase('cancelled')
          break
        case 'error':
          setError(frame.message)
          break
        default:
          break
      }
    }
  }, [append, recorder, refresh, say])

  // Auto-send on stop: in a conversation, a second "send" click is friction.
  useEffect(() => {
    if (!clip || sentClipRef.current === clip.id) return
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return
    sentClipRef.current = clip.id

    const first = !snapshot || !snapshot.command
    const confirming = phase === 'awaiting_confirmation'

    ;(async () => {
      const payload = {
        type: confirming ? 'confirmation_audio' : 'audio_chunk',
        audio: await blobToBase64(clip.blob),
      }
      // The backend keeps the frame on the session, so only the opening
      // command needs to carry it.
      if (first && image) payload.image = await blobToBase64(image.file)
      setThinking(true)
      setSaying(null)
      socket.send(JSON.stringify(payload))
    })()
  }, [clip, image, phase, snapshot])

  const live = status === 'open' && !outcome
  const confirming = phase === 'awaiting_confirmation'
  const awaitingAnswer =
    snapshot && snapshot.turns && snapshot.turns.some((turn) => turn.answer === null)

  let buttonLabel = 'Record command'
  if (recording) buttonLabel = 'Stop'
  else if (confirming) buttonLabel = 'Record confirmation'
  else if (awaitingAnswer) buttonLabel = 'Answer'

  return (
    <section className="panel">
      <h2>4 · Multi-turn dialogue</h2>
      <p className="note" style={{ marginTop: 0 }}>
        Clarify → plan → verify → <strong>voice confirmation</strong>, over{' '}
        <code>/ws/dialogue</code>. Every clip is one complete utterance; the backend decides
        whether it is an answer or the confirmation from the session phase.
      </p>

      <div className="dlgbar">
        <button className="ghost" onClick={live ? disconnect : connect}>
          {live ? 'End session' : status === 'connecting' ? 'Connecting…' : 'Start session'}
        </button>
        {phase && <span className={`phase ${phase}`}>{PHASE_LABEL[phase] || phase}</span>}
        {sessionId && <code className="sid">{sessionId}</code>}
        <label className="chk">
          <input
            type="checkbox"
            checked={speakAloud}
            onChange={(event) => setSpeakAloud(event.target.checked)}
          />
          speak aloud
        </label>
      </div>

      {!sessionId && status !== 'connecting' && (
        <p className="note">
          Start a session, then record your command. Any reference image picked in step 1 is sent
          with the opening command and reused for every later VLM call.
        </p>
      )}

      {sessionId && (
        <>
          <div className="thread">
            {snapshot && snapshot.command && (
              <div className="bubble you">
                <span className="who">you</span>
                {snapshot.command}
              </div>
            )}
            {(snapshot ? snapshot.turns : []).map((turn, index) => (
              <div key={index}>
                <div className="bubble robot">
                  <span className="who">robot</span>
                  {turn.question}
                </div>
                {turn.answer && (
                  <div className="bubble you">
                    <span className="who">you</span>
                    {turn.answer}
                  </div>
                )}
              </div>
            ))}
            {saying && !(snapshot && snapshot.turns.some((t) => t.question === saying)) && (
              <div className="bubble robot">
                <span className="who">robot</span>
                {saying}
              </div>
            )}
            {thinking && <div className="bubble thinking">thinking…</div>}
          </div>

          {snapshot && (
            <div className="turnbar">
              <span>
                turn {snapshot.turn_count}
                {snapshot.capped && <strong className="capped"> · cap hit</strong>}
              </span>
              {snapshot.resolved_command && (
                <span className="resolved">resolved: {snapshot.resolved_command}</span>
              )}
            </div>
          )}

          {plan && (
            <div className={`planbox ${confirming ? 'gate' : ''}`}>
              <h3 style={{ marginTop: 0 }}>Plan — awaiting voice confirmation</h3>
              <ol className="steps">
                {((plan.plan && plan.plan.steps) || []).map((step, index) => (
                  <li key={step.id ?? index}>
                    <span className="act">{step.action}</span>
                    {step.target && <span className="tgt">{step.target}</span>}
                  </li>
                ))}
              </ol>
              {plan.plan && plan.plan.notes && <p className="notes">{plan.plan.notes}</p>}
              {plan.verified === false && <p className="warn">Verifier flagged: {plan.concerns}</p>}
              {plan.capped && (
                <p className="warn">
                  Clarification cap hit — this is a best-guess interpretation, not a confident one.
                </p>
              )}
            </div>
          )}

          {outcome === 'execute' && (
            <div className="outcome go">
              Confirmed — plan handed off. Nothing consumes <code>execute</code> yet; the execution
              WebSocket is still to be built.
            </div>
          )}
          {outcome === 'cancelled' && (
            <div className="outcome stop">
              Cancelled — no clear confirmation. The rover does not move.
            </div>
          )}

          {live && (
            <>
              <button
                className={`rec ${recording ? 'active' : ''} ${
                  confirming && !recording ? 'gate' : ''
                }`}
                onClick={recording ? recorder.stop : recorder.start}
                disabled={thinking}
              >
                {buttonLabel}
              </button>
              <div className="meter">
                {recording && <span className="dot" />}
                <span className="time">{elapsed.toFixed(1)}s</span>
                {confirming && !recording && (
                  <span className="hint">say yes / ja to go, or nee / cancel to stop</span>
                )}
              </div>
            </>
          )}

          {recorder.error && <pre className="error">{recorder.error}</pre>}
          {error && <pre className="error">{error}</pre>}

          <button className="ghost logtoggle" onClick={() => setShowLog((prev) => !prev)}>
            {showLog ? 'Hide' : 'Show'} raw frames ({log.length})
          </button>
          {showLog && (
            <pre className="log">
              {log.map((entry) => `${entry.at}  ${JSON.stringify(entry.frame)}`).join('\n')}
            </pre>
          )}
        </>
      )}
    </section>
  )
}

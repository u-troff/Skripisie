import { useCallback, useEffect, useRef } from 'react'
import useRecorder from '../hooks/useRecorder'
import { blobToBase64, fetchDialogueSnapshot, openSocket, type Socket } from '../net'
import { say, stopSpeaking } from '../speak'
import { storeApi, useStore } from '../store'
import type { DialogueEvent, DialoguePhase } from '../types'

const PHASE_LABEL: Record<DialoguePhase, string> = {
  clarifying: 'clarifying',
  planning: 'planning',
  verifying: 'verifying',
  awaiting_confirmation: 'awaiting confirmation',
  executing: 'executing',
  reporting: 'reporting',
  cancelled: 'cancelled',
}

export default function DialoguePanel() {
  const dialogue = useStore((state) => state.dialogue)
  const speakAloud = useStore((state) => state.speakAloud)
  const setSpeakAloud = useStore((state) => state.setSpeakAloud)

  const socketRef = useRef<Socket | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const sentClipRef = useRef<string | null>(null)

  const recorder = useRecorder()
  const { clip, recording, elapsed } = recorder

  // The socket never echoes what Whisper heard, so pull the authoritative
  // turn history from the session snapshot after every exchange.
  const refresh = useCallback(async () => {
    const id = sessionIdRef.current
    if (!id) return
    const snapshot = await fetchDialogueSnapshot(id)
    if (snapshot) storeApi().setDialogueSnapshot(snapshot)
  }, [])

  const onEvent = useCallback(
    (event: DialogueEvent) => {
      const store = storeApi()
      store.applyDialogueEvent(event)

      switch (event.type) {
        case 'session':
          sessionIdRef.current = event.session_id
          break
        case 'speak':
          if (store.speakAloud) say(event.text, store.language)
          void refresh()
          break
        case 'plan_ready':
          void refresh()
          break
        case 'execute':
          store.setMissionId(event.session_id)
          void refresh()
          break
        default:
          break
      }
    },
    [refresh],
  )

  const disconnect = useCallback(() => {
    // StrictMode runs mount cleanups once before the real mount, so this must
    // be a no-op when there was never a socket.
    if (!socketRef.current) return
    socketRef.current.close()
    socketRef.current = null
    stopSpeaking()
    storeApi().setDialogueStatus('closed')
  }, [])

  useEffect(() => disconnect, [disconnect])

  const connect = useCallback(() => {
    const store = storeApi()
    store.resetDialogue()
    store.setMissionId(null)
    sessionIdRef.current = null
    recorder.reset()

    socketRef.current = openSocket<DialogueEvent>('/ws/dialogue', {
      onEvent,
      onStatus: (status) => storeApi().setDialogueStatus(status),
      onError: (message) => storeApi().setDialogueError(message),
      onOpen: (socket) => socket.send({ type: 'start', language: storeApi().language }),
    })
  }, [onEvent, recorder])

  // Auto-send on stop: in a conversation, a second "send" click is friction.
  useEffect(() => {
    if (!clip || sentClipRef.current === clip.id) return
    const socket = socketRef.current
    if (!socket?.isOpen()) return
    sentClipRef.current = clip.id

    const store = storeApi()
    const first = !store.dialogue.snapshot?.command
    const confirming = store.dialogue.phase === 'awaiting_confirmation'

    void (async () => {
      const payload: Record<string, unknown> = {
        type: confirming ? 'confirmation_audio' : 'audio_chunk',
        audio: await blobToBase64(clip.blob),
      }
      // The backend keeps the frame on the session, so only the opening
      // command needs to carry it.
      if (first && store.image) payload.image = await blobToBase64(store.image.file)
      store.setDialogueThinking(true)
      socket.send(payload)
    })()
  }, [clip])

  const { status, sessionId, phase, snapshot, saying, planReady, outcome, thinking, error, log } =
    dialogue
  const live = status === 'open' && !outcome
  const confirming = phase === 'awaiting_confirmation'
  const awaitingAnswer = snapshot?.turns.some((turn) => turn.answer === null) ?? false

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
        {phase && <span className={`phase ${phase}`}>{PHASE_LABEL[phase]}</span>}
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
            {snapshot?.command && (
              <div className="bubble you">
                <span className="who">you</span>
                {snapshot.command}
              </div>
            )}
            {(snapshot?.turns ?? []).map((turn, index) => (
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
            {saying && !snapshot?.turns.some((turn) => turn.question === saying) && (
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

          {planReady && (
            <div className={`planbox ${confirming ? 'gate' : ''}`}>
              <h3 style={{ marginTop: 0 }}>Plan — awaiting voice confirmation</h3>
              <ol className="steps">
                {(planReady.plan.steps ?? []).map((step, index) => (
                  <li key={step.id ?? index}>
                    <span className="act">{step.action}</span>
                    {step.target && <span className="tgt">{step.target}</span>}
                  </li>
                ))}
              </ol>
              {planReady.plan.notes && <p className="notes">{planReady.plan.notes}</p>}
              {planReady.verified === false && (
                <p className="warn">Verifier flagged: {planReady.concerns}</p>
              )}
              {planReady.capped && (
                <p className="warn">
                  Clarification cap hit — this is a best-guess interpretation, not a confident one.
                </p>
              )}
            </div>
          )}

          {outcome === 'execute' && (
            <div className="outcome go">Confirmed — handed off to the mission panel below.</div>
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
                onClick={recording ? recorder.stop : () => void recorder.start()}
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

          <details>
            <summary className="note">Raw frames ({log.length})</summary>
            <pre className="log">
              {log.map((entry) => `${entry.at}  ${JSON.stringify(entry.frame)}`).join('\n')}
            </pre>
          </details>
        </>
      )}
    </section>
  )
}

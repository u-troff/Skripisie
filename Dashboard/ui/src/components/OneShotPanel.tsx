import { useCallback, useState } from 'react'
import useRecorder from '../hooks/useRecorder'
import { useStore } from '../store'
import type { CommandResult, TranscribeResult } from '../types'

type Kind = 'transcribe' | 'command'
type Result = { kind: Kind; roundTrip: number } & Partial<TranscribeResult> &
  Partial<CommandResult>

export default function OneShotPanel() {
  const language = useStore((state) => state.language)
  const image = useStore((state) => state.image)

  // Local by design: nothing outside this panel reads a one-shot result.
  const [busy, setBusy] = useState<Kind | null>(null)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState<string | null>(null)

  const recorder = useRecorder()
  const { clip, recording, elapsed } = recorder

  const startTake = useCallback(() => {
    setError(null)
    setResult(null)
    void recorder.start()
  }, [recorder])

  const send = useCallback(
    async (kind: Kind) => {
      if (!clip) return
      setBusy(kind)
      setError(null)
      setResult(null)
      const started = performance.now()
      try {
        const form = new FormData()
        form.append('audio', clip.blob, 'clip')
        form.append('language', language)
        // The image is only read by the pipeline's VLM calls; /transcribe
        // ignores it, so don't bother uploading it there.
        if (kind === 'command' && image) form.append('image', image.file, image.file.name)

        const response = await fetch(`/api/${kind}`, { method: 'POST', body: form })
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)

        const data = (await response.json()) as TranscribeResult & CommandResult
        setResult({ kind, ...data, roundTrip: (performance.now() - started) / 1000 })
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        setBusy(null)
      }
    },
    [clip, image, language],
  )

  const disabled = recording || busy !== null

  return (
    <>
      <section className="panel">
        <h2>2 · Record</h2>
        <button
          className={recording ? 'rec active' : 'rec'}
          onClick={recording ? recorder.stop : startTake}
          disabled={busy !== null}
        >
          {recording ? 'Stop' : clip ? 'Record again' : 'Record'}
        </button>

        <div className="meter">
          {recording && <span className="dot" />}
          <span className="time">{elapsed.toFixed(1)}s</span>
          {clip && !recording && <span className="ok">clip ready</span>}
        </div>

        {clip && !recording && <audio className="player" controls src={clip.url} />}
        {recorder.error && <pre className="error">{recorder.error}</pre>}

        <p className="note">
          Whisper pads every clip to a fixed 30s window, so a 2s clip costs about as much as a 25s
          one. Speak a full sentence rather than a single word.
        </p>
      </section>

      <section className="panel">
        <h2>3 · Run (single-shot)</h2>
        <div className="actions">
          <button
            className="go"
            onClick={() => void send('transcribe')}
            disabled={!clip || disabled}
          >
            {busy === 'transcribe' ? 'Transcribing…' : 'Transcribe only'}
          </button>
          <button
            className="go alt"
            onClick={() => void send('command')}
            disabled={!clip || disabled}
          >
            {busy === 'command' ? 'Planning…' : 'Run full pipeline'}
          </button>
        </div>
        <p className="note">
          <strong>Transcribe only</strong> hits Whisper and stops — use it to tell a bad transcript
          apart from a bad plan. <strong>Full pipeline</strong> adds the ambiguity check, planner,
          and verification. This is the old one-question flow, kept as the baseline to compare the
          dialogue against.
        </p>
      </section>

      {error && <pre className="error">{error}</pre>}

      {result && (
        <section className="panel result">
          <h2>Result</h2>
          <p className="transcript">{result.text || result.transcript || <em>(empty)</em>}</p>

          {result.status === 'needs_clarification' && (
            <div className="clarify">
              <strong>Needs clarification</strong>
              <p>{result.question}</p>
              {result.reason && <small>{result.reason}</small>}
            </div>
          )}

          {result.plan && (
            <>
              <h3>Plan</h3>
              <ol className="steps">
                {(result.plan.steps ?? []).map((step, index) => (
                  <li key={step.id ?? index}>
                    <span className="act">{step.action}</span>
                    {step.target && <span className="tgt">{step.target}</span>}
                  </li>
                ))}
              </ol>
              {result.plan.notes && <p className="notes">{result.plan.notes}</p>}
              {result.verified === false && (
                <p className="warn">Verifier flagged: {result.concerns}</p>
              )}
            </>
          )}

          <dl>
            <div>
              <dt>mode</dt>
              <dd>{result.kind}</dd>
            </div>
            {result.model && (
              <div>
                <dt>stt model</dt>
                <dd>{result.model}</dd>
              </div>
            )}
            {result.duration != null && (
              <div>
                <dt>audio</dt>
                <dd>{result.duration.toFixed(1)}s</dd>
              </div>
            )}
            {result.elapsed != null && (
              <div>
                <dt>server</dt>
                <dd>{result.elapsed.toFixed(2)}s</dd>
              </div>
            )}
            <div>
              <dt>round trip</dt>
              <dd>{result.roundTrip.toFixed(2)}s</dd>
            </div>
            {result.had_image != null && (
              <div>
                <dt>image</dt>
                <dd>{result.had_image ? 'sent' : 'none'}</dd>
              </div>
            )}
          </dl>
        </section>
      )}
    </>
  )
}

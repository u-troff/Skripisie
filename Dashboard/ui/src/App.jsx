import { useCallback, useEffect, useState } from 'react'
import DialoguePanel from './DialoguePanel'
import useRecorder from './useRecorder'

// qwen2.5vl turns image resolution into vision tokens, and a phone photo alone
// can exceed the model's context window. Cap the long edge before upload.
const MAX_IMAGE_EDGE = 768

function downscale(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(img.width, img.height))
      if (scale === 1) {
        URL.revokeObjectURL(url)
        return resolve(file)
      }
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(img.width * scale)
      canvas.height = Math.round(img.height * scale)
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height)
      URL.revokeObjectURL(url)
      canvas.toBlob(
        (blob) => resolve(blob ? new File([blob], file.name, { type: 'image/jpeg' }) : file),
        'image/jpeg',
        0.85,
      )
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      resolve(file)
    }
    img.src = url
  })
}

export default function App() {
  const [health, setHealth] = useState('checking')
  const [language, setLanguage] = useState('af')
  const [image, setImage] = useState(null) // { file, url }

  const [busy, setBusy] = useState(null) // 'transcribe' | 'command' | null
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const recorder = useRecorder()
  const { clip, recording, elapsed } = recorder

  useEffect(() => {
    fetch('/api/health')
      .then((r) => setHealth(r.ok ? 'up' : 'down'))
      .catch(() => setHealth('down'))
  }, [])

  // Revoke object URLs so repeated takes don't leak blobs.
  useEffect(() => () => image && URL.revokeObjectURL(image.url), [image])

  const startTake = useCallback(() => {
    setError(null)
    setResult(null)
    recorder.start()
  }, [recorder])

  const send = useCallback(
    async (kind) => {
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

        const response = await fetch(`/api/${kind === 'command' ? 'command' : 'transcribe'}`, {
          method: 'POST',
          body: form,
        })
        if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)

        const data = await response.json()
        setResult({ kind, ...data, roundTrip: (performance.now() - started) / 1000 })
      } catch (err) {
        setError(String(err.message || err))
      } finally {
        setBusy(null)
      }
    },
    [clip, image, language],
  )

  const onPickImage = async (event) => {
    const file = event.target.files && event.target.files[0]
    event.target.value = ''
    if (!file) return
    const scaled = await downscale(file)
    setImage({ file: scaled, url: URL.createObjectURL(scaled) })
  }

  const disabled = recording || busy !== null

  return (
    <main className="wrap">
      <header>
        <h1>Brain — voice &amp; plan test</h1>
        <span className={`pill ${health}`}>backend {health}</span>
      </header>

      {/* 1 — reference image */}
      <section className="panel">
        <h2>1 · Reference image (optional)</h2>
        <p className="note">
          Sent to the VLM for the ambiguity check and plan verification. Used by the pipeline and
          the dialogue only — plain transcription ignores it.
        </p>
        {image ? (
          <div className="imgrow">
            <img src={image.url} alt="reference" />
            <div>
              <div className="fname">{image.file.name}</div>
              <button className="ghost" onClick={() => setImage(null)} disabled={disabled}>
                Remove
              </button>
            </div>
          </div>
        ) : (
          <label className={`drop ${disabled ? 'off' : ''}`}>
            <input type="file" accept="image/*" onChange={onPickImage} disabled={disabled} />
            Choose an image…
          </label>
        )}
      </section>

      {/* 2 — record */}
      <section className="panel">
        <h2>2 · Record</h2>
        <div className="row">
          <label htmlFor="lang">Language</label>
          <select
            id="lang"
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
            disabled={disabled}
          >
            <option value="af">Afrikaans (af)</option>
            <option value="en">English (en)</option>
          </select>
        </div>

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

      {/* 3 — run */}
      <section className="panel">
        <h2>3 · Run (single-shot)</h2>
        <div className="actions">
          <button className="go" onClick={() => send('transcribe')} disabled={!clip || disabled}>
            {busy === 'transcribe' ? 'Transcribing…' : 'Transcribe only'}
          </button>
          <button className="go alt" onClick={() => send('command')} disabled={!clip || disabled}>
            {busy === 'command' ? 'Planning…' : 'Run full pipeline'}
          </button>
        </div>
        <p className="note">
          <strong>Transcribe only</strong> hits Whisper and stops — use it to tell a bad transcript
          apart from a bad plan. <strong>Full pipeline</strong> adds the ambiguity check, planner,
          and verification, so it loads all three models. This is the old one-question flow, kept
          as the baseline to compare the dialogue against.
        </p>
      </section>

      {error && <pre className="error">{error}</pre>}

      {result && (
        <section className="panel result">
          <h2>Result</h2>

          {result.transcript !== undefined || result.text !== undefined ? (
            <p className="transcript">{result.text || result.transcript || <em>(empty)</em>}</p>
          ) : null}

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
                {(result.plan.steps || []).map((step, index) => (
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

      <DialoguePanel language={language} image={image} />
    </main>
  )
}

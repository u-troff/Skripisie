import { useEffect } from 'react'
import DialoguePanel from './components/DialoguePanel'
import ExecutionPanel from './components/ExecutionPanel'
import OneShotPanel from './components/OneShotPanel'
import { fetchHealth, postScene } from './net'
import { useStore } from './store'
import type { Language } from './types'

// qwen2.5vl turns image resolution into vision tokens, and a phone photo alone
// can exceed the model's context window. Cap the long edge before upload.
const MAX_IMAGE_EDGE = 768

function downscale(file: File): Promise<File> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(img.width, img.height))
      if (scale === 1) {
        URL.revokeObjectURL(url)
        resolve(file)
        return
      }
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(img.width * scale)
      canvas.height = Math.round(img.height * scale)
      canvas.getContext('2d')?.drawImage(img, 0, 0, canvas.width, canvas.height)
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
  const health = useStore((state) => state.health)
  const setHealth = useStore((state) => state.setHealth)
  const language = useStore((state) => state.language)
  const setLanguage = useStore((state) => state.setLanguage)
  const image = useStore((state) => state.image)
  const setImage = useStore((state) => state.setImage)
  const scene = useStore((state) => state.scene)
  const setScene = useStore((state) => state.setScene)
  const sceneUploading = useStore((state) => state.sceneUploading)
  const setSceneUploading = useStore((state) => state.setSceneUploading)
  const sceneError = useStore((state) => state.sceneError)
  const setSceneError = useStore((state) => state.setSceneError)

  useEffect(() => {
    void fetchHealth().then((ok) => setHealth(ok ? 'up' : 'down'))
  }, [setHealth])

  const onPickVideo = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setSceneError(null)
    setScene(null)
    setSceneUploading(true)
    try {
      setScene(await postScene(file))
    } catch (err) {
      setSceneError(err instanceof Error ? err.message : String(err))
    } finally {
      setSceneUploading(false)
    }
  }

  const onPickImage = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const scaled = await downscale(file)
    setImage({ file: scaled, url: URL.createObjectURL(scaled) })
  }

  return (
    <main className="wrap">
      <header>
        <h1>Brain — voice &amp; plan test</h1>
        <span className={`pill ${health}`}>backend {health}</span>
      </header>

      <section className="panel">
        <h2>1 · The room</h2>
        <p className="note" style={{ marginTop: 0 }}>
          Upload a clip of the room first. Keyframes are extracted and catalogued once, and that
          catalogue is what the clarifying questions and the planner reason over — without it the
          model has no way to know whether "the thing by the window" is ambiguous.
        </p>

        <label className={`drop ${sceneUploading ? 'off' : ''}`}>
          <input type="file" accept="video/*" onChange={(e) => void onPickVideo(e)}
                 disabled={sceneUploading} />
          {sceneUploading ? 'Reading the room… (one model call per keyframe)' : 'Choose a room video…'}
        </label>

        {sceneError && <pre className="error">{sceneError}</pre>}

        {scene && (
          <>
            <div className="turnbar">
              <span>
                {scene.frame_count} keyframe(s) in {scene.elapsed.toFixed(1)}s
              </span>
              <code className="sid">{scene.scene_id}</code>
            </div>
            <div className="strip">
              {scene.frames.map((frame) => (
                <figure key={frame.index} className={frame.error ? 'kf bad' : 'kf'}>
                  <img src={`data:image/jpeg;base64,${frame.thumbnail}`} alt={frame.place} />
                  <figcaption>
                    <strong>{frame.place || `view ${frame.index + 1}`}</strong>
                    {frame.error ? (
                      <span className="warn">{frame.error}</span>
                    ) : (
                      <span>
                        {frame.objects.map((o) => o.name).join(', ') || 'nothing identified'}
                      </span>
                    )}
                    {frame.obstacles.length > 0 && (
                      <span className="warn">blocked: {frame.obstacles.join(', ')}</span>
                    )}
                  </figcaption>
                </figure>
              ))}
            </div>
          </>
        )}

        <h3>Reference still (optional)</h3>
        <p className="note" style={{ marginTop: 0 }}>
          Only needed if you skip the video. The single-shot pipeline in step 3 uses this.
        </p>

        <div className="row">
          <label htmlFor="lang">Language</label>
          <select
            id="lang"
            value={language}
            onChange={(event) => setLanguage(event.target.value as Language)}
          >
            <option value="af">Afrikaans (af)</option>
            <option value="en">English (en)</option>
          </select>
        </div>

        {image ? (
          <div className="imgrow">
            <img src={image.url} alt="reference" />
            <div>
              <div className="fname">{image.file.name}</div>
              <button className="ghost" onClick={() => setImage(null)}>
                Remove
              </button>
            </div>
          </div>
        ) : (
          <label className="drop">
            <input type="file" accept="image/*" onChange={(event) => void onPickImage(event)} />
            Choose an image…
          </label>
        )}
      </section>

      <OneShotPanel />
      <DialoguePanel />
      <ExecutionPanel />
    </main>
  )
}

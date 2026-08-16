import { useCallback, useEffect, useRef, useState } from 'react'
import useRecorder from '../hooks/useRecorder'
import { blobToBase64, openSocket, type Socket } from '../net'
import { say } from '../speak'
import { storeApi, useStore } from '../store'
import type { MissionEvent } from '../types'

// One frame per second is plenty: the backend's Tier 0 discards most of them,
// and Tier 1 is rate-limited by PERCEPTION_MIN_INTERVAL_S anyway.
const FRAME_INTERVAL_MS = 1000
const FRAME_MAX_EDGE = 640
const FRAME_QUALITY = 0.7

type FrameSource = 'camera' | 'video' | 'off'

async function grabFrame(
  video: HTMLVideoElement | null,
  canvas: HTMLCanvasElement | null,
): Promise<string | null> {
  if (!video || !canvas || !video.videoWidth) return null
  const scale = Math.min(1, FRAME_MAX_EDGE / Math.max(video.videoWidth, video.videoHeight))
  canvas.width = Math.round(video.videoWidth * scale)
  canvas.height = Math.round(video.videoHeight * scale)
  canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, 'image/jpeg', FRAME_QUALITY),
  )
  return blob ? blobToBase64(blob) : null
}

export default function ExecutionPanel() {
  const sessionId = useStore((state) => state.missionId)
  const mission = useStore((state) => state.mission)

  // Genuinely local: nothing outside this panel reads the frame source.
  const [source, setSource] = useState<FrameSource>('camera')
  const [videoUrl, setVideoUrl] = useState<string | null>(null)

  const socketRef = useRef<Socket | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const sentClipRef = useRef<string | null>(null)

  const recorder = useRecorder()
  const { clip, recording } = recorder

  useEffect(() => {
    if (!sessionId) return undefined
    storeApi().resetMission()

    const socket = openSocket<MissionEvent>('/ws/execution', {
      onEvent: (event) => {
        const store = storeApi()
        store.applyMissionEvent(event)
        if (event.type === 'speak' && store.speakAloud) say(event.text, store.language)
      },
      onStatus: (status) => storeApi().setMissionStatus(status),
      onOpen: (opened) => opened.send({ type: 'begin', session_id: sessionId }),
    })
    socketRef.current = socket

    return () => {
      socket.close()
      socketRef.current = null
    }
  }, [sessionId])

  useEffect(() => {
    if (source !== 'camera') {
      streamRef.current?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      return undefined
    }
    let cancelled = false
    navigator.mediaDevices
      .getUserMedia({ video: { width: 1280 } })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop())
          return
        }
        streamRef.current = stream
        if (videoRef.current) {
          videoRef.current.srcObject = stream
          void videoRef.current.play().catch(() => undefined)
        }
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [source])

  const terminal =
    mission.phase === 'halted' || mission.phase === 'aborted' || mission.phase === 'completed'

  useEffect(() => {
    if (mission.status !== 'open' || source === 'off' || terminal) return undefined
    const id = window.setInterval(() => {
      void (async () => {
        const socket = socketRef.current
        if (!socket?.isOpen()) return
        const image = await grabFrame(videoRef.current, canvasRef.current)
        if (!image) return
        socket.send({ type: 'frame', image })
        storeApi().countFrame()
      })()
    }, FRAME_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [mission.status, source, terminal])

  useEffect(() => {
    if (!clip || sentClipRef.current === clip.id) return
    const socket = socketRef.current
    if (!socket?.isOpen()) return
    sentClipRef.current = clip.id
    void (async () => {
      socket.send({ type: 'revision_audio', audio: await blobToBase64(clip.blob) })
    })()
  }, [clip])

  // The one control that never touches a model.
  const abort = useCallback(() => {
    socketRef.current?.send({ type: 'abort' })
  }, [])

  const onPickVideo = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setSource('video')
    setVideoUrl(URL.createObjectURL(file))
  }

  if (!sessionId) return null
  const steps = mission.plan?.steps ?? []

  return (
    <section className="panel">
      <h2>5 · Mission</h2>

      <div className="dlgbar">
        {mission.phase && (
          <span className={`phase ${mission.phase}`}>{mission.phase.replace(/_/g, ' ')}</span>
        )}
        <code className="sid">{sessionId}</code>
        <span className="note" style={{ margin: 0 }}>
          {mission.framesSent} frames sent
        </span>
        <button className="ghost abort" onClick={abort} disabled={terminal}>
          Abort
        </button>
      </div>

      <div className="row">
        <label htmlFor="src">Frame source</label>
        <select
          id="src"
          value={source}
          onChange={(event) => setSource(event.target.value as FrameSource)}
        >
          <option value="camera">Live camera</option>
          <option value="video">Video file</option>
          <option value="off">Off</option>
        </select>
        <label className="drop inline">
          <input type="file" accept="video/*" onChange={onPickVideo} />
          Choose a clip…
        </label>
      </div>

      <video
        ref={videoRef}
        className="feed"
        src={source === 'video' ? videoUrl ?? undefined : undefined}
        controls={source === 'video'}
        muted
        playsInline
      />
      <canvas ref={canvasRef} style={{ display: 'none' }} />

      {steps.length > 0 && (
        <ol className="steps mission">
          {steps.map((step, index) => (
            <li
              key={step.id ?? index}
              className={
                index < mission.cursor
                  ? 'done'
                  : index === mission.cursor && !terminal
                    ? 'current'
                    : ''
              }
            >
              <span className="act">{step.action}</span>
              {step.target && <span className="tgt">{step.target}</span>}
            </li>
          ))}
        </ol>
      )}

      {mission.pending && (
        <div className="planbox gate">
          <h3 style={{ marginTop: 0 }}>Material change — re-confirmation required</h3>
          <p className="warn">{mission.pending.revision.reason}</p>
          <ol className="steps">
            {(mission.pending.plan.steps ?? []).map((step, index) => (
              <li key={step.id ?? index}>
                <span className="act">{step.action}</span>
                {step.target && <span className="tgt">{step.target}</span>}
              </li>
            ))}
          </ol>
          <button
            className={`rec ${recording ? 'active' : 'gate'}`}
            onClick={recording ? recorder.stop : () => void recorder.start()}
          >
            {recording ? 'Stop' : 'Answer'}
          </button>
        </div>
      )}

      {mission.saying && <p className="transcript">{mission.saying}</p>}

      {mission.digest.length > 0 && (
        <>
          <h3>What it has seen</h3>
          <ul className="digest">
            {mission.digest.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        </>
      )}

      {mission.revisions.length > 0 && (
        <>
          <h3>Revision log</h3>
          <ul className="digest">
            {mission.revisions.map((entry, index) => (
              <li key={index}>
                <strong>{entry.kind}</strong> — {entry.reason}
                {entry.applied ? ' (applied)' : ''}
              </li>
            ))}
          </ul>
        </>
      )}

      {mission.error && <pre className="error">{mission.error}</pre>}
      {mission.ended && (
        <p className="note">
          Mission ended: {mission.phase}. {mission.results.length} step(s) run.
        </p>
      )}
    </section>
  )
}

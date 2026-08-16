import { useCallback, useEffect, useRef, useState } from 'react'

// faster-whisper decodes via FFmpeg, so any of these containers is fine.
// Safari on macOS does not support webm and will fall through to mp4.
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
]

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) ?? ''
}

export interface Clip {
  blob: Blob
  url: string
  seconds: number
  id: string
}

export interface Recorder {
  recording: boolean
  elapsed: number
  clip: Clip | null
  error: string | null
  start: () => Promise<void>
  stop: () => void
  reset: () => void
}

export default function useRecorder(): Recorder {
  const [recording, setRecording] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [clip, setClip] = useState<Clip | null>(null)
  const [error, setError] = useState<string | null>(null)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const tickRef = useRef<number | null>(null)
  const secondsRef = useRef(0)

  // Revoke object URLs so repeated takes don't leak blobs.
  useEffect(
    () => () => {
      if (clip) URL.revokeObjectURL(clip.url)
    },
    [clip],
  )

  const start = useCallback(async () => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

      const mimeType = pickMimeType()
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []

      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType })
        stream.getTracks().forEach((track) => track.stop())
        streamRef.current = null
        if (blob.size > 0) {
          setClip({
            blob,
            url: URL.createObjectURL(blob),
            seconds: secondsRef.current,
            // Lets a consumer auto-send on stop without re-firing for the same
            // take under React 18 StrictMode's double effect run.
            id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          })
        }
      }

      // No timeslice: one complete container on stop, not per-chunk fragments
      // (only the first chunk carries the header).
      recorder.start()
      recorderRef.current = recorder

      setClip(null)
      setRecording(true)
      setElapsed(0)
      secondsRef.current = 0
      tickRef.current = window.setInterval(() => {
        secondsRef.current += 0.1
        setElapsed(secondsRef.current)
      }, 100)
    } catch (err) {
      setError(`Microphone unavailable: ${err instanceof Error ? err.message : String(err)}`)
    }
  }, [])

  const stop = useCallback(() => {
    if (tickRef.current !== null) window.clearInterval(tickRef.current)
    tickRef.current = null
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop()
    }
    setRecording(false)
  }, [])

  const reset = useCallback(() => {
    setClip(null)
    setElapsed(0)
    secondsRef.current = 0
  }, [])

  useEffect(
    () => () => {
      if (tickRef.current !== null) window.clearInterval(tickRef.current)
      streamRef.current?.getTracks().forEach((track) => track.stop())
    },
    [],
  )

  return { recording, elapsed, clip, error, start, stop, reset }
}

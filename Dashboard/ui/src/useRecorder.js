import { useCallback, useEffect, useRef, useState } from 'react'

// faster-whisper decodes via FFmpeg, so any of these containers is fine.
// Safari on macOS does not support webm and will fall through to mp4.
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
]

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || ''
}

export default function useRecorder() {
  const [recording, setRecording] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [clip, setClip] = useState(null) // { blob, url, seconds, id }
  const [error, setError] = useState(null)

  const recorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)
  const tickRef = useRef(null)
  const secondsRef = useRef(0)

  // Revoke object URLs so repeated takes don't leak blobs.
  useEffect(() => () => clip && URL.revokeObjectURL(clip.url), [clip])

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
            // Lets a consumer auto-send on stop without re-firing for the
            // same take under React 18 StrictMode's double effect run.
            id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          })
        }
      }

      // No timeslice: we want ONE complete container on stop, not per-chunk
      // fragments (only the first chunk carries the header).
      recorder.start()
      recorderRef.current = recorder

      setClip(null)
      setRecording(true)
      setElapsed(0)
      secondsRef.current = 0
      tickRef.current = setInterval(() => {
        secondsRef.current += 0.1
        setElapsed(secondsRef.current)
      }, 100)
    } catch (err) {
      setError(`Microphone unavailable: ${err.message || err}`)
    }
  }, [])

  const stop = useCallback(() => {
    if (tickRef.current) clearInterval(tickRef.current)
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

  useEffect(() => {
    return () => {
      if (tickRef.current) clearInterval(tickRef.current)
      if (streamRef.current) streamRef.current.getTracks().forEach((t) => t.stop())
    }
  }, [])

  return { recording, elapsed, clip, error, start, stop, reset }
}

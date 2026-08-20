import type { DialogueSnapshot, SceneResponse, SocketStatus } from './types'

export function socketUrl(path: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}${path}`
}

export function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '')
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

export interface Socket {
  send(message: unknown): void
  close(): void
  isOpen(): boolean
}

interface SocketHandlers<E> {
  onEvent: (event: E) => void
  onStatus?: (status: SocketStatus) => void
  onOpen?: (socket: Socket) => void
  onError?: (message: string) => void
}

/** One place that knows how a socket is opened, so both panels behave alike. */
export function openSocket<E>(path: string, handlers: SocketHandlers<E>): Socket {
  const raw = new WebSocket(socketUrl(path))

  const socket: Socket = {
    send: (message) => {
      if (raw.readyState === WebSocket.OPEN) raw.send(JSON.stringify(message))
    },
    close: () => {
      raw.onclose = null
      raw.close()
    },
    isOpen: () => raw.readyState === WebSocket.OPEN,
  }

  handlers.onStatus?.('connecting')
  raw.onopen = () => {
    handlers.onStatus?.('open')
    handlers.onOpen?.(socket)
  }
  raw.onclose = () => handlers.onStatus?.('closed')
  raw.onerror = () => handlers.onError?.(`socket error on ${path} — is the brain running on :8000?`)
  raw.onmessage = (event: MessageEvent<string>) => {
    handlers.onEvent(JSON.parse(event.data) as E)
  }

  return socket
}

export async function fetchHealth(): Promise<boolean> {
  try {
    return (await fetch('/api/health')).ok
  } catch {
    return false
  }
}

export async function fetchDialogueSnapshot(id: string): Promise<DialogueSnapshot | null> {
  try {
    const response = await fetch(`/api/dialogue/${id}`)
    const data = (await response.json()) as DialogueSnapshot | { error: string }
    return 'error' in data ? null : data
  } catch {
    // The socket is the source of truth; a failed poll is cosmetic.
    return null
  }
}

/** Room video upload. Slow — one VLM call per surviving keyframe. */
export async function postScene(file: File): Promise<SceneResponse> {
  const form = new FormData()
  form.append('video', file, file.name)
  const response = await fetch('/api/scene', { method: 'POST', body: form })
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as SceneResponse
}

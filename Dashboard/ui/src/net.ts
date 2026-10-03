import type {
  DialogueSnapshot,
  RoomData,
  RunReport,
  RunSummary,
  RunTrace,
  RuntimeInfo,
  SceneResponse,
  SceneSummary,
  SocketStatus,
} from './types'

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

/** Static room geometry for the live map. Null on a non-virtual rover. */
export async function fetchRoom(): Promise<RoomData | null> {
  try {
    const response = await fetch('/api/room')
    return response.ok ? ((await response.json()) as RoomData) : null
  } catch {
    return null
  }
}

export async function fetchRuntime(): Promise<RuntimeInfo | null> {
  try {
    const response = await fetch('/api/runtime')
    return response.ok ? ((await response.json()) as RuntimeInfo) : null
  } catch {
    return null
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

/** Room video upload. Slow — one VLM call per surviving keyframe. The result
 * is persisted on the backend, so this only needs to run once per room. */
export async function postScene(file: File, name = ''): Promise<SceneResponse> {
  const form = new FormData()
  form.append('video', file, file.name)
  if (name) form.append('name', name)
  const response = await fetch('/api/scene', { method: 'POST', body: form })
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as SceneResponse
}

/** Previously catalogued rooms, newest first — for the room picker. */
export async function fetchScenes(): Promise<SceneSummary[]> {
  const response = await fetch('/api/scenes')
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as SceneSummary[]
}

/** Reload a previously built scene by id — no re-upload, no VLM calls. */
export async function fetchScene(sceneId: string): Promise<SceneResponse> {
  const response = await fetch(`/api/scene/${sceneId}`)
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as SceneResponse
}

/** Summaries of every run log on disk, newest first — for the Logs page. */
export async function fetchLogs(): Promise<RunSummary[]> {
  const response = await fetch('/api/logs')
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as RunSummary[]
}

/** A run's recorded movement for the map. Null when it has none (non-virtual rover, old log). */
export async function fetchRunTrace(sessionId: string): Promise<RunTrace | null> {
  try {
    const response = await fetch(`/api/logs/${sessionId}/trace`)
    return response.ok ? ((await response.json()) as RunTrace) : null
  } catch {
    return null
  }
}

/** One run's full report, for the Logs page's detail view. */
export async function fetchLog(sessionId: string): Promise<RunReport> {
  const response = await fetch(`/api/logs/${sessionId}`)
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`)
  return (await response.json()) as RunReport
}

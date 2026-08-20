import { create, type StateCreator } from 'zustand'
import type {
  DialogueEvent,
  DialoguePhase,
  DialogueSnapshot,
  Language,
  MissionEvent,
  MissionPhase,
  Plan,
  PlanReady,
  RevisionInfo,
  SceneResponse,
  SocketStatus,
  StepResult,
} from './types'

interface LoggedFrame {
  at: string
  frame: unknown
}

interface SessionSlice {
  health: 'checking' | 'up' | 'down'
  language: Language
  image: { file: File; url: string } | null
  speakAloud: boolean
  missionId: string | null
  scene: SceneResponse | null
  sceneUploading: boolean
  sceneError: string | null
  setScene: (scene: SceneResponse | null) => void
  setSceneUploading: (on: boolean) => void
  setSceneError: (message: string | null) => void
  setHealth: (health: 'checking' | 'up' | 'down') => void
  setLanguage: (language: Language) => void
  setImage: (image: { file: File; url: string } | null) => void
  setSpeakAloud: (on: boolean) => void
  setMissionId: (id: string | null) => void
}

interface DialogueState {
  status: SocketStatus
  sessionId: string | null
  phase: DialoguePhase | null
  snapshot: DialogueSnapshot | null
  saying: string | null
  planReady: PlanReady | null
  outcome: 'execute' | 'cancelled' | null
  thinking: boolean
  error: string | null
  log: LoggedFrame[]
}

interface DialogueSlice {
  dialogue: DialogueState
  resetDialogue: () => void
  setDialogueStatus: (status: SocketStatus) => void
  setDialogueThinking: (thinking: boolean) => void
  setDialogueSnapshot: (snapshot: DialogueSnapshot | null) => void
  setDialogueError: (message: string | null) => void
  applyDialogueEvent: (event: DialogueEvent) => void
}

interface MissionState {
  status: SocketStatus
  phase: MissionPhase | null
  plan: Plan | null
  cursor: number
  results: Array<{ index: number } & StepResult>
  digest: string[]
  revisions: Array<RevisionInfo & { applied: boolean }>
  pending: { revision: RevisionInfo; plan: Plan } | null
  saying: string | null
  framesSent: number
  error: string | null
  ended: boolean
}

interface MissionSlice {
  mission: MissionState
  resetMission: () => void
  setMissionStatus: (status: SocketStatus) => void
  countFrame: () => void
  applyMissionEvent: (event: MissionEvent) => void
}

type Store = SessionSlice & DialogueSlice & MissionSlice

const emptyDialogue: DialogueState = {
  status: 'idle',
  sessionId: null,
  phase: null,
  snapshot: null,
  saying: null,
  planReady: null,
  outcome: null,
  thinking: false,
  error: null,
  log: [],
}

const emptyMission: MissionState = {
  status: 'idle',
  phase: null,
  plan: null,
  cursor: 0,
  results: [],
  digest: [],
  revisions: [],
  pending: null,
  saying: null,
  framesSent: 0,
  error: null,
  ended: false,
}

const createSessionSlice: StateCreator<Store, [], [], SessionSlice> = (set) => ({
  health: 'checking',
  language: 'af',
  image: null,
  speakAloud: true,
  missionId: null,
  scene: null,
  sceneUploading: false,
  sceneError: null,
  setScene: (scene) => set({ scene }),
  setSceneUploading: (sceneUploading) => set({ sceneUploading }),
  setSceneError: (sceneError) => set({ sceneError }),
  setHealth: (health) => set({ health }),
  setLanguage: (language) => set({ language }),
  setImage: (image) => set({ image }),
  setSpeakAloud: (speakAloud) => set({ speakAloud }),
  setMissionId: (missionId) => set({ missionId }),
})

const createDialogueSlice: StateCreator<Store, [], [], DialogueSlice> = (set) => {
  const patch = (change: Partial<DialogueState>) =>
    set((state) => ({ dialogue: { ...state.dialogue, ...change } }))

  return {
    dialogue: emptyDialogue,
    resetDialogue: () => set({ dialogue: { ...emptyDialogue, log: [] } }),
    setDialogueStatus: (status) => patch({ status }),
    setDialogueThinking: (thinking) => patch({ thinking }),
    setDialogueSnapshot: (snapshot) => patch({ snapshot }),
    setDialogueError: (error) => patch({ error }),

    applyDialogueEvent: (event) =>
      set((state) => {
        const dialogue: DialogueState = {
          ...state.dialogue,
          thinking: false,
          log: [
            ...state.dialogue.log.slice(-49),
            { at: new Date().toLocaleTimeString(), frame: event },
          ],
        }

        switch (event.type) {
          case 'session':
            return {
              dialogue: { ...dialogue, sessionId: event.session_id, phase: event.phase },
            }
          case 'speak':
            return {
              dialogue: {
                ...dialogue,
                saying: event.text,
                phase: event.phase ?? dialogue.phase,
              },
            }
          case 'plan_ready':
            return {
              dialogue: {
                ...dialogue,
                phase: 'awaiting_confirmation',
                planReady: {
                  plan: event.plan,
                  verified: event.verified,
                  concerns: event.concerns,
                  capped: event.capped,
                  turn_count: event.turn_count,
                },
              },
            }
          case 'execute':
            return { dialogue: { ...dialogue, outcome: 'execute', phase: 'executing' } }
          case 'revise':
            return { dialogue: { ...dialogue, planReady: null, phase: 'clarifying' } }
          case 'cancelled':
            return { dialogue: { ...dialogue, outcome: 'cancelled', phase: 'cancelled' } }
          case 'error':
            return { dialogue: { ...dialogue, error: event.message } }
          default:
            return { dialogue }
        }
      }),
  }
}

const createMissionSlice: StateCreator<Store, [], [], MissionSlice> = (set) => ({
  mission: emptyMission,
  resetMission: () => set({ mission: { ...emptyMission } }),
  setMissionStatus: (status) =>
    set((state) => ({ mission: { ...state.mission, status } })),
  countFrame: () =>
    set((state) => ({ mission: { ...state.mission, framesSent: state.mission.framesSent + 1 } })),

  applyMissionEvent: (event) =>
    set((state) => {
      const mission = { ...state.mission }

      switch (event.type) {
        case 'mission_started':
          return { mission: { ...mission, plan: event.plan, phase: 'executing' } }
        case 'step_started':
          return { mission: { ...mission, cursor: event.index } }
        case 'step_done':
          return {
            mission: {
              ...mission,
              results: [...mission.results, { index: event.index, ...event.result }],
            },
          }
        case 'observation':
          return { mission: { ...mission, digest: event.digest } }
        case 'revision':
          return {
            mission: {
              ...mission,
              revisions: [...mission.revisions, { ...event.revision, applied: event.applied }],
            },
          }
        case 'plan_revised':
          return {
            mission: {
              ...mission,
              plan: event.plan,
              cursor: 0,
              pending: null,
              phase: 'executing',
            },
          }
        case 'awaiting_revision':
          return {
            mission: {
              ...mission,
              pending: { revision: event.revision, plan: event.plan },
              phase: 'awaiting_revision_confirmation',
            },
          }
        case 'halted':
          return { mission: { ...mission, phase: 'halted', pending: null } }
        case 'aborted':
          return { mission: { ...mission, phase: 'aborted', pending: null } }
        case 'mission_ended':
          return { mission: { ...mission, phase: event.phase, ended: true } }
        case 'speak':
          return { mission: { ...mission, saying: event.text } }
        case 'error':
          return { mission: { ...mission, error: event.message } }
        default:
          return { mission }
      }
    }),
})

export const useStore = create<Store>()((...args) => ({
  ...createSessionSlice(...args),
  ...createDialogueSlice(...args),
  ...createMissionSlice(...args),
}))

/** For non-React callers (socket handlers) that need the current value. */
export const storeApi = useStore.getState

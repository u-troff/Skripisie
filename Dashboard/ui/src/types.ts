export interface PlanStep {
  id?: number
  action?: string
  target?: string
}

export interface Plan {
  steps?: PlanStep[]
  notes?: string
}

export type Language = 'af' | 'en'
export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed'

export type DialoguePhase =
  | 'clarifying'
  | 'planning'
  | 'verifying'
  | 'awaiting_confirmation'
  | 'executing'
  | 'reporting'
  | 'cancelled'

export interface Turn {
  question: string
  answer: string | null
}

export interface DialogueSnapshot {
  session_id: string
  phase: DialoguePhase
  command: string
  resolved_command: string
  turns: Turn[]
  turn_count: number
  capped: boolean
  plan: Plan | null
  verified: boolean | null
  concerns: string | null
}

export interface PlanReady {
  plan: Plan
  verified: boolean | null
  concerns: string | null
  capped: boolean
  turn_count: number
}

export type DialogueEvent =
  | { type: 'session'; session_id: string; phase: DialoguePhase }
  | { type: 'speak'; session_id: string; text: string; phase?: DialoguePhase }
  | ({ type: 'plan_ready'; session_id: string } & PlanReady)
  | { type: 'execute'; session_id: string; plan: Plan }
  | { type: 'revise'; session_id: string; objection: string }
  | { type: 'cancelled'; session_id: string; reason: string }
  | { type: 'error'; message: string }

export type MissionPhase =
  | 'executing'
  | 'awaiting_revision_confirmation'
  | 'halted'
  | 'completed'
  | 'aborted'

export type RevisionKind = 'NO_CHANGE' | 'REROUTE' | 'MATERIAL' | 'BLOCKED'

export interface RevisionInfo {
  kind: RevisionKind
  reason: string
  added_targets: string[]
  dropped_targets: string[]
  new_actions: string[]
  step_delta: number
}

export interface StepResult {
  status: 'ok' | 'blocked' | 'halted'
  detail: string | null
}

export type MissionEvent =
  | { type: 'mission_started'; session_id: string; plan: Plan }
  | { type: 'step_started'; session_id: string; index: number; step: PlanStep }
  | { type: 'step_done'; session_id: string; index: number; step: PlanStep; result: StepResult }
  | { type: 'observation'; session_id: string; text: string; digest: string[] }
  | { type: 'revision'; session_id: string; revision: RevisionInfo; plan: Plan; applied: boolean }
  | { type: 'plan_revised'; session_id: string; plan: Plan }
  | { type: 'awaiting_revision'; session_id: string; revision: RevisionInfo; plan: Plan }
  | { type: 'halted'; session_id: string; reason?: string; revision?: RevisionInfo }
  | { type: 'aborted'; session_id: string }
  | { type: 'mission_ended'; session_id: string; phase: MissionPhase; snapshot: unknown }
  | { type: 'speak'; session_id: string; text: string; phase: MissionPhase }
  | { type: 'error'; message: string }

export interface TranscribeResult {
  text: string
  model: string
  language: string
  duration: number | null
  elapsed: number
}

export interface CommandResult {
  status: 'ready' | 'needs_clarification'
  transcript: string
  plan?: Plan
  verified?: boolean | null
  concerns?: string | null
  question?: string
  reason?: string
  elapsed: number
  had_image: boolean
}

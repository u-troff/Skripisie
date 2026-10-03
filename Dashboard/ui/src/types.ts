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

// --- virtual rover live map -------------------------------------------------

export interface RoverPose {
  x: number
  y: number
  /** degrees */
  theta: number
}

export interface RoverState {
  pose: RoverPose
  path: RoverPose[]
  planned_path: Array<[number, number]> | null
  odometer_m: number
  sim_time_s: number
}

export interface RoomData {
  name: string
  width_m: number
  height_m: number
  start: RoverPose
  obstacles: Array<{ name: string; polygon: Array<[number, number]>; where: string | null }>
  landmarks: Array<{ name: string; x: number; y: number; aliases: string[]; where: string | null }>
  source: string
}

export interface RuntimeInfo {
  rover: string
  robot: { length_m: number; width_m: number; clearance_m: number }
}

export interface RunTrace {
  room: RoomData
  settings: { robot_length_m: number; robot_width_m: number; clearance_m: number }
  start: RoverPose
  steps: Array<{
    pose: RoverPose
    planned_path: Array<[number, number]> | null
    status: string
    odometer_m: number
    sim_time_s: number
  }>
  summary: Record<string, unknown>
}

export type MissionEvent =
  | { type: 'rover_state'; session_id: string; state: RoverState }
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
  | { type: 'check'; session_id: string; check: CheckRecord }
  | { type: 'approach_cycle'; session_id: string; check: ApproachCycleRecord }


export interface TranscribeResult {
  text: string
  model: string
  language: string
  duration: number | null
  elapsed: number
}

export interface ApproachCycleRecord {
  kind: 'approach_cycle'
  cycle: number
  phase: string
  t_rel_s: number
  bbox?: [number, number, number, number] | null
  x_center?: number | null
  fill?: number | null
  bottom?: number | null
  confidence?: string | null
  sharpness?: number | null
  gate?: string | null
  reject_reason?: string | null
  action?: string
  frame_path?: string | null
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

export interface SceneObject {
  name: string
  attributes?: string[]
  where?: string
}

export interface SceneFrameInfo {
  index: number
  place: string
  objects: SceneObject[]
  obstacles: string[]
  error: string | null
  /** base64 JPEG, no data: prefix */
  thumbnail: string
}

export interface SceneResponse {
  scene_id: string
  name: string
  frame_count: number
  elapsed: number
  digest: string
  frames: SceneFrameInfo[]
}

/** One row from GET /scenes — a previously catalogued room, picked without
 * re-uploading the video or re-running the VLM inventory. */
export interface SceneSummary {
  scene_id: string
  name: string
  frame_count: number
  created_at: number
}

// --- run logs / reports (Logs page) ---------------------------------------

export interface RunSummary {
  session_id: string
  command: string | null
  outcome: string | null
  rover: string | null
  plan_was_revised: boolean | null
  target_confirmed: boolean
  step_count: number
  checks_completed: number | null
  checks_skipped: number | null
  checks_failed: number | null
  total_s: number | null
  revision_count: number
  mtime: number
}

export interface ReportStep {
  index: number
  action?: string
  target?: string
  status?: string
  detail?: unknown
  elapsed_s?: number
}

export interface CheckResult {
  target_visible?: boolean
  path_clear?: boolean
  confidence?: string
  seen?: string[]
  missing?: string[]
  unexpected?: string[]
  objects?: string[]
  description?: string
}

export interface CheckRecord {
  kind: string
  target?: string
  aimed?: string
  reason?: string
  known_count?: number
  t_rel_s?: number
  est_distance_cm?: number
  latency_s?: number
  frame_path?: string
  // approach-loop records (kind "approach_cycle" / "skipped") carry these
  phase?: string
  cycle?: number
  gate?: string | null
  action?: string
  result?: CheckResult
}

export interface ArrivalRecord {
  kind: string
  target?: string
  expected?: string[]
  known?: string[]
  t_rel_s?: number
  latency_s?: number
  frame_path?: string
  result?: CheckResult
}

export interface RevisionLogEntry {
  at: number
  kind: string
  reason: string
  applied: boolean
}

export interface BendRecord {
  side?: string
  t_rel_s?: number
  est_distance_cm?: number
}

export interface RunReport {
  session_id: string
  outcome: string
  rover: string
  command: string
  resolved_command: string
  plan: Plan
  confirmed_plan: Plan
  plan_was_revised: boolean
  steps: ReportStep[]
  checks: CheckRecord[]
  look_left: CheckRecord | null
  arrival: ArrivalRecord | null
  target_confirmed: CheckRecord | null
  bends: BendRecord[]
  route_summary?: string | null
  looked?: string[]
  scene_id?: string | null
  room_grounding?: unknown
  sonar_obstacle_events?: number
  rover_telemetry?: Record<string, unknown>
  digest: string[]
  revisions: RevisionLogEntry[]
  timings: {
    total_s?: number
    per_step_s?: Array<number | null>
    vlm_latency_mean_s?: number | null
    vlm_latency_max_s?: number | null
    checks_completed?: number
    checks_skipped?: number
    checks_failed?: number
  }
  models?: { planner?: unknown; vlm?: unknown }
  rover_summary?: Record<string, unknown>
  grounding?: unknown[]
  grounding_summary?: unknown
  dialogue?: {
    turn_count?: number
    capped?: boolean
    verified?: boolean | null
    concerns?: string | null
    replan_count?: number
  }
  usage_summary?: unknown
}

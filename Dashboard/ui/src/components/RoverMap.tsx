import { useEffect, useState } from 'react'
import { fetchRoom, fetchRuntime } from '../net'
import { useStore } from '../store'
import type { RoomData, RoverPose, RuntimeInfo } from '../types'

// Self-gating: /api/room returns null unless ROVER=virtual, so this renders nothing
// on a Pi run without the parent needing to know the rover type.
const MARGIN = 24
const TARGET_W = 420

function footprintCorners(pose: RoverPose, lengthM: number, widthM: number): Array<[number, number]> {
  const theta = (pose.theta * Math.PI) / 180
  const halfL = lengthM / 2
  const halfW = widthM / 2
  const cosT = Math.cos(theta)
  const sinT = Math.sin(theta)
  const local: Array<[number, number]> = [
    [halfL, halfW],
    [halfL, -halfW],
    [-halfL, -halfW],
    [-halfL, halfW],
  ]
  return local.map(([lx, ly]) => [pose.x + lx * cosT - ly * sinT, pose.y + lx * sinT + ly * cosT])
}

// Exact for the axis-aligned box obstacles every fixture currently uses, a
// reasonable envelope otherwise — same approximation tools/plot_run.py makes.
function inflatedBox(polygon: Array<[number, number]>, clearanceM: number): Array<[number, number]> {
  const xs = polygon.map((p) => p[0])
  const ys = polygon.map((p) => p[1])
  const minX = Math.min(...xs) - clearanceM
  const maxX = Math.max(...xs) + clearanceM
  const minY = Math.min(...ys) - clearanceM
  const maxY = Math.max(...ys) + clearanceM
  return [
    [minX, minY],
    [maxX, minY],
    [maxX, maxY],
    [minX, maxY],
  ]
}

interface RoomMapViewProps {
  room: RoomData
  robot: { length_m: number; width_m: number; clearance_m: number } | null
  /** Where the rover has been, start first. */
  travelled: Array<[number, number]>
  planned: Array<[number, number]> | null
  pose: RoverPose | null
  /** Where a literal move stopped short on an obstacle or the wall. */
  collisions?: Array<[number, number]>
  footer?: string
}

export function RoomMapView({ room, robot, travelled, planned, pose, collisions, footer }: RoomMapViewProps) {
  const scale = TARGET_W / room.width_m
  const width = room.width_m * scale + 2 * MARGIN
  const height = room.height_m * scale + 2 * MARGIN
  const sx = (x: number) => MARGIN + x * scale
  const sy = (y: number) => MARGIN + (room.height_m - y) * scale
  const poly = (points: Array<[number, number]>) =>
    points.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(' ')

  return (
    <div className="rovermap">
      <svg width={width} height={height} className="roommap-svg">
        <rect
          x={sx(0)}
          y={sy(room.height_m)}
          width={room.width_m * scale}
          height={room.height_m * scale}
          fill="none"
          stroke="#222"
          strokeWidth={2}
        />
        {robot &&
          room.obstacles.map((obstacle) => (
            <polygon
              key={`inflated-${obstacle.name}`}
              points={poly(inflatedBox(obstacle.polygon, robot.clearance_m))}
              fill="#f3ecec"
              stroke="#d9c8c8"
              strokeDasharray="4 3"
            />
          ))}
        {room.obstacles.map((obstacle) => (
          <polygon key={obstacle.name} points={poly(obstacle.polygon)} fill="#e2e2e2" stroke="#9a9a9a" />
        ))}
        {room.landmarks.map((landmark) => (
          <g key={landmark.name}>
            <circle cx={sx(landmark.x)} cy={sy(landmark.y)} r={4} fill="#2a6fdb" />
            <text x={sx(landmark.x) + 7} y={sy(landmark.y) + 4} fill="#2a6fdb" fontSize={11}>
              {landmark.name}
            </text>
          </g>
        ))}
        {planned && <polyline points={poly(planned)} fill="none" stroke="#3d78d1" strokeWidth={1} />}
        <polyline points={poly(travelled)} fill="none" stroke="#444444" strokeWidth={2} />
        {(collisions ?? []).map(([x, y], i) => (
          <g key={`collision-${i}`} stroke="#d93025" strokeWidth={2}>
            <line x1={sx(x) - 5} y1={sy(y) - 5} x2={sx(x) + 5} y2={sy(y) + 5} />
            <line x1={sx(x) - 5} y1={sy(y) + 5} x2={sx(x) + 5} y2={sy(y) - 5} />
          </g>
        ))}
        {pose && robot && (
          <polygon
            points={poly(footprintCorners(pose, robot.length_m, robot.width_m))}
            fill="none"
            stroke="#2e8b57"
            strokeWidth={2}
          />
        )}
      </svg>
      {footer && (
        <p className="note" style={{ marginTop: 4 }}>
          {footer}
        </p>
      )}
    </div>
  )
}

/** Live map for the Mission panel: room from the API, movement from the store. */
export default function RoverMap() {
  const roverState = useStore((state) => state.mission.roverState)
  const [room, setRoom] = useState<RoomData | null>(null)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)

  useEffect(() => {
    void fetchRoom().then(setRoom)
    void fetchRuntime().then(setRuntime)
  }, [])

  if (!room) return null

  const travelled: Array<[number, number]> = [
    [room.start.x, room.start.y],
    ...(roverState?.path ?? []).map((p): [number, number] => [p.x, p.y]),
  ]

  return (
    <>
      <h3>Live map</h3>
      <RoomMapView
        room={room}
        robot={runtime?.robot ?? null}
        travelled={travelled}
        planned={roverState?.planned_path ?? null}
        pose={roverState?.pose ?? null}
        footer={
          roverState
            ? `odometer ${roverState.odometer_m.toFixed(2)}m · sim time ${roverState.sim_time_s.toFixed(1)}s`
            : undefined
        }
      />
    </>
  )
}

/**
 * URL for a saved run frame. The backend records frame_path relative to the
 * brain dir, and older run logs written on Windows use backslashes
 * (logs\frames\<id>\arrival.jpg), so normalise before matching.
 */
export function frameUrl(framePath?: string | null): string | null {
  if (!framePath) return null
  const path = framePath.replace(/\\/g, '/')
  const idx = path.indexOf('logs/frames/')
  return idx === -1 ? null : '/' + path.slice(idx)
}

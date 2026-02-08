/**
 * Geographic utility functions for Chase Mode.
 * Pure math - no external dependencies.
 */

const EARTH_RADIUS_MILES = 3958.8;
const DEG_TO_RAD = Math.PI / 180;
const RAD_TO_DEG = 180 / Math.PI;

/** Haversine distance between two lat/lon points in miles. */
export function distanceMiles(
  lat1: number, lon1: number,
  lat2: number, lon2: number
): number {
  const dLat = (lat2 - lat1) * DEG_TO_RAD;
  const dLon = (lon2 - lon1) * DEG_TO_RAD;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1 * DEG_TO_RAD) * Math.cos(lat2 * DEG_TO_RAD) *
    Math.sin(dLon / 2) * Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_MILES * c;
}

/** Compass bearing from point 1 to point 2 in degrees (0-360). */
export function bearing(
  lat1: number, lon1: number,
  lat2: number, lon2: number
): number {
  const dLon = (lon2 - lon1) * DEG_TO_RAD;
  const y = Math.sin(dLon) * Math.cos(lat2 * DEG_TO_RAD);
  const x =
    Math.cos(lat1 * DEG_TO_RAD) * Math.sin(lat2 * DEG_TO_RAD) -
    Math.sin(lat1 * DEG_TO_RAD) * Math.cos(lat2 * DEG_TO_RAD) * Math.cos(dLon);
  const brng = Math.atan2(y, x) * RAD_TO_DEG;
  return (brng + 360) % 360;
}

/** Convert degrees to cardinal direction (N, NE, E, etc.). */
export function bearingToCardinal(degrees: number): string {
  const dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const index = Math.round(degrees / 45) % 8;
  return dirs[index];
}

/**
 * Ray-casting point-in-polygon test.
 * polygon is an array of [lat, lon] pairs.
 */
export function pointInPolygon(
  lat: number, lon: number,
  polygon: number[][]
): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const yi = polygon[i][0], xi = polygon[i][1];
    const yj = polygon[j][0], xj = polygon[j][1];

    const intersect =
      ((yi > lat) !== (yj > lat)) &&
      (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

/**
 * Distance in miles from a point to the nearest edge of a polygon.
 * Returns 0 if inside the polygon.
 */
export function distanceToPolygon(
  lat: number, lon: number,
  polygon: number[][]
): number {
  if (pointInPolygon(lat, lon, polygon)) return 0;

  let minDist = Infinity;
  for (let i = 0; i < polygon.length; i++) {
    const j = (i + 1) % polygon.length;
    const dist = distanceToSegment(
      lat, lon,
      polygon[i][0], polygon[i][1],
      polygon[j][0], polygon[j][1]
    );
    if (dist < minDist) minDist = dist;
  }
  return minDist;
}

/** Distance from a point to the nearest point on a line segment, in miles. */
function distanceToSegment(
  px: number, py: number,
  ax: number, ay: number,
  bx: number, by: number
): number {
  const dx = bx - ax;
  const dy = by - ay;
  if (dx === 0 && dy === 0) {
    return distanceMiles(px, py, ax, ay);
  }

  let t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
  t = Math.max(0, Math.min(1, t));

  const nearestLat = ax + t * dx;
  const nearestLon = ay + t * dy;
  return distanceMiles(px, py, nearestLat, nearestLon);
}

/** Convert m/s to mph. */
export function msToMph(ms: number): number {
  return ms * 2.237;
}

/**
 * API URL helpers that respect Vite's base path configuration.
 *
 * Locally (base = '/'): apiUrl('/api/alerts') → '/api/alerts'
 * Behind Caddy (base = '/v2/'): apiUrl('/api/alerts') → '/v2/api/alerts'
 */

/** Base path WITHOUT trailing slash. Empty string when base is '/' */
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');

/** Prefix an API path with the configured base path. */
export function apiUrl(path: string): string {
  const p = path.startsWith('/') ? path : '/' + path;
  return `${BASE}${p}`;
}

/** Build the WebSocket URL respecting the base path. */
export function wsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (import.meta.env.DEV) {
    return `${protocol}//${window.location.host}/ws`;
  }
  return `${protocol}//${window.location.host}${BASE}/ws`;
}

/**
 * Sends a request to the backend to manually clear/delete an alert.
 * @param alertId The unique ID of the alert to clear (e.g., product_id).
 * @returns True if successful, false otherwise.
 */
export async function clearAlert(alertId: string): Promise<boolean> {
  try {
    const response = await fetch(apiUrl(`/api/alerts/${alertId}`), {
      method: 'DELETE',
    });
    if (response.ok) {
      console.log(`Alert ${alertId} cleared successfully.`);
      return true;
    }
    console.error(`Failed to clear alert ${alertId}: ${response.status} ${response.statusText}`);
    return false;
  } catch (error) {
    console.error(`Error clearing alert ${alertId}:`, error);
    return false;
  }
}

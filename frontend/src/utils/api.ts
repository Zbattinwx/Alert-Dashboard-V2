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

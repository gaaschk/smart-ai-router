/**
 * Resolve the dashboard backend's origin (protocol + host + port).
 *
 * - In dev (`npm run dev`), Vite's dev server proxies `/api` and `/socket.io`
 *   to the backend (see vite.config.ts), so a relative/same-origin URL works
 *   and we return ''.
 * - In production, the frontend is served as static files by `serve`, which
 *   has no proxy. A relative `/api` call would 200 with `index.html` (SPA
 *   fallback) instead of ever reaching the backend. So we point directly at
 *   the backend port on whatever host the page was loaded from -- this makes
 *   it work whether the dashboard is opened via `localhost`, a LAN IP, or a
 *   `.local` mDNS hostname (e.g. the Mac Mini), without hardcoding a host.
 *
 * Override with VITE_BACKEND_PORT / VITE_BACKEND_ORIGIN at build time if the
 * backend ever runs on a different port or a different host entirely.
 */
export function getBackendOrigin(): string {
  const explicitOrigin = import.meta.env.VITE_BACKEND_ORIGIN as string | undefined;
  if (explicitOrigin) return explicitOrigin.replace(/\/$/, '');

  if (import.meta.env.DEV) return '';

  const backendPort = (import.meta.env.VITE_BACKEND_PORT as string | undefined) || '5050';
  const { protocol, hostname } = window.location;
  return `${protocol}//${hostname}:${backendPort}`;
}

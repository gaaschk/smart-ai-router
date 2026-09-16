import { io, Socket } from 'socket.io-client';
import { getBackendOrigin } from './backendOrigin';

/**
 * One shared Socket.IO connection to the dashboard backend, used for the
 * connection-status indicator and for live `routing_decision` events (e.g. so
 * an Analytics tab can reflect a chat sent from another tab without a
 * refresh). Connects to same-origin in dev because Vite's dev server proxies
 * websocket upgrades for `/socket.io` alongside `/api` (see vite.config.ts).
 * In production (served statically, no proxy) it connects directly to the
 * backend's origin -- see backendOrigin.ts.
 */
let socket: Socket | null = null;

export function getSocket(): Socket {
  if (!socket) {
    const origin = getBackendOrigin();
    socket = io(origin || '/', {
      autoConnect: true,
      reconnection: true,
    });
  }
  return socket;
}

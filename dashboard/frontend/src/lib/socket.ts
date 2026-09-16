import { io, Socket } from 'socket.io-client';

/**
 * One shared Socket.IO connection to the dashboard backend, used for the
 * connection-status indicator and for live `routing_decision` events (e.g. so
 * an Analytics tab can reflect a chat sent from another tab without a
 * refresh). Connects to same-origin in dev because Vite's dev server proxies
 * websocket upgrades for `/socket.io` alongside `/api` (see vite.config.ts).
 */
let socket: Socket | null = null;

export function getSocket(): Socket {
  if (!socket) {
    socket = io('/', {
      autoConnect: true,
      reconnection: true,
    });
  }
  return socket;
}

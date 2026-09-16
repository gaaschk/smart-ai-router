import http from 'http';
import { Server as SocketIOServer } from 'socket.io';
import { createApp } from './app';
import { config } from './config';
import { log } from './middleware/logging';
import { initializePool, runMigrations, closePool } from './services/database';

async function bootstrap(): Promise<void> {
  try {
    // Initialize database pool
    initializePool();
    log('info', 'Database pool initialized', { database: config.database.url });

    // Run migrations
    await runMigrations();

    // Create Express app
    const app = createApp();

    // Create HTTP server
    const server = http.createServer(app);

    // Create Socket.IO server
    const io = new SocketIOServer(server, {
      cors: {
        origin: config.cors.origin,
        methods: ['GET', 'POST'],
        credentials: true,
      },
      pingInterval: config.websocket.pingInterval,
      pingTimeout: config.websocket.pingTimeout,
    });

    // Attach Socket.IO to app for use in routes
    (app as any).io = io;

    // Socket.IO event handlers (will be enhanced later)
    io.on('connection', (socket) => {
      log('info', 'Client connected', { socketId: socket.id });

      socket.on('disconnect', () => {
        log('info', 'Client disconnected', { socketId: socket.id });
      });

      socket.on('error', (err) => {
        log('error', 'Socket error', { socketId: socket.id, error: err });
      });
    });

    // Start server
    server.listen(config.port, () => {
      log('info', `🚀 Dashboard server running on port ${config.port}`, {
        environment: config.nodeEnv,
        smartRouter: config.smartRouter.url,
        gbrainBin: config.gbrain.bin,
      });
    });

    // Graceful shutdown
    const shutdown = async () => {
      log('info', 'Shutting down gracefully...');
      server.close(async () => {
        await closePool();
        log('info', 'Server and database closed');
        process.exit(0);
      });
    };

    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);

  } catch (error) {
    log('error', 'Bootstrap failed', { error });
    process.exit(1);
  }
}

bootstrap();

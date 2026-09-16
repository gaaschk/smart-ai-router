import express, { Express, Request, Response, NextFunction } from 'express';
import cors from 'cors';
import helmet from 'helmet';
import morgan from 'morgan';
import { config } from './config';
import { AppError } from './types';

// Middleware imports
import { errorHandler } from './middleware/error';
import { requestLogger } from './middleware/logging';

// Routes (will be added as we create them)
// import authRoutes from './routes/auth';
// import chatRoutes from './routes/chat';
// import analyticsRoutes from './routes/analytics';
// import memoryRoutes from './routes/memory';
// import skillsRoutes from './routes/skills';
// import usersRoutes from './routes/users';

export function createApp(): Express {
  const app = express();

  // Trust proxy
  app.set('trust proxy', 1);

  // Security middleware
  app.use(helmet());
  app.use(cors({
    origin: config.cors.origin,
    credentials: true,
  }));

  // Body parsing middleware
  app.use(express.json({ limit: '10mb' }));
  app.use(express.urlencoded({ limit: '10mb', extended: true }));

  // Logging middleware
  app.use(morgan('combined'));
  app.use(requestLogger);

  // Health check endpoint
  app.get('/health', (req: Request, res: Response) => {
    res.json({
      status: 'ok',
      timestamp: new Date().toISOString(),
      environment: config.nodeEnv,
    });
  });

  // API Routes (will be added as we build them)
  // app.use('/api/auth', authRoutes);
  // app.use('/api/chat', chatRoutes);
  // app.use('/api/analytics', analyticsRoutes);
  // app.use('/api/memory', memoryRoutes);
  // app.use('/api/skills', skillsRoutes);
  // app.use('/api/users', usersRoutes);

  // 404 handler
  app.use((req: Request, res: Response) => {
    res.status(404).json({
      error: 'Not found',
      path: req.path,
      method: req.method,
    });
  });

  // Error handler (must be last)
  app.use(errorHandler);

  return app;
}

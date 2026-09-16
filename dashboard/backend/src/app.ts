import express, { Express, Request, Response } from 'express';
import cors from 'cors';
import helmet from 'helmet';
import morgan from 'morgan';
import { config } from './config';

// Middleware imports
import { errorHandler } from './middleware/error';
import { requestLogger } from './middleware/logging';

// Routes
import { chatRouter } from './routes/chat';
import { analyticsRouter } from './routes/analytics';
import { memoryRouter } from './routes/memory';
import { skillsRouter } from './routes/skills';
// import authRoutes from './routes/auth';
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
  app.get('/health', (_req: Request, res: Response) => {
    res.json({
      status: 'ok',
      timestamp: new Date().toISOString(),
      environment: config.nodeEnv,
    });
  });

  // API Routes
  app.use('/api/chat', chatRouter);
  app.use('/api/analytics', analyticsRouter);
  app.use('/api/memory', memoryRouter);
  app.use('/api/skills', skillsRouter);
  // app.use('/api/auth', authRoutes);
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

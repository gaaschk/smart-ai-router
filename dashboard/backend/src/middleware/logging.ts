import { Request, Response, NextFunction } from 'express';
import { config } from '../config';

const LOG_LEVELS = {
  error: 0,
  warn: 1,
  info: 2,
  debug: 3,
};

const currentLogLevel = LOG_LEVELS[config.logLevel as keyof typeof LOG_LEVELS] || LOG_LEVELS.info;

function log(level: keyof typeof LOG_LEVELS, message: string, data?: any): void {
  if (LOG_LEVELS[level] <= currentLogLevel) {
    const timestamp = new Date().toISOString();
    const prefix = `[${timestamp}] [${level.toUpperCase()}]`;
    
    if (data) {
      console.log(`${prefix} ${message}`, data);
    } else {
      console.log(`${prefix} ${message}`);
    }
  }
}

export function requestLogger(
  req: Request,
  res: Response,
  next: NextFunction
): void {
  const start = Date.now();

  // Log request
  log('debug', `${req.method} ${req.path}`, {
    ip: req.ip,
    userAgent: req.get('user-agent'),
  });

  // Intercept response
  const originalSend = res.send;
  res.send = function (data: any) {
    const duration = Date.now() - start;
    
    log('info', `${req.method} ${req.path} ${res.statusCode}`, {
      duration: `${duration}ms`,
      size: data?.length,
    });

    return originalSend.call(this, data);
  };

  next();
}

export { log };

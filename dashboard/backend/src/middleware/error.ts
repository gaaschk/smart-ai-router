import { Request, Response, NextFunction } from 'express';
import { AppError } from '../types';
import { log } from './logging';

export function errorHandler(
  err: Error | AppError,
  req: Request,
  res: Response,
  _next: NextFunction
): void {
  const requestId = (req as any).id || 'unknown';
  const isDev = process.env.NODE_ENV === 'development';

  // Determine status code and error details
  let statusCode = 500;
  let errorCode = 'INTERNAL_ERROR';
  let userMessage = 'Internal server error';

  if (err instanceof AppError) {
    statusCode = err.statusCode;
    errorCode = err.code || 'UNKNOWN_ERROR';
    userMessage = err.message;
  }

  // Log error with full context
  const errorLog: any = {
    requestId,
    statusCode,
    errorCode,
    message: err.message,
    path: req.path,
    method: req.method,
    userId: (req as any).userId || null,
    userAgent: req.headers['user-agent'] || 'unknown',
    ip: req.ip || 'unknown',
    timestamp: new Date().toISOString(),
  };

  if (isDev && 'stack' in err) {
    errorLog.stack = (err as any).stack;
  }

  log('error', `${statusCode} ${err.constructor.name}`, errorLog);

  // Return error response
  res.status(statusCode).json({
    error: userMessage,
    code: errorCode,
    ...(isDev && { message: err.message, stack: err.stack }),
    ...(requestId && { requestId }), // Include requestId for debugging
  });
}

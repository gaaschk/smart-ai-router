import { Request, Response, NextFunction } from 'express';
import { v4 as uuidv4 } from 'uuid';

/**
 * Middleware to assign a unique request ID for tracing and debugging.
 * Reads from X-Request-ID header if provided, otherwise generates a new UUID.
 */
export function requestIdMiddleware(req: Request, _res: Response, next: NextFunction): void {
  const requestId = (req.headers['x-request-id'] as string) || uuidv4();
  (req as any).id = requestId;
  next();
}

/**
 * Authentication middleware — JWT + API key support.
 */
import { Request, Response, NextFunction } from 'express';
import { verifyToken, verifyApiKey, getUserById } from '../services/userService';
import { AppError, User } from '../types';

declare global {
  namespace Express {
    interface Request {
      user?: User;
      userId?: string;
    }
  }
}

/**
 * Middleware: require authentication (JWT or API key).
 * Attaches user to req.user and req.userId.
 */
export async function authRequired(req: Request, _res: Response, next: NextFunction) {
  try {
    let userId: string | null = null;

    // Try JWT first (Authorization: Bearer <token>)
    const authHeader = req.headers.authorization;
    if (authHeader?.startsWith('Bearer ')) {
      try {
        const token = authHeader.slice(7);
        const decoded = verifyToken(token);
        userId = decoded.sub;
      } catch {
        // Invalid JWT, try API key next
      }
    }

    // Try API key (X-API-Key header)
    if (!userId) {
      const apiKey = req.headers['x-api-key'];
      if (apiKey && typeof apiKey === 'string') {
        userId = await verifyApiKey(apiKey);
      }
    }

    if (!userId) {
      throw new AppError(401, 'Missing or invalid authentication', 'AUTH_REQUIRED');
    }

    const user = await getUserById(userId);
    if (!user) {
      throw new AppError(401, 'User not found', 'USER_NOT_FOUND');
    }

    req.user = user;
    req.userId = userId;
    next();
  } catch (err) {
    if (err instanceof AppError) {
      next(err);
    } else {
      next(new AppError(401, 'Authentication failed', 'AUTH_ERROR'));
    }
  }
}

/**
 * Middleware: require admin role.
 * Must be placed after authRequired.
 */
export function adminRequired(req: Request, _res: Response, next: NextFunction) {
  if (!req.user || req.user.role !== 'admin') {
    return next(new AppError(403, 'Admin access required', 'ADMIN_REQUIRED'));
  }
  next();
}

/**
 * Optional auth: attach user if authenticated, but don't error if not.
 */
export async function authOptional(req: Request, _res: Response, next: NextFunction) {
  try {
    let userId: string | null = null;

    const authHeader = req.headers.authorization;
    if (authHeader?.startsWith('Bearer ')) {
      const token = authHeader.slice(7);
      const decoded = verifyToken(token);
      userId = decoded.sub;
    }

    if (!userId) {
      const apiKey = req.headers['x-api-key'];
      if (apiKey && typeof apiKey === 'string') {
        userId = await verifyApiKey(apiKey);
      }
    }

    if (userId) {
      const user = await getUserById(userId);
      if (user) {
        req.user = user;
        req.userId = userId;
      }
    }
  } catch {
    // Silently ignore auth errors; it's optional.
  }

  next();
}

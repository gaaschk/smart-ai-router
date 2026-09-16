/**
 * Authentication routes — login, register, API key management.
 */
import { Router, Request, Response, NextFunction } from 'express';
import { createUser, authenticateUser, listApiKeys, createApiKey, revokeApiKey } from '../services/userService';
import { authRequired } from '../middleware/auth';
import { AppError } from '../types';

export const authRouter = Router();

/** POST /api/auth/register — Create a new user account */
authRouter.post('/register', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { email, name, password } = req.body;

    if (!email || !name || !password) {
      throw new AppError(400, 'Missing required fields: email, name, password', 'MISSING_FIELDS');
    }

    if (password.length < 8) {
      throw new AppError(400, 'Password must be at least 8 characters', 'WEAK_PASSWORD');
    }

    const user = await createUser(email, name, password, 'user');
    res.status(201).json({ user });
  } catch (err) {
    next(err);
  }
});

/** POST /api/auth/login — Authenticate and get JWT token */
authRouter.post('/login', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { email, password } = req.body;

    if (!email || !password) {
      throw new AppError(400, 'Missing email or password', 'MISSING_FIELDS');
    }

    const ipAddress = req.ip || req.socket.remoteAddress;
    const { user, token } = await authenticateUser(email, password, ipAddress);

    res.json({ token, user });
  } catch (err) {
    next(err);
  }
});

/** POST /api/auth/api-keys — Create an API key for programmatic access */
authRouter.post('/api-keys', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { name } = req.body;

    if (!name) {
      throw new AppError(400, 'Missing API key name', 'MISSING_FIELDS');
    }

    const { key, id } = await createApiKey(req.userId!, name);

    res.status(201).json({
      id,
      name,
      key, // Only returned once at creation
      message: 'Save this key securely — you will not be able to view it again',
    });
  } catch (err) {
    next(err);
  }
});

/** GET /api/auth/api-keys — List API keys for the current user */
authRouter.get('/api-keys', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const keys = await listApiKeys(req.userId!);
    res.json({ keys });
  } catch (err) {
    next(err);
  }
});

/** DELETE /api/auth/api-keys/:id — Revoke an API key */
authRouter.delete(
  '/api-keys/:id',
  authRequired,
  async (req: Request, res: Response, next: NextFunction) => {
    try {
      await revokeApiKey(req.params.id, req.userId!);
      res.json({ message: 'API key revoked' });
    } catch (err) {
      next(err);
    }
  }
);

/** GET /api/auth/me — Get current user info */
authRouter.get('/me', authRequired, (req: Request, res: Response) => {
  res.json({ user: req.user });
});

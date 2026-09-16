/**
 * Users management routes (admin only) — list, update, disable users.
 */
import { Router, Request, Response, NextFunction } from 'express';
import { authRequired, adminRequired } from '../middleware/auth';
import { listUsers, getUserById } from '../services/userService';
import { query } from '../services/database';
import { AppError } from '../types';

export const usersRouter = Router();

/** GET /api/users — List all users (admin only) with cost/activity summary */
usersRouter.get('/', authRequired, adminRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const limit = Math.min(Number(req.query.limit ?? 50) || 50, 200);
    const offset = Math.max(Number(req.query.offset ?? 0) || 0, 0);

    const { users, total } = await listUsers(limit, offset);

    // Fetch per-user cost and message counts
    const enriched = await Promise.all(
      users.map(async (user) => {
        const costRes = await query(
          `SELECT COUNT(*) as count, SUM(cost_usd) as total_cost
           FROM cost_tracking WHERE user_id = $1`,
          [user.id]
        );

        const messageRes = await query(
          `SELECT COUNT(*) as count FROM chat_messages WHERE user_id = $1`,
          [user.id]
        );

        const cost = costRes.rows[0];
        const msgCount = messageRes.rows[0];

        return {
          ...user,
          stats: {
            totalCost: parseFloat(cost.total_cost || 0),
            messageCount: parseInt(cost.count || 0, 10),
            chatMessageCount: parseInt(msgCount.count || 0, 10),
          },
        };
      })
    );

    res.json({
      users: enriched,
      pagination: { limit, offset, total },
    });
  } catch (err) {
    next(err);
  }
});

/** GET /api/users/:id — Get a specific user's detail (admin only) */
usersRouter.get('/:id', authRequired, adminRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const user = await getUserById(req.params.id);

    if (!user) {
      throw new AppError(404, 'User not found', 'USER_NOT_FOUND');
    }

    // Fetch user's cost data
    const costRes = await query(
      `SELECT 
        COUNT(*) as request_count,
        SUM(cost_usd) as total_cost,
        AVG(cost_usd) as avg_cost,
        MAX(created_at) as last_cost_date
       FROM cost_tracking WHERE user_id = $1`,
      [user.id]
    );

    const costRow = costRes.rows[0];

    res.json({
      user,
      stats: {
        totalCost: parseFloat(costRow.total_cost || 0),
        avgCostPerRequest: parseFloat(costRow.avg_cost || 0),
        requestCount: parseInt(costRow.request_count || 0, 10),
        lastCostDate: costRow.last_cost_date,
      },
    });
  } catch (err) {
    next(err);
  }
});

/** PATCH /api/users/:id — Update user (admin only) — currently role only */
usersRouter.patch('/:id', authRequired, adminRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { role, active } = req.body;

    if (!role && active === undefined) {
      throw new AppError(400, 'No fields to update', 'NO_UPDATE_FIELDS');
    }

    const updates: string[] = [];
    const values: any[] = [];
    let paramIndex = 1;

    if (role) {
      if (!['admin', 'user'].includes(role)) {
        throw new AppError(400, 'Invalid role', 'INVALID_ROLE');
      }
      updates.push(`role = $${paramIndex++}`);
      values.push(role);
    }

    if (active !== undefined) {
      updates.push(`active = $${paramIndex++}`);
      values.push(active);
    }

    values.push(req.params.id);

    const res_ = await query(
      `UPDATE users SET ${updates.join(', ')}, updated_at = CURRENT_TIMESTAMP
       WHERE id = $${paramIndex}
       RETURNING id, email, name, role, created_at, last_login, active`,
      values
    );

    if (res_.rows.length === 0) {
      throw new AppError(404, 'User not found', 'USER_NOT_FOUND');
    }

    const row = res_.rows[0];
    res.json({
      user: {
        id: row.id,
        email: row.email,
        name: row.name,
        role: row.role,
        createdAt: row.created_at,
        lastLogin: row.last_login,
      },
    });
  } catch (err) {
    next(err);
  }
});

/** DELETE /api/users/:id — Soft-delete a user (admin only) */
usersRouter.delete('/:id', authRequired, adminRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    // Prevent admin from deleting themselves
    if (req.params.id === req.userId) {
      throw new AppError(400, 'Cannot delete your own account', 'CANNOT_DELETE_SELF');
    }

    const res_ = await query(
      `UPDATE users SET active = false, updated_at = CURRENT_TIMESTAMP
       WHERE id = $1
       RETURNING id`,
      [req.params.id]
    );

    if (res_.rows.length === 0) {
      throw new AppError(404, 'User not found', 'USER_NOT_FOUND');
    }

    res.json({ message: 'User deactivated' });
  } catch (err) {
    next(err);
  }
});

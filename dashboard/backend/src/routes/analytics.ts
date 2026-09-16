import { Router, Request, Response, NextFunction } from 'express';
import { query } from '../services/database';
import { listModels } from '../services/smartRouterClient';
import { authRequired } from '../middleware/auth';
import { log } from '../middleware/logging';

export const analyticsRouter = Router();

/**
 * GET /api/analytics/usage?days=30
 *
 * Per-user cost aggregation from the cost_tracking table. Returns totals,
 * by-model, by-day, and by-domain breakdowns for the authenticated user only.
 */
analyticsRouter.get('/usage', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const userId = (req as any).userId;
    const days = req.query.days ? Number(req.query.days) : 30;
    const daysNum = Number.isFinite(days) ? days : 30;

    const cutoff = new Date(Date.now() - daysNum * 24 * 60 * 60 * 1000).toISOString();

    // Total aggregation
    const totalsResult = await query(
      `SELECT 
        COUNT(*) as requests,
        COALESCE(SUM(tokens_prompt), 0) as prompt_tokens,
        COALESCE(SUM(tokens_completion), 0) as completion_tokens,
        COALESCE(SUM(cost_usd), 0) as cost_usd
      FROM cost_tracking
      WHERE user_id = $1 AND created_at >= $2`,
      [userId, cutoff]
    );

    const totals = {
      requests: parseInt(totalsResult.rows[0].requests || 0, 10),
      prompt_tokens: parseInt(totalsResult.rows[0].prompt_tokens || 0, 10),
      completion_tokens: parseInt(totalsResult.rows[0].completion_tokens || 0, 10),
      cost_usd: parseFloat(totalsResult.rows[0].cost_usd || 0),
      estimated_rows: 0, // Not relevant for user scoping
    };

    // By model
    const byModelResult = await query(
      `SELECT 
        model as key,
        COUNT(*) as requests,
        COALESCE(SUM(tokens_prompt), 0) as prompt_tokens,
        COALESCE(SUM(tokens_completion), 0) as completion_tokens,
        COALESCE(SUM(cost_usd), 0) as cost_usd
      FROM cost_tracking
      WHERE user_id = $1 AND created_at >= $2
      GROUP BY model
      ORDER BY cost_usd DESC`,
      [userId, cutoff]
    );

    const by_model = byModelResult.rows.map((row: any) => ({
      key: row.key,
      requests: parseInt(row.requests, 10),
      prompt_tokens: parseInt(row.prompt_tokens, 10),
      completion_tokens: parseInt(row.completion_tokens, 10),
      cost_usd: parseFloat(row.cost_usd),
      estimated_rows: 0,
    }));

    // By day
    const byDayResult = await query(
      `SELECT 
        DATE(created_at AT TIME ZONE 'UTC')::text as key,
        COUNT(*) as requests,
        COALESCE(SUM(tokens_prompt), 0) as prompt_tokens,
        COALESCE(SUM(tokens_completion), 0) as completion_tokens,
        COALESCE(SUM(cost_usd), 0) as cost_usd
      FROM cost_tracking
      WHERE user_id = $1 AND created_at >= $2
      GROUP BY DATE(created_at AT TIME ZONE 'UTC')
      ORDER BY key ASC`,
      [userId, cutoff]
    );

    const by_day = byDayResult.rows.map((row: any) => ({
      key: row.key,
      requests: parseInt(row.requests, 10),
      prompt_tokens: parseInt(row.prompt_tokens, 10),
      completion_tokens: parseInt(row.completion_tokens, 10),
      cost_usd: parseFloat(row.cost_usd),
      estimated_rows: 0,
    }));

    // By domain
    const byDomainResult = await query(
      `SELECT 
        COALESCE(domain, 'unknown') as key,
        COUNT(*) as requests,
        COALESCE(SUM(tokens_prompt), 0) as prompt_tokens,
        COALESCE(SUM(tokens_completion), 0) as completion_tokens,
        COALESCE(SUM(cost_usd), 0) as cost_usd
      FROM cost_tracking
      WHERE user_id = $1 AND created_at >= $2
      GROUP BY domain
      ORDER BY cost_usd DESC`,
      [userId, cutoff]
    );

    const by_domain = byDomainResult.rows.map((row: any) => ({
      key: row.key,
      requests: parseInt(row.requests, 10),
      prompt_tokens: parseInt(row.prompt_tokens, 10),
      completion_tokens: parseInt(row.completion_tokens, 10),
      cost_usd: parseFloat(row.cost_usd),
      estimated_rows: 0,
    }));

    res.json({
      totals,
      by_model,
      by_day,
      by_domain,
      by_classifier: [], // Not tracked in cost_tracking; could extend schema
      by_user: null, // Only for admin view (see /api/analytics/admin/usage)
    });
  } catch (err) {
    log('error', 'Failed to fetch usage summary', { error: err });
    next(err);
  }
});

/**
 * GET /api/analytics/models
 *
 * The live catalog smart-ai-router can currently route to — cost, context
 * window, capability flags — so the dashboard can show *why* a cheaper model
 * did or didn't qualify, not just what was picked.
 */
analyticsRouter.get('/models', async (_req: Request, res: Response, next: NextFunction) => {
  try {
    const models = await listModels();
    res.json({ data: models });
  } catch (err) {
    next(err);
  }
});

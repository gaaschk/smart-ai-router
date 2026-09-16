import { Router, Request, Response, NextFunction } from 'express';
import { getUsageSummary, listModels } from '../services/smartRouterClient';

export const analyticsRouter = Router();

/**
 * GET /api/analytics/usage?days=30
 *
 * Passes through smart-ai-router's own usage aggregation. The router already
 * computes everything the Analytics page needs (totals, by-model, by-day,
 * savings-relevant breakdowns) in SQL, so this endpoint is a pure pass-through
 * rather than a second aggregation layer the dashboard would have to keep in
 * sync with the router's usage_log schema.
 */
analyticsRouter.get('/usage', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const days = req.query.days ? Number(req.query.days) : 30;
    const summary = await getUsageSummary(Number.isFinite(days) ? days : 30);
    res.json(summary);
  } catch (err) {
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

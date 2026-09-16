/**
 * Skills routes.
 *
 * GBrain doesn't have a generic "skill" concept with arbitrary trigger
 * parameters -- what it has is:
 *   1. Integration recipes ("senses"/"reflexes"): pluggable data sources
 *      (email, calendar, X, voice, ...) with their own setup + secrets.
 *   2. Background jobs (the "Minions" queue): built-in job types
 *      (sync, embed, lint, import, extract, backlinks, autopilot-cycle)
 *      that can be submitted, tracked, retried, and cancelled.
 *
 * This route exposes both under /api/skills so the dashboard's Skills page
 * has one real, triggerable action list instead of a fake generic API.
 */
import { Router, Request, Response, NextFunction } from 'express';
import * as gbrain from '../services/gbrainClient';

export const skillsRouter = Router();

/** Integration recipes -- the closest thing GBrain has to a "skill library". */
skillsRouter.get('/integrations', async (_req: Request, res: Response, next: NextFunction) => {
  try {
    const integrations = await gbrain.listIntegrations();
    res.json(integrations);
  } catch (err) {
    next(err);
  }
});

skillsRouter.get(
  '/integrations/:id/status',
  async (req: Request, res: Response, next: NextFunction) => {
    try {
      const status = await gbrain.getIntegrationStatus(req.params.id);
      res.json(status);
    } catch (err) {
      next(err);
    }
  }
);

/**
 * Built-in Minions job types the dashboard can safely submit on demand.
 * `shell` is deliberately excluded -- the MCP layer itself rejects it, and
 * we don't want an arbitrary-command trigger reachable from a web UI anyway.
 */
const RUNNABLE_JOBS = ['sync', 'embed', 'lint', 'import', 'extract', 'backlinks', 'autopilot-cycle'] as const;
type RunnableJob = (typeof RUNNABLE_JOBS)[number];

skillsRouter.get('/jobs/catalog', (_req: Request, res: Response) => {
  res.json({
    jobs: RUNNABLE_JOBS.map((name) => ({
      id: name,
      name,
      description: describeJob(name),
    })),
  });
});

skillsRouter.get('/jobs', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { status, queue, name } = req.query;
    const limit = Math.min(Number(req.query.limit ?? 20) || 20, 100);
    const jobs = await gbrain.listJobs({
      status: status ? String(status) : undefined,
      queue: queue ? String(queue) : undefined,
      name: name ? String(name) : undefined,
      limit,
    });
    res.json({ jobs });
  } catch (err) {
    next(err);
  }
});

skillsRouter.get('/jobs/:id', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const id = Number(req.params.id);
    if (!Number.isFinite(id)) {
      res.status(400).json({ error: 'id must be numeric' });
      return;
    }
    const job = await gbrain.getJob(id);
    res.json({ job });
  } catch (err) {
    next(err);
  }
});

skillsRouter.post('/jobs', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const name = String(req.body?.name ?? '') as RunnableJob;
    if (!RUNNABLE_JOBS.includes(name)) {
      res.status(400).json({
        error: `Unsupported job "${name}". Allowed: ${RUNNABLE_JOBS.join(', ')}`,
      });
      return;
    }
    const params = req.body?.params && typeof req.body.params === 'object' ? req.body.params : {};
    const job = await gbrain.submitJob(name, params);

    const io = (req.app as any).io;
    if (io) {
      io.emit('skill_execution', {
        type: 'skill_execution',
        data: {
          id: String(job.id),
          skillId: job.name,
          parameters: params,
          status: job.status,
          startedAt: job.created_at,
        },
      });
    }

    res.status(201).json({ job });
  } catch (err) {
    next(err);
  }
});

function describeJob(name: RunnableJob): string {
  switch (name) {
    case 'sync':
      return 'Incrementally sync a git repo into the brain';
    case 'embed':
      return 'Generate/refresh embeddings for semantic search';
    case 'lint':
      return 'Catch LLM artifacts, placeholder dates, and bad frontmatter';
    case 'import':
      return 'Import a markdown directory into the brain';
    case 'extract':
      return 'Extract links and/or timeline entries from page content';
    case 'backlinks':
      return 'Find and fix missing back-links across the brain';
    case 'autopilot-cycle':
      return 'Run one overnight-maintenance enrichment cycle now';
    default:
      return '';
  }
}

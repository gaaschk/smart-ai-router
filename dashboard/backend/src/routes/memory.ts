/**
 * Memory routes -- search and browse the GBrain knowledge base.
 *
 * GBrain doesn't have a separate "entities" API: a person/company/topic is
 * just a page (optionally tagged and linked to other pages). So the
 * entity/relationship browsing the dashboard plan calls for is exposed here
 * as page detail: a page plus its tags, outgoing links, and backlinks.
 */
import { Router, Request, Response, NextFunction } from 'express';
import * as gbrain from '../services/gbrainClient';

export const memoryRouter = Router();

/** Brain-wide stats + health, shown at the top of the Memory page. */
memoryRouter.get('/stats', async (_req: Request, res: Response, next: NextFunction) => {
  try {
    const [stats, health] = await Promise.all([gbrain.getStats(), gbrain.getHealth()]);
    res.json({ stats, health });
  } catch (err) {
    next(err);
  }
});

/**
 * Search the brain. `mode=hybrid` (default) uses vector + keyword + query
 * expansion; `mode=keyword` is a plain full-text search (faster, no LLM
 * calls, useful when smart-ai-router / embeddings aren't configured).
 */
memoryRouter.get('/search', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const query = String(req.query.q ?? '').trim();
    if (!query) {
      res.json({ results: [] });
      return;
    }
    const limit = Math.min(Number(req.query.limit ?? 10) || 10, 50);
    const mode = req.query.mode === 'keyword' ? 'keyword' : 'hybrid';

    const results =
      mode === 'keyword'
        ? await gbrain.search(query, limit)
        : await gbrain.hybridQuery(query, limit, true);

    res.json({ mode, results });
  } catch (err) {
    next(err);
  }
});

/** List pages, optionally filtered by type/tag -- the browsable page index. */
memoryRouter.get('/pages', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const { type, tag } = req.query;
    const limit = Math.min(Number(req.query.limit ?? 50) || 50, 200);
    const pages = await gbrain.listPages({
      type: type ? String(type) : undefined,
      tag: tag ? String(tag) : undefined,
      limit,
    });
    res.json({ pages });
  } catch (err) {
    next(err);
  }
});

/**
 * One page's full detail: content, tags, outgoing links, and backlinks.
 * Slug is passed as a query param (not a path segment) since GBrain slugs
 * routinely contain slashes, e.g. "src/tests/readme".
 */
memoryRouter.get('/page', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const slug = String(req.query.slug ?? '').trim();
    if (!slug) {
      res.status(400).json({ error: 'slug query param is required' });
      return;
    }
    const [page, tags, links, backlinks] = await Promise.all([
      gbrain.getPage(slug),
      gbrain.getTags(slug).catch(() => []),
      gbrain.getLinks(slug).catch(() => []),
      gbrain.getBacklinks(slug).catch(() => []),
    ]);
    res.json({ page, tags, links, backlinks });
  } catch (err) {
    next(err);
  }
});

/** Link graph around a page -- the "relationship browser" view. */
memoryRouter.get('/graph', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const slug = String(req.query.slug ?? '').trim();
    if (!slug) {
      res.status(400).json({ error: 'slug query param is required' });
      return;
    }
    const depth = Math.min(Number(req.query.depth ?? 1) || 1, 4);
    const nodes = await gbrain.traverseGraph(slug, depth);
    res.json({ nodes });
  } catch (err) {
    next(err);
  }
});

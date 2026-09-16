/**
 * Thin client for GBrain.
 *
 * Unlike smart-ai-router, GBrain does not expose an HTTP API. It ships as a
 * CLI (`gbrain call <tool> '<json-args>'`) and a stdio-only MCP server
 * (`gbrain serve`) meant for editor/agent integrations, not for a Node
 * backend to reach over a socket. So this client shells out to the CLI's
 * `call` subcommand, which is a one-shot, non-interactive JSON-in/JSON-out
 * bridge to the exact same 40+ tools the MCP server exposes (see
 * `gbrain --tools-json`).
 *
 * GBrain's default embedded store (PGLite) takes an exclusive file lock for
 * the lifetime of each CLI invocation, so two `gbrain call` processes cannot
 * run concurrently against the same brain -- the second one blocks on the
 * lock and eventually times out. We serialize all calls through a tiny
 * in-process queue to keep every request instead of racing and dropping one.
 */
import { execFile } from 'child_process';
import { config } from '../config';
import { AppError } from '../types';

export interface GBrainPageSummary {
  slug: string;
  type: string;
  title: string;
  updated_at: string;
}

export interface GBrainSearchResult {
  slug: string;
  page_id: number;
  title: string;
  type: string;
  chunk_text: string;
  chunk_source: string;
  chunk_id: number;
  chunk_index: number;
  score: number;
  stale: boolean;
  source_id: string;
}

export interface GBrainStats {
  page_count: number;
  chunk_count: number;
  embedded_count: number;
  link_count: number;
  tag_count: number;
  timeline_entry_count: number;
  pages_by_type: Record<string, number>;
}

export interface GBrainHealth {
  page_count: number;
  embed_coverage: number;
  stale_pages: number;
  orphan_pages: number;
  missing_embeddings: number;
  brain_score: number;
  dead_links: number;
  link_coverage: number;
  timeline_coverage: number;
  most_connected: unknown[];
}

export interface GBrainIntegration {
  id: string;
  name: string;
  version: string;
  description: string;
  category: string;
  status: string;
  setup_time: string;
  requires: string[];
}

export interface GBrainIntegrationsList {
  infra: GBrainIntegration[];
  senses: GBrainIntegration[];
  reflexes: GBrainIntegration[];
}

export interface GBrainJob {
  id: number;
  name: string;
  queue: string;
  status: string;
  priority: number;
  data: Record<string, unknown>;
  attempts_made: number;
  max_attempts: number;
  result: unknown;
  progress: unknown;
  error_text: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

/** Simple FIFO mutex so we never run two `gbrain` CLI calls at once (PGLite lock). */
class CallQueue {
  private tail: Promise<unknown> = Promise.resolve();

  run<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.tail.then(fn, fn);
    // Swallow rejections in the chain itself so one failed call doesn't wedge
    // the queue for everything queued after it.
    this.tail = result.catch(() => undefined);
    return result;
  }
}

const queue = new CallQueue();

function runGBrainCli(argv: string[], label: string): Promise<unknown> {
  return queue.run(
    () =>
      new Promise((resolve, reject) => {
        execFile(
          config.gbrain.bin,
          argv,
          { timeout: config.gbrain.cliTimeoutMs, maxBuffer: 10 * 1024 * 1024 },
          (error, stdout, stderr) => {
            if (error) {
              const message = (stderr || error.message || '').trim();
              const isMissingBinary = (error as NodeJS.ErrnoException).code === 'ENOENT';
              reject(
                new AppError(
                  isMissingBinary ? 503 : 502,
                  isMissingBinary
                    ? `gbrain CLI not found (looked for "${config.gbrain.bin}" on PATH)`
                    : `gbrain ${label} failed: ${message || 'unknown error'}`,
                  'GBRAIN_ERROR'
                )
              );
              return;
            }
            const text = stdout.trim();
            if (!text) {
              resolve(null);
              return;
            }
            try {
              resolve(JSON.parse(text));
            } catch {
              // Some subcommands (or CLI-level errors printed to stdout) aren't JSON.
              resolve(text);
            }
          }
        );
      })
  );
}

/** Invoke one of the ~40 MCP tools via `gbrain call <tool> '<json>'` (see `gbrain --tools-json`). */
function runGBrainCall(tool: string, args: Record<string, unknown>): Promise<unknown> {
  return runGBrainCli(['call', tool, JSON.stringify(args)], tool);
}

export async function getStats(): Promise<GBrainStats> {
  return runGBrainCall('get_stats', {}) as Promise<GBrainStats>;
}

export async function getHealth(): Promise<GBrainHealth> {
  return runGBrainCall('get_health', {}) as Promise<GBrainHealth>;
}

export async function listPages(params: {
  type?: string;
  tag?: string;
  limit?: number;
} = {}): Promise<GBrainPageSummary[]> {
  return runGBrainCall('list_pages', params) as Promise<GBrainPageSummary[]>;
}

export async function getPage(slug: string, fuzzy = false): Promise<unknown> {
  return runGBrainCall('get_page', { slug, fuzzy });
}

/** Keyword (full-text) search -- fast, no LLM involved. */
export async function search(query: string, limit = 10): Promise<GBrainSearchResult[]> {
  return runGBrainCall('search', { query, limit }) as Promise<GBrainSearchResult[]>;
}

/** Hybrid vector + keyword search with multi-query expansion -- the "smart" search. */
export async function hybridQuery(
  query: string,
  limit = 10,
  expand = true
): Promise<GBrainSearchResult[]> {
  return runGBrainCall('query', { query, limit, expand }) as Promise<GBrainSearchResult[]>;
}

export async function getTags(slug: string): Promise<string[]> {
  return runGBrainCall('get_tags', { slug }) as Promise<string[]>;
}

export async function getLinks(slug: string): Promise<unknown[]> {
  return runGBrainCall('get_links', { slug }) as Promise<unknown[]>;
}

export async function getBacklinks(slug: string): Promise<unknown[]> {
  return runGBrainCall('get_backlinks', { slug }) as Promise<unknown[]>;
}

export async function traverseGraph(
  slug: string,
  depth = 1
): Promise<unknown[]> {
  return runGBrainCall('traverse_graph', { slug, depth }) as Promise<unknown[]>;
}

/**
 * Available integration recipes -- GBrain's "senses/reflexes" that plug external
 * data sources (email, calendar, X/Twitter, voice, ...) into the brain. This is
 * the closest concept GBrain has to a "skill library"; it's not an MCP tool, so
 * it goes through `gbrain integrations list --json` instead of `gbrain call`.
 */
export async function listIntegrations(): Promise<GBrainIntegrationsList> {
  return runGBrainCli(
    ['integrations', 'list', '--json'],
    'integrations list'
  ) as Promise<GBrainIntegrationsList>;
}

export interface GBrainIntegrationStatus {
  id: string;
  status: string;
  secrets: {
    set: string[];
    missing: { name: string; where: string }[];
  };
  heartbeat: {
    total_events: number;
    last_event: string | null;
  };
}

export async function getIntegrationStatus(id: string): Promise<GBrainIntegrationStatus> {
  return runGBrainCli(
    ['integrations', 'status', id, '--json'],
    'integrations status'
  ) as Promise<GBrainIntegrationStatus>;
}

/** Background jobs (Minions queue) -- sync/embed/lint/import/extract/etc. */
export async function listJobs(params: {
  status?: string;
  queue?: string;
  name?: string;
  limit?: number;
} = {}): Promise<GBrainJob[]> {
  return runGBrainCall('list_jobs', params) as Promise<GBrainJob[]>;
}

export async function getJob(id: number): Promise<GBrainJob> {
  return runGBrainCall('get_job', { id }) as Promise<GBrainJob>;
}

export async function submitJob(
  name: string,
  data: Record<string, unknown> = {},
  extra: { queue?: string; priority?: number } = {}
): Promise<GBrainJob> {
  return runGBrainCall('submit_job', { name, data, ...extra }) as Promise<GBrainJob>;
}

/**
 * Save a fact/learning to the brain using the `remember` tool.
 * This persists the learning as a brain page so it can be retrieved in future chats.
 *
 * @param title Short title of the learning
 * @param content Full learning text
 * @param entity Optional entity to tag (e.g. "people/me" for personal facts)
 * @returns GBrain's response object
 */
export async function remember(
  title: string,
  content: string,
  entity?: string
): Promise<unknown> {
  return runGBrainCall('remember', {
    fact: `${title}\n\n${content}`,
    visibility: 'private',
    entity: entity || 'chat-learnings',
  });
}

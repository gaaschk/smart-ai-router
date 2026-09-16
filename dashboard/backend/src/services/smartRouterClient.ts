/**
 * Thin client for the smart-ai-router HTTP API.
 *
 * The dashboard backend talks to smart-ai-router exactly like any other
 * OpenAI-compatible client would — POST /v1/chat/completions for a turn, plus
 * the router's own /api/usage and /api/whoami endpoints for the dashboard
 * views that need routing/cost data a plain OpenAI client has no concept of.
 *
 * A single axios instance is shared so the base URL, auth header, and timeout
 * stay in one place; every call site here is a thin wrapper that shapes the
 * request/response rather than repeating that plumbing.
 */
import axios, { AxiosInstance, AxiosResponse } from 'axios';
import { config } from '../config';
import { AppError } from '../types';

export interface SmartRouterChatResult {
  content: string;
  modelUsed: string;
  costUsd: number | null;
  promptTokens: number;
  completionTokens: number;
  routingWhy: string;
  domain: string;
  complexity: string;
  escalated: boolean;
  qualified: boolean;
  raw: unknown;
}

export interface UsageBucket {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  estimated_rows: number;
}

export interface UsageGroupRow extends UsageBucket {
  key: string;
}

export interface UsageSummary {
  totals: UsageBucket;
  by_model: UsageGroupRow[];
  by_day: UsageGroupRow[];
  by_domain: UsageGroupRow[];
  by_classifier: UsageGroupRow[];
  by_user?: UsageGroupRow[] | null;
  overhead: {
    totals: UsageBucket;
    by_kind: UsageGroupRow[];
    by_model: UsageGroupRow[];
  };
}

export interface WhoAmI {
  authenticated: boolean;
  kind: string;
  user?: string;
  is_admin?: boolean;
}

function client(): AxiosInstance {
  return axios.create({
    baseURL: config.smartRouter.url,
    timeout: config.smartRouter.timeout,
    headers: config.smartRouter.apiKey
      ? { Authorization: `Bearer ${config.smartRouter.apiKey}` }
      : {},
    validateStatus: () => true, // we inspect status ourselves for clean errors
  });
}

function assertOk(resp: AxiosResponse, action: string): void {
  if (resp.status >= 400) {
    const detail =
      (resp.data && (resp.data.detail || resp.data.error)) || resp.statusText;
    throw new AppError(
      resp.status,
      `smart-ai-router ${action} failed: ${detail}`,
      'SMART_ROUTER_ERROR'
    );
  }
}

/**
 * Send one chat turn through smart-ai-router's OpenAI-compatible endpoint and
 * return both the reply and the routing metadata the router reports in
 * X-* response headers (which a plain OpenAI client would never read).
 */
export async function chatCompletion(
  messages: { role: string; content: string }[]
): Promise<SmartRouterChatResult> {
  const resp = await client().post('/v1/chat/completions', {
    model: 'auto',
    messages,
    stream: false,
  });
  assertOk(resp, 'chat completion');

  const data = resp.data;
  const message = data?.choices?.[0]?.message?.content ?? '';
  const usage = data?.usage ?? {};
  const headers = resp.headers || {};

  const promptTokens = Number(usage.prompt_tokens ?? 0);
  const completionTokens = Number(usage.completion_tokens ?? 0);
  const routedModel = String(headers['x-routed-model'] ?? data?.model ?? 'unknown');

  let costUsd: number | null = null;
  try {
    const costResp = await client().post('/api/cost', {
      model: routedModel,
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
    });
    if (costResp.status < 400) {
      costUsd = costResp.data?.cost_usd ?? null;
    }
  } catch {
    costUsd = null; // cost lookup is best-effort; a chat reply must not fail over it
  }

  return {
    content: message,
    modelUsed: routedModel,
    costUsd,
    promptTokens,
    completionTokens,
    routingWhy: String(headers['x-routing-why'] ?? ''),
    domain: String(headers['x-domain'] ?? ''),
    complexity: String(headers['x-complexity'] ?? ''),
    escalated: String(headers['x-escalated'] ?? '') === 'true',
    qualified: String(headers['x-qualified'] ?? 'true') !== 'false',
    raw: data,
  };
}

/** Aggregated usage/cost data for the Analytics page (GET /api/usage). */
export async function getUsageSummary(days = 30): Promise<UsageSummary> {
  const resp = await client().get('/api/usage', { params: { days } });
  assertOk(resp, 'usage summary');
  return resp.data as UsageSummary;
}

/** Who the configured API key authenticates as (used for a connectivity check). */
export async function whoAmI(): Promise<WhoAmI> {
  const resp = await client().get('/api/whoami');
  assertOk(resp, 'whoami');
  return resp.data as WhoAmI;
}

/** Live model catalog (GET /api/models) — used to show what the router can route to. */
export async function listModels(): Promise<unknown[]> {
  const resp = await client().get('/api/models');
  assertOk(resp, 'list models');
  return resp.data as unknown[];
}

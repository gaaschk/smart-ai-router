import axios from 'axios';

/**
 * Talks to the dashboard backend (not smart-ai-router directly — the backend
 * proxies chat/analytics calls so the browser never needs the router's own
 * API key). Vite's dev server proxies `/api` to the backend (see
 * vite.config.ts), so a relative base URL works in both dev and prod.
 */
export const api = axios.create({
  baseURL: '/api',
  timeout: 60000, // chat replies can take a while on a reasoning model
});

export interface ChatRoutingInfo {
  why: string;
  domain: string;
  complexity: string;
  escalated: boolean;
  qualified: boolean;
}

export interface ChatApiResponse {
  id: string;
  message: string;
  modelUsed: string;
  cost: number | null;
  promptTokens: number;
  completionTokens: number;
  routing: ChatRoutingInfo;
  timestamp: string;
}

export async function sendChatMessage(
  message: string,
  history: { role: string; content: string }[] = []
): Promise<ChatApiResponse> {
  const { data } = await api.post<ChatApiResponse>('/chat', { message, history });
  return data;
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
}

export async function fetchUsageSummary(days = 30): Promise<UsageSummary> {
  const { data } = await api.get<UsageSummary>('/analytics/usage', { params: { days } });
  return data;
}

import axios from 'axios';
import { getBackendOrigin } from './backendOrigin';

/**
 * Talks to the dashboard backend (not smart-ai-router directly — the backend
 * proxies chat/analytics calls so the browser never needs the router's own
 * API key). Vite's dev server proxies `/api` to the backend (see
 * vite.config.ts), so a relative base URL works there.
 *
 * In production the frontend is served as static files by `serve`, which has
 * no proxy -- a relative `/api` request would just 200 with `index.html`
 * (SPA fallback) instead of reaching the backend. So in production we target
 * the backend's actual origin directly, computed from the page's own
 * hostname (works from `localhost`, a LAN IP, or a `.local` hostname alike).
 *
 * Authorization: Bearer tokens are set in App.tsx whenever the auth state changes.
 */
export const api = axios.create({
  baseURL: `${getBackendOrigin()}/api`,
  timeout: 60000, // chat replies can take a while on a reasoning model
});

// Add response interceptor to handle 401 (unauthorized)
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Clear auth state and redirect to login
      localStorage.removeItem('authToken');
      localStorage.removeItem('user');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

export interface ChatRoutingInfo {
  why: string;
  domain: string;
  complexity: string;
  escalated: boolean;
  qualified: boolean;
}

export interface ChatApiResponse {
  conversationId: string;
  messageId: string;
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
  conversationId?: string,
  history: { role: string; content: string }[] = []
): Promise<ChatApiResponse> {
  const { data } = await api.post<ChatApiResponse>('/chat', { message, conversationId, history });
  return data;
}

// ---------------------------------------------------------------------------
// Conversation History
// ---------------------------------------------------------------------------

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  archived: boolean;
}

export async function fetchConversations(limit = 20, offset = 0): Promise<{
  conversations: Conversation[];
  total: number;
  limit: number;
  offset: number;
}> {
  const { data } = await api.get('/chat/conversations', { params: { limit, offset } });
  return data;
}

export interface StoredChatMessage {
  id: string;
  role: string;
  content: string;
  modelUsed?: string;
  routing?: Record<string, unknown>;
  tokens?: { prompt: number; completion: number };
  cost?: number;
  timestamp: string;
}

export async function fetchConversationHistory(conversationId: string): Promise<{
  conversationId: string;
  messages: StoredChatMessage[];
}> {
  const { data } = await api.get(`/chat/conversations/${conversationId}`);
  return data;
}

export async function updateConversation(
  conversationId: string,
  updates: { title?: string; archived?: boolean }
): Promise<{ success: boolean }> {
  const { data } = await api.patch(`/chat/conversations/${conversationId}`, updates);
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

// ---------------------------------------------------------------------------
// Memory (GBrain)
// ---------------------------------------------------------------------------

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
}

export async function fetchMemoryStats(): Promise<{ stats: GBrainStats; health: GBrainHealth }> {
  const { data } = await api.get('/memory/stats');
  return data;
}

export interface MemorySearchResult {
  slug: string;
  page_id: number;
  title: string;
  type: string;
  chunk_text: string;
  score: number;
  stale: boolean;
}

export async function searchMemory(
  query: string,
  mode: 'hybrid' | 'keyword' = 'hybrid',
  limit = 10
): Promise<MemorySearchResult[]> {
  if (!query.trim()) return [];
  const { data } = await api.get<{ results: MemorySearchResult[] }>('/memory/search', {
    params: { q: query, mode, limit },
  });
  return data.results;
}

export interface MemoryPageSummary {
  slug: string;
  type: string;
  title: string;
  updated_at: string;
}

export async function listMemoryPages(params: {
  type?: string;
  tag?: string;
  limit?: number;
} = {}): Promise<MemoryPageSummary[]> {
  const { data } = await api.get<{ pages: MemoryPageSummary[] }>('/memory/pages', { params });
  return data.pages;
}

export async function fetchMemoryPage(slug: string): Promise<{
  page: unknown;
  tags: string[];
  links: unknown[];
  backlinks: unknown[];
}> {
  const { data } = await api.get('/memory/page', { params: { slug } });
  return data;
}

// ---------------------------------------------------------------------------
// Skills (GBrain integrations + background jobs)
// ---------------------------------------------------------------------------

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

export async function fetchIntegrations(): Promise<GBrainIntegrationsList> {
  const { data } = await api.get<GBrainIntegrationsList>('/skills/integrations');
  return data;
}

export interface JobCatalogEntry {
  id: string;
  name: string;
  description: string;
}

export async function fetchJobCatalog(): Promise<JobCatalogEntry[]> {
  const { data } = await api.get<{ jobs: JobCatalogEntry[] }>('/skills/jobs/catalog');
  return data.jobs;
}

export interface GBrainJob {
  id: number;
  name: string;
  status: string;
  queue: string;
  created_at: string;
  finished_at: string | null;
  error_text: string | null;
}

export async function listJobs(limit = 10): Promise<GBrainJob[]> {
  const { data } = await api.get<{ jobs: GBrainJob[] }>('/skills/jobs', { params: { limit } });
  return data.jobs;
}

export async function submitJob(name: string, params: Record<string, unknown> = {}): Promise<GBrainJob> {
  const { data } = await api.post<{ job: GBrainJob }>('/skills/jobs', { name, params });
  return data.job;
}

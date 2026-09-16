// User and Authentication Types
export interface User {
  id: string;
  email: string;
  name: string;
  role: 'admin' | 'user';
  createdAt: Date;
  lastLogin?: Date;
}

export interface Session {
  id: string;
  userId: string;
  token: string;
  createdAt: Date;
  expiresAt: Date;
}

export interface ApiKey {
  id: string;
  userId: string;
  name: string;
  keyHash: string;
  createdAt: Date;
  lastUsed?: Date;
  revokedAt?: Date;
}

// Smart Router Types
export interface RoutingDecision {
  id: string;
  userId: string;
  modelRequested: string;
  modelSelected: string;
  cost: number;
  reasoning: string;
  domainProfile?: Record<string, unknown>;
  timestamp: Date;
}

export interface ChatMessage {
  id: string;
  userId: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  modelUsed?: string;
  routingCost?: number;
  timestamp: Date;
}

export interface CostAnalytics {
  totalCost: number;
  averageCostPerRequest: number;
  modelUsageDistribution: Record<string, number>;
  costTrend: { date: Date; cost: number }[];
  savings: number;
}

// GBrain Types
export interface MemoryItem {
  id: string;
  userId: string;
  content: string;
  metadata?: Record<string, unknown>;
  createdAt: Date;
  updatedAt: Date;
}

export interface Entity {
  id: string;
  type: string; // person, company, topic, etc.
  name: string;
  description?: string;
  relationships: EntityRelationship[];
}

export interface EntityRelationship {
  targetId: string;
  type: string; // works_at, invested_in, etc.
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
}

export interface SkillExecution {
  id: string;
  userId: string;
  skillId: string;
  parameters: Record<string, unknown>;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: Record<string, unknown>;
  error?: string;
  startedAt: Date;
  completedAt?: Date;
}

// API Request/Response Types
export interface CreateUserRequest {
  email: string;
  name: string;
  password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  token: string;
  user: User;
}

export interface ChatRequest {
  message: string;
  conversationId?: string;
}

export interface ChatResponse {
  id: string;
  message: string;
  modelUsed: string;
  cost: number;
  timestamp: Date;
}

export interface MemorySearchRequest {
  query: string;
  limit?: number;
}

export interface MemorySearchResponse {
  results: MemoryItem[];
  totalCount: number;
}

export interface SkillTriggerRequest {
  skillId: string;
  parameters?: Record<string, unknown>;
}

// WebSocket Events
export type WebSocketEvent = 
  | RoutingDecisionEvent
  | MemoryChangeEvent
  | SkillExecutionEvent
  | ChatMessageEvent;

export interface RoutingDecisionEvent {
  type: 'routing_decision';
  data: RoutingDecision;
}

export interface MemoryChangeEvent {
  type: 'memory_change';
  data: MemoryItem;
}

export interface SkillExecutionEvent {
  type: 'skill_execution';
  data: SkillExecution;
}

export interface ChatMessageEvent {
  type: 'chat_message';
  data: ChatMessage;
}

// Error Types
export class AppError extends Error {
  constructor(
    public statusCode: number,
    public message: string,
    public code?: string
  ) {
    super(message);
    Object.setPrototypeOf(this, AppError.prototype);
  }
}

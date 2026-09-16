import { Router, Request, Response, NextFunction } from 'express';
import { v4 as uuidv4 } from 'uuid';
import { chatCompletion } from '../services/smartRouterClient';
import { query } from '../services/database';
import { authRequired } from '../middleware/auth';
import { AppError } from '../types';
import { log } from '../middleware/logging';

export const chatRouter = Router();

interface ChatBody {
  message: string;
  conversationId?: string;
  history?: { role: string; content: string }[];
}

interface StoredMessage {
  id: string;
  role: string;
  content: string;
  modelUsed?: string;
  routing?: Record<string, unknown>;
  tokens?: { prompt: number; completion: number };
  cost?: number;
  timestamp: string;
}

/**
 * POST /api/chat
 *
 * Forwards one turn to smart-ai-router (model="auto", so the router's own
 * classifier picks the model) and returns the reply plus the routing decision
 * that produced it. Saves user message and assistant reply to the conversation
 * history in PostgreSQL. Also emits a `routing_decision` event over Socket.IO so
 * any connected dashboard client (e.g. an Analytics tab open in another
 * window) sees the pick land in real time, not just the tab that sent it.
 */
chatRouter.post('/', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const userId = (req as any).userId;
    const body = req.body as ChatBody;
    if (!body || typeof body.message !== 'string' || !body.message.trim()) {
      throw new AppError(422, '`message` is required', 'INVALID_REQUEST');
    }

    let conversationId = body.conversationId;

    // If no conversationId, create a new conversation
    if (!conversationId) {
      const result = await query(
        'INSERT INTO conversations (user_id, title) VALUES ($1, $2) RETURNING id',
        [userId, null]
      );
      conversationId = result.rows[0].id;
    }

    // Verify ownership of conversation
    const convOwner = await query(
      'SELECT user_id FROM conversations WHERE id = $1',
      [conversationId]
    );
    if (!convOwner.rows.length || convOwner.rows[0].user_id !== userId) {
      throw new AppError(403, 'Conversation not found or access denied', 'FORBIDDEN');
    }

    // Save user message
    const userMsgId = uuidv4();
    await query(
      'INSERT INTO chat_messages (id, conversation_id, user_id, role, content) VALUES ($1, $2, $3, $4, $5)',
      [userMsgId, conversationId, userId, 'user', body.message.trim()]
    );

    const messages = [...(body.history ?? []), { role: 'user', content: body.message }];
    const result = await chatCompletion(messages);

    // Save assistant message
    const assistantMsgId = uuidv4();
    const routingMetadata = {
      why: result.routingWhy,
      domain: result.domain,
      complexity: result.complexity,
      escalated: result.escalated,
      qualified: result.qualified,
    };
    await query(
      `INSERT INTO chat_messages 
       (id, conversation_id, user_id, role, content, model_used, routing_metadata, tokens_in, tokens_out, cost_usd) 
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
      [
        assistantMsgId,
        conversationId,
        userId,
        'assistant',
        result.content,
        result.modelUsed,
        JSON.stringify(routingMetadata),
        result.promptTokens || 0,
        result.completionTokens || 0,
        result.costUsd || 0,
      ]
    );

    // Also track in cost_tracking for analytics
    if (result.costUsd) {
      await query(
        `INSERT INTO cost_tracking 
         (user_id, chat_message_id, model, domain, complexity, tokens_prompt, tokens_completion, cost_usd) 
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
        [
          userId,
          assistantMsgId,
          result.modelUsed,
          result.domain,
          result.complexity,
          result.promptTokens || 0,
          result.completionTokens || 0,
          result.costUsd,
        ]
      );
    }

    // Update conversation's updated_at
    await query('UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = $1', [
      conversationId,
    ]);

    const responsePayload = {
      conversationId,
      messageId: assistantMsgId,
      message: result.content,
      modelUsed: result.modelUsed,
      cost: result.costUsd,
      promptTokens: result.promptTokens,
      completionTokens: result.completionTokens,
      routing: routingMetadata,
      timestamp: new Date().toISOString(),
    };

    const io = (req.app as any).io;
    if (io) {
      io.emit('routing_decision', {
        type: 'routing_decision',
        data: {
          id: assistantMsgId,
          modelSelected: result.modelUsed,
          cost: result.costUsd ?? 0,
          reasoning: result.routingWhy,
          domain: result.domain,
          complexity: result.complexity,
          escalated: result.escalated,
          qualified: result.qualified,
          timestamp: responsePayload.timestamp,
        },
      });
    }

    res.json(responsePayload);
  } catch (err) {
    log('error', 'Chat request failed', { error: err });
    next(err);
  }
});

/**
 * GET /api/chat/conversations
 *
 * List all conversations for the authenticated user.
 */
chatRouter.get('/conversations', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const userId = (req as any).userId;
    const limit = parseInt((req.query.limit as string) || '20', 10);
    const offset = parseInt((req.query.offset as string) || '0', 10);

    const result = await query(
      `SELECT id, title, created_at, updated_at, archived 
       FROM conversations 
       WHERE user_id = $1 AND archived = false 
       ORDER BY updated_at DESC 
       LIMIT $2 OFFSET $3`,
      [userId, limit, offset]
    );

    const countResult = await query(
      'SELECT COUNT(*) as count FROM conversations WHERE user_id = $1 AND archived = false',
      [userId]
    );

    res.json({
      conversations: result.rows,
      total: parseInt(countResult.rows[0].count, 10),
      limit,
      offset,
    });
  } catch (err) {
    log('error', 'Failed to list conversations', { error: err });
    next(err);
  }
});

/**
 * GET /api/chat/conversations/:conversationId
 *
 * Fetch all messages in a conversation.
 */
chatRouter.get('/conversations/:conversationId', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const userId = (req as any).userId;
    const { conversationId } = req.params;

    // Verify ownership
    const convOwner = await query(
      'SELECT user_id FROM conversations WHERE id = $1',
      [conversationId]
    );
    if (!convOwner.rows.length || convOwner.rows[0].user_id !== userId) {
      throw new AppError(403, 'Conversation not found or access denied', 'FORBIDDEN');
    }

    const result = await query(
      `SELECT id, role, content, model_used, routing_metadata, tokens_in, tokens_out, cost_usd, created_at 
       FROM chat_messages 
       WHERE conversation_id = $1 
       ORDER BY created_at ASC`,
      [conversationId]
    );

    const messages: StoredMessage[] = result.rows.map((row: any) => ({
      id: row.id,
      role: row.role,
      content: row.content,
      modelUsed: row.model_used,
      routing: row.routing_metadata,
      tokens:
        row.tokens_in || row.tokens_out
          ? { prompt: row.tokens_in || 0, completion: row.tokens_out || 0 }
          : undefined,
      cost: row.cost_usd ? parseFloat(row.cost_usd) : undefined,
      timestamp: row.created_at,
    }));

    res.json({ conversationId, messages });
  } catch (err) {
    log('error', 'Failed to fetch conversation history', { error: err });
    next(err);
  }
});

/**
 * PATCH /api/chat/conversations/:conversationId
 *
 * Update conversation metadata (title, archived status).
 */
chatRouter.patch('/conversations/:conversationId', authRequired, async (req: Request, res: Response, next: NextFunction) => {
  try {
    const userId = (req as any).userId;
    const { conversationId } = req.params;
    const { title, archived } = req.body;

    // Verify ownership
    const convOwner = await query(
      'SELECT user_id FROM conversations WHERE id = $1',
      [conversationId]
    );
    if (!convOwner.rows.length || convOwner.rows[0].user_id !== userId) {
      throw new AppError(403, 'Conversation not found or access denied', 'FORBIDDEN');
    }

    const updates: string[] = [];
    const values: unknown[] = [conversationId];
    let paramCount = 2;

    if (title !== undefined) {
      updates.push(`title = $${paramCount}`);
      values.push(title);
      paramCount++;
    }
    if (archived !== undefined) {
      updates.push(`archived = $${paramCount}`);
      values.push(archived);
      paramCount++;
    }

    if (updates.length > 0) {
      updates.push(`updated_at = CURRENT_TIMESTAMP`);
      await query(`UPDATE conversations SET ${updates.join(', ')} WHERE id = $1`, values);
    }

    res.json({ success: true });
  } catch (err) {
    log('error', 'Failed to update conversation', { error: err });
    next(err);
  }
});

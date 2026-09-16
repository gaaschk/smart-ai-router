import { Router, Request, Response, NextFunction } from 'express';
import { v4 as uuidv4 } from 'uuid';
import { chatCompletion } from '../services/smartRouterClient';
import { AppError } from '../types';
import { log } from '../middleware/logging';

export const chatRouter = Router();

interface ChatBody {
  message: string;
  history?: { role: string; content: string }[];
}

/**
 * POST /api/chat
 *
 * Forwards one turn to smart-ai-router (model="auto", so the router's own
 * classifier picks the model) and returns the reply plus the routing decision
 * that produced it. Also emits a `routing_decision` event over Socket.IO so
 * any connected dashboard client (e.g. an Analytics tab open in another
 * window) sees the pick land in real time, not just the tab that sent it.
 */
chatRouter.post('/', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const body = req.body as ChatBody;
    if (!body || typeof body.message !== 'string' || !body.message.trim()) {
      throw new AppError(422, '`message` is required', 'INVALID_REQUEST');
    }

    const messages = [...(body.history ?? []), { role: 'user', content: body.message }];
    const result = await chatCompletion(messages);

    const responsePayload = {
      id: uuidv4(),
      message: result.content,
      modelUsed: result.modelUsed,
      cost: result.costUsd,
      promptTokens: result.promptTokens,
      completionTokens: result.completionTokens,
      routing: {
        why: result.routingWhy,
        domain: result.domain,
        complexity: result.complexity,
        escalated: result.escalated,
        qualified: result.qualified,
      },
      timestamp: new Date().toISOString(),
    };

    const io = (req.app as any).io;
    if (io) {
      io.emit('routing_decision', {
        type: 'routing_decision',
        data: {
          id: responsePayload.id,
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

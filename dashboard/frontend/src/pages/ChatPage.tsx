import { useState, useEffect } from 'react';
import { Send, Zap, AlertTriangle, Plus, MessageCircle, Trash2 } from 'lucide-react';
import {
  sendChatMessage,
  fetchConversations,
  fetchConversationHistory,
  updateConversation,
  ChatRoutingInfo,
  Conversation,
} from '../lib/api';

interface DisplayMessage {
  id?: string;
  role: 'user' | 'assistant';
  content: string;
  modelUsed?: string;
  cost?: number | null;
  routing?: ChatRoutingInfo;
  timestamp?: string;
}

export function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showSidebar, setShowSidebar] = useState(true);
  const [loadingHistory, setLoadingHistory] = useState(false);

  // Fetch past conversations on mount
  useEffect(() => {
    const loadConversations = async () => {
      try {
        const result = await fetchConversations(10);
        setConversations(result.conversations);
      } catch (err) {
        // Silently fail for now (user may not be authenticated yet)
      }
    };
    loadConversations();
  }, []);

  // Fetch conversation history when switching conversations
  useEffect(() => {
    if (!currentConversationId) return;

    const loadHistory = async () => {
      setLoadingHistory(true);
      try {
        const result = await fetchConversationHistory(currentConversationId);
        setMessages(result.messages as DisplayMessage[]);
      } catch (err: any) {
        setError(
          err?.response?.data?.error || err?.message || 'Failed to load conversation history'
        );
      } finally {
        setLoadingHistory(false);
      }
    };

    loadHistory();
  }, [currentConversationId]);

  const startNewConversation = () => {
    setCurrentConversationId(null);
    setMessages([]);
    setInput('');
    setError('');
  };

  const deleteConversation = async (convId: string) => {
    try {
      await updateConversation(convId, { archived: true });
      setConversations((prev) => prev.filter((c) => c.id !== convId));
      if (currentConversationId === convId) {
        startNewConversation();
      }
    } catch (err) {
      setError('Failed to delete conversation');
    }
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || loading) return;

    setError('');
    const history = messages.map((m) => ({ role: m.role, content: m.content }));
    const userMsg: DisplayMessage = { role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setLoading(true);

    try {
      const result = await sendChatMessage(text, currentConversationId || undefined, history);

      // If this was the first message (no conversationId), update to use the returned one
      if (!currentConversationId) {
        setCurrentConversationId(result.conversationId);
        // Refresh conversations list
        const convs = await fetchConversations(10);
        setConversations(convs.conversations);
      }

      setMessages((prev) => [
        ...prev,
        {
          id: result.messageId,
          role: 'assistant',
          content: result.message,
          modelUsed: result.modelUsed,
          cost: result.cost,
          routing: result.routing,
          timestamp: result.timestamp,
        },
      ]);
    } catch (err: any) {
      const detail =
        err?.response?.data?.error || err?.message || 'Failed to reach the dashboard backend';
      setError(detail);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="h-full flex bg-white">
      {/* Sidebar: Conversation List */}
      {showSidebar && (
        <div className="w-64 border-r border-gray-200 flex flex-col bg-gray-50">
          {/* New Conversation Button */}
          <button
            onClick={startNewConversation}
            className="m-3 p-3 bg-blue-500 text-white rounded-lg hover:bg-blue-600 flex items-center justify-center gap-2 font-medium"
          >
            <Plus className="w-4 h-4" />
            New Chat
          </button>

          {/* Conversations List */}
          <div className="flex-1 overflow-y-auto">
            {conversations.length === 0 ? (
              <div className="p-3 text-center text-gray-500 text-sm">
                No conversations yet
              </div>
            ) : (
              <div className="space-y-1 p-3">
                {conversations.map((conv) => (
                  <div
                    key={conv.id}
                    className={`group p-3 rounded-lg cursor-pointer transition ${
                      currentConversationId === conv.id
                        ? 'bg-blue-100 text-blue-900'
                        : 'hover:bg-gray-200'
                    }`}
                    onClick={() => setCurrentConversationId(conv.id)}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <MessageCircle className="w-4 h-4 flex-shrink-0" />
                          <p className="font-medium truncate text-sm">
                            {conv.title || 'Untitled'}
                          </p>
                        </div>
                        <p className="text-xs text-gray-500 mt-1">
                          {new Date(conv.updated_at).toLocaleDateString()}
                        </p>
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          deleteConversation(conv.id);
                        }}
                        className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-100 text-red-600 rounded transition"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col">
        {/* Header */}
        <div className="border-b border-gray-200 p-6">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-2xl font-bold text-gray-800">Chat with AI</h2>
              <p className="text-sm text-gray-500 mt-1">
                {currentConversationId
                  ? 'Continue your conversation'
                  : 'Messages are routed through smart-ai-router for cost optimization'}
              </p>
            </div>
            <button
              onClick={() => setShowSidebar(!showSidebar)}
              className="px-3 py-2 text-gray-500 hover:bg-gray-100 rounded"
            >
              ☰
            </button>
          </div>
        </div>

        {/* Messages Area */}
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          {loadingHistory ? (
            <div className="flex items-center justify-center h-full">
              <p className="text-gray-500">Loading conversation...</p>
            </div>
          ) : messages.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <p className="text-gray-500 text-lg">No messages yet</p>
                <p className="text-gray-400 text-sm mt-2">
                  Start a conversation by typing below
                </p>
              </div>
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div
                key={msg.id || idx}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-md px-4 py-2 rounded-lg ${
                    msg.role === 'user'
                      ? 'bg-blue-500 text-white'
                      : 'bg-gray-200 text-gray-800'
                  }`}
                >
                  <p className="whitespace-pre-wrap">{msg.content}</p>
                  {msg.role === 'assistant' && msg.modelUsed && (
                    <div className="mt-2 pt-2 border-t border-gray-300 flex items-center gap-2 text-xs text-gray-600">
                      <Zap className="w-3 h-3" />
                      <span className="font-medium">{msg.modelUsed}</span>
                      {typeof msg.cost === 'number' && (
                        <span>· ${msg.cost.toFixed(5)}</span>
                      )}
                      {msg.routing?.escalated && (
                        <span className="flex items-center gap-1 text-amber-600">
                          <AlertTriangle className="w-3 h-3" /> escalated
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          {loading && (
            <div className="flex justify-start">
              <div className="max-w-md px-4 py-2 rounded-lg bg-gray-100 text-gray-500 text-sm">
                Routing and generating…
              </div>
            </div>
          )}
        </div>

        {error && (
          <div className="px-6 py-2 bg-red-50 text-red-700 text-sm border-t border-red-100">
            {error}
          </div>
        )}

        {/* Input Area */}
        <div className="border-t border-gray-200 p-6">
          <div className="flex gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSend()}
              placeholder="Type your message..."
              className="flex-1 px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              disabled={loading || loadingHistory}
            />
            <button
              onClick={handleSend}
              disabled={loading || loadingHistory || !input.trim()}
              className="px-6 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:bg-gray-400 flex items-center gap-2"
            >
              <Send className="w-4 h-4" />
              {loading ? 'Sending...' : 'Send'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

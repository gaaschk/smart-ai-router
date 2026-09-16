import { useState } from 'react';
import { Send, Zap, AlertTriangle } from 'lucide-react';
import { sendChatMessage, ChatRoutingInfo } from '../lib/api';

interface DisplayMessage {
  role: 'user' | 'assistant';
  content: string;
  modelUsed?: string;
  cost?: number | null;
  routing?: ChatRoutingInfo;
}

export function ChatPage() {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

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
      const result = await sendChatMessage(text, history);
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: result.message,
          modelUsed: result.modelUsed,
          cost: result.cost,
          routing: result.routing,
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
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="border-b border-gray-200 p-6">
        <h2 className="text-2xl font-bold text-gray-800">Chat with AI</h2>
        <p className="text-sm text-gray-500 mt-1">
          Messages are routed through smart-ai-router for cost optimization
        </p>
      </div>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {messages.length === 0 ? (
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
              key={idx}
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
            disabled={loading}
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            className="px-6 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:bg-gray-400 flex items-center gap-2"
          >
            <Send className="w-4 h-4" />
            {loading ? 'Sending...' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}

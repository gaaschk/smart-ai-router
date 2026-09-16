import { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';
import { fetchUsageSummary, UsageSummary } from '../lib/api';

export function AnalyticsPage() {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchUsageSummary(30)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err?.response?.data?.error || err?.message || 'Failed to load usage data'
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const totals = summary?.totals;
  const avgCost =
    totals && totals.requests > 0 ? totals.cost_usd / totals.requests : 0;

  const chartData = (summary?.by_day ?? []).map((row) => ({
    date: row.key,
    cost: Number(row.cost_usd.toFixed(4)),
  }));

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-6">Cost Analytics</h2>

      {error && (
        <div className="mb-4 px-4 py-3 bg-red-50 text-red-700 text-sm rounded-lg">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-white p-6 rounded-lg shadow">
          <p className="text-gray-500 text-sm">Total Cost (30d)</p>
          <p className="text-3xl font-bold text-gray-800">
            ${(totals?.cost_usd ?? 0).toFixed(2)}
          </p>
        </div>
        <div className="bg-white p-6 rounded-lg shadow">
          <p className="text-gray-500 text-sm">Avg Cost/Request</p>
          <p className="text-3xl font-bold text-gray-800">${avgCost.toFixed(4)}</p>
        </div>
        <div className="bg-white p-6 rounded-lg shadow">
          <p className="text-gray-500 text-sm">Total Requests</p>
          <p className="text-3xl font-bold text-gray-800">{totals?.requests ?? 0}</p>
        </div>
        <div className="bg-white p-6 rounded-lg shadow">
          <p className="text-gray-500 text-sm">Total Tokens</p>
          <p className="text-3xl font-bold text-gray-800">
            {((totals?.prompt_tokens ?? 0) + (totals?.completion_tokens ?? 0)).toLocaleString()}
          </p>
        </div>
      </div>

      <div className="bg-white p-6 rounded-lg shadow mb-6">
        <h3 className="text-lg font-semibold text-gray-800 mb-4">Cost Trends (30 days)</h3>
        {loading ? (
          <div className="h-64 flex items-center justify-center bg-gray-50 rounded">
            <p className="text-gray-400">Loading…</p>
          </div>
        ) : chartData.length === 0 ? (
          <div className="h-64 flex items-center justify-center bg-gray-50 rounded">
            <p className="text-gray-400">No usage yet — send a chat message to see data here</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={256}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 12 }} />
              <YAxis tick={{ fontSize: 12 }} />
              <Tooltip formatter={(value: number) => `$${value.toFixed(4)}`} />
              <Line type="monotone" dataKey="cost" stroke="#3b82f6" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white p-6 rounded-lg shadow">
          <h3 className="text-lg font-semibold text-gray-800 mb-4">By Model</h3>
          {(summary?.by_model ?? []).length === 0 ? (
            <p className="text-gray-400 text-sm">No routed requests yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-200">
                  <th className="py-2">Model</th>
                  <th className="py-2">Requests</th>
                  <th className="py-2">Cost</th>
                </tr>
              </thead>
              <tbody>
                {(summary?.by_model ?? []).map((row) => (
                  <tr key={row.key} className="border-b border-gray-100">
                    <td className="py-2 text-gray-800">{row.key}</td>
                    <td className="py-2 text-gray-600">{row.requests}</td>
                    <td className="py-2 text-gray-600">${row.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="bg-white p-6 rounded-lg shadow">
          <h3 className="text-lg font-semibold text-gray-800 mb-4">By Domain</h3>
          {(summary?.by_domain ?? []).length === 0 ? (
            <p className="text-gray-400 text-sm">No requests yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-200">
                  <th className="py-2">Domain</th>
                  <th className="py-2">Requests</th>
                  <th className="py-2">Cost</th>
                </tr>
              </thead>
              <tbody>
                {(summary?.by_domain ?? []).map((row) => (
                  <tr key={row.key} className="border-b border-gray-100">
                    <td className="py-2 text-gray-800">{row.key}</td>
                    <td className="py-2 text-gray-600">{row.requests}</td>
                    <td className="py-2 text-gray-600">${row.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

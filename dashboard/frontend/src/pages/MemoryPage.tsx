import { useEffect, useState } from 'react';
import { Search, Brain, Link2, Tag as TagIcon } from 'lucide-react';
import {
  searchMemory,
  fetchMemoryStats,
  fetchMemoryPage,
  MemorySearchResult,
  GBrainStats,
  GBrainHealth,
} from '../lib/api';

export function MemoryPage() {
  const [searchQuery, setSearchQuery] = useState('');
  const [results, setResults] = useState<MemorySearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [stats, setStats] = useState<GBrainStats | null>(null);
  const [health, setHealth] = useState<GBrainHealth | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<{
    tags: string[];
    links: unknown[];
    backlinks: unknown[];
  } | null>(null);

  useEffect(() => {
    fetchMemoryStats()
      .then(({ stats, health }) => {
        setStats(stats);
        setHealth(health);
      })
      .catch(() => {
        // Brain stats are a nice-to-have header; a failure here shouldn't
        // block search from working.
      });
  }, []);

  useEffect(() => {
    const query = searchQuery.trim();
    if (!query) {
      setResults([]);
      setSearchError(null);
      return;
    }
    const handle = setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        const found = await searchMemory(query, 'hybrid', 10);
        setResults(found);
      } catch (err: any) {
        setSearchError(err?.response?.data?.error || 'Search failed');
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 300); // debounce so every keystroke doesn't shell out to gbrain
    return () => clearTimeout(handle);
  }, [searchQuery]);

  const openPage = async (slug: string) => {
    setSelected(slug);
    setSelectedDetail(null);
    try {
      const detail = await fetchMemoryPage(slug);
      setSelectedDetail({
        tags: detail.tags as string[],
        links: detail.links as unknown[],
        backlinks: detail.backlinks as unknown[],
      });
    } catch {
      setSelectedDetail({ tags: [], links: [], backlinks: [] });
    }
  };

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-2">Knowledge Memory</h2>
      <p className="text-gray-500 text-sm mb-6">Powered by GBrain — hybrid vector + keyword search</p>

      {/* Brain stats */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <StatCard label="Pages" value={stats.page_count} />
          <StatCard label="Chunks" value={stats.chunk_count} />
          <StatCard label="Links" value={stats.link_count} />
          <StatCard
            label="Brain Score"
            value={health ? `${health.brain_score}/100` : '—'}
          />
        </div>
      )}

      {/* Search Bar */}
      <div className="mb-6">
        <div className="relative">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search your knowledge base..."
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <Search className="absolute right-3 top-3 w-5 h-5 text-gray-400" />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Results */}
        <div className="lg:col-span-2">
          {searchError ? (
            <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-lg text-sm">
              {searchError}
            </div>
          ) : results.length === 0 ? (
            <div className="bg-white p-12 rounded-lg shadow text-center">
              <Brain className="w-10 h-10 text-gray-300 mx-auto mb-3" />
              <p className="text-gray-500">
                {searching
                  ? 'Searching...'
                  : searchQuery
                  ? 'No results found'
                  : 'Start by searching your knowledge base'}
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {results.map((result, idx) => (
                <button
                  key={`${result.slug}-${idx}`}
                  onClick={() => openPage(result.slug)}
                  className={`w-full text-left bg-white p-4 rounded-lg shadow hover:shadow-md transition-shadow ${
                    selected === result.slug ? 'ring-2 ring-blue-500' : ''
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <h3 className="font-semibold text-gray-800">{result.title}</h3>
                    <span className="text-xs text-gray-400 font-mono">
                      {result.score.toFixed(2)}
                    </span>
                  </div>
                  <p className="text-gray-400 text-xs mt-1 font-mono">{result.slug}</p>
                  <p className="text-gray-600 text-sm mt-2 line-clamp-3">
                    {result.chunk_text.slice(0, 240)}
                    {result.chunk_text.length > 240 ? '…' : ''}
                  </p>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Selected page detail */}
        <div>
          {selected && (
            <div className="bg-white p-4 rounded-lg shadow sticky top-6">
              <h3 className="font-semibold text-gray-800 mb-1">{selected}</h3>
              {selectedDetail ? (
                <>
                  <div className="flex items-center gap-1 text-xs text-gray-500 mt-3 mb-1">
                    <TagIcon className="w-3.5 h-3.5" /> Tags
                  </div>
                  {selectedDetail.tags.length ? (
                    <div className="flex flex-wrap gap-1">
                      {selectedDetail.tags.map((t) => (
                        <span
                          key={t}
                          className="text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-gray-400">None</p>
                  )}

                  <div className="flex items-center gap-1 text-xs text-gray-500 mt-3 mb-1">
                    <Link2 className="w-3.5 h-3.5" /> Links / Backlinks
                  </div>
                  <p className="text-xs text-gray-400">
                    {selectedDetail.links.length} outgoing · {selectedDetail.backlinks.length}{' '}
                    incoming
                  </p>
                </>
              ) : (
                <p className="text-xs text-gray-400">Loading…</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-white p-4 rounded-lg shadow">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-xl font-bold text-gray-800 mt-1">{value}</p>
    </div>
  );
}

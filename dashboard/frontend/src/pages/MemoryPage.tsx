import { useState } from 'react';
import { Search } from 'lucide-react';

export function MemoryPage() {
  const [searchQuery, setSearchQuery] = useState('');
  const [results] = useState([]);

  const handleSearch = (query: string) => {
    setSearchQuery(query);
    // TODO: Call backend to search memory
    console.log('Searching memory for:', query);
  };

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-6">Knowledge Memory</h2>

      {/* Search Bar */}
      <div className="mb-6">
        <div className="relative">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder="Search your knowledge base..."
            className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <Search className="absolute right-3 top-3 w-5 h-5 text-gray-400" />
        </div>
      </div>

      {/* Results */}
      {results.length === 0 ? (
        <div className="bg-white p-12 rounded-lg shadow text-center">
          <p className="text-gray-500">
            {searchQuery ? 'No results found' : 'Start by searching your knowledge base'}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {results.map((result: any, idx) => (
            <div key={idx} className="bg-white p-4 rounded-lg shadow">
              <h3 className="font-semibold text-gray-800">{result.title}</h3>
              <p className="text-gray-600 text-sm mt-2">{result.preview}</p>
              <p className="text-gray-400 text-xs mt-2">
                Created: {new Date(result.createdAt).toLocaleDateString()}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

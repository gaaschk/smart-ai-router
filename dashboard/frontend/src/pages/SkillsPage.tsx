import { useEffect, useState } from 'react';
import { Play, Plug, ListChecks } from 'lucide-react';
import {
  fetchIntegrations,
  fetchJobCatalog,
  listJobs,
  submitJob,
  GBrainIntegration,
  JobCatalogEntry,
  GBrainJob,
} from '../lib/api';

export function SkillsPage() {
  const [integrations, setIntegrations] = useState<GBrainIntegration[]>([]);
  const [jobCatalog, setJobCatalog] = useState<JobCatalogEntry[]>([]);
  const [recentJobs, setRecentJobs] = useState<GBrainJob[]>([]);
  const [loadingJob, setLoadingJob] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAll = async () => {
    try {
      const [ints, catalog, jobs] = await Promise.all([
        fetchIntegrations(),
        fetchJobCatalog(),
        listJobs(10),
      ]);
      setIntegrations([...ints.infra, ...ints.senses, ...ints.reflexes]);
      setJobCatalog(catalog);
      setRecentJobs(jobs);
    } catch (err: any) {
      setError(err?.response?.data?.error || 'Failed to load GBrain skills');
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  const handleRunJob = async (jobId: string) => {
    setLoadingJob(jobId);
    try {
      await submitJob(jobId);
      const jobs = await listJobs(10);
      setRecentJobs(jobs);
    } catch (err: any) {
      setError(err?.response?.data?.error || `Failed to run ${jobId}`);
    } finally {
      setLoadingJob(null);
    }
  };

  if (error) {
    return (
      <div className="p-6">
        <h2 className="text-2xl font-bold text-gray-800 mb-6">GBrain Skills</h2>
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-lg text-sm">
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-6">GBrain Skills</h2>

      {/* Background jobs (Minions) */}
      <section className="mb-8">
        <h3 className="flex items-center gap-2 text-lg font-semibold text-gray-700 mb-3">
          <ListChecks className="w-5 h-5" /> Maintenance Jobs
        </h3>
        {jobCatalog.length === 0 ? (
          <p className="text-gray-400 text-sm">Loading jobs...</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {jobCatalog.map((job) => (
              <div key={job.id} className="bg-white p-4 rounded-lg shadow">
                <h4 className="font-semibold text-gray-800">{job.name}</h4>
                <p className="text-gray-600 text-sm mt-2">{job.description}</p>
                <button
                  onClick={() => handleRunJob(job.id)}
                  disabled={loadingJob === job.id}
                  className="mt-4 w-full px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:bg-gray-400 flex items-center justify-center gap-2"
                >
                  <Play className="w-4 h-4" />
                  {loadingJob === job.id ? 'Submitting...' : 'Run'}
                </button>
              </div>
            ))}
          </div>
        )}

        {recentJobs.length > 0 && (
          <div className="mt-4 bg-white rounded-lg shadow overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  <th className="text-left px-4 py-2">ID</th>
                  <th className="text-left px-4 py-2">Name</th>
                  <th className="text-left px-4 py-2">Status</th>
                  <th className="text-left px-4 py-2">Created</th>
                </tr>
              </thead>
              <tbody>
                {recentJobs.map((job) => (
                  <tr key={job.id} className="border-t border-gray-100">
                    <td className="px-4 py-2 text-gray-500">{job.id}</td>
                    <td className="px-4 py-2 font-medium text-gray-800">{job.name}</td>
                    <td className="px-4 py-2">
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="px-4 py-2 text-gray-400">
                      {new Date(job.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Integration recipes */}
      <section>
        <h3 className="flex items-center gap-2 text-lg font-semibold text-gray-700 mb-3">
          <Plug className="w-5 h-5" /> Integrations
        </h3>
        {integrations.length === 0 ? (
          <div className="bg-white p-12 rounded-lg shadow text-center">
            <p className="text-gray-500 text-lg">Loading integrations...</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {integrations.map((integration) => (
              <div key={integration.id} className="bg-white p-4 rounded-lg shadow">
                <div className="flex items-center justify-between">
                  <h4 className="font-semibold text-gray-800">{integration.name}</h4>
                  <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded">
                    {integration.category}
                  </span>
                </div>
                <p className="text-gray-600 text-sm mt-2">{integration.description}</p>
                <p className="text-gray-400 text-xs mt-2">
                  Setup: {integration.setup_time} · {integration.status}
                </p>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    completed: 'bg-green-100 text-green-700',
    waiting: 'bg-gray-100 text-gray-600',
    active: 'bg-blue-100 text-blue-700',
    failed: 'bg-red-100 text-red-700',
    dead: 'bg-red-100 text-red-700',
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${colors[status] || 'bg-gray-100 text-gray-600'}`}>
      {status}
    </span>
  );
}

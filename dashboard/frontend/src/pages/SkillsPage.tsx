import React, { useState } from 'react';
import { Play } from 'lucide-react';

export function SkillsPage() {
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(false);

  const handleTriggerSkill = async (skillId: string) => {
    // TODO: Call backend to trigger skill
    console.log('Triggering skill:', skillId);
    setLoading(true);
    setTimeout(() => setLoading(false), 1000);
  };

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-6">GBrain Skills</h2>

      {skills.length === 0 ? (
        <div className="bg-white p-12 rounded-lg shadow text-center">
          <p className="text-gray-500 text-lg">Loading skills...</p>
          <p className="text-gray-400 text-sm mt-2">
            Connect to GBrain to see available skills
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {skills.map((skill: any) => (
            <div key={skill.id} className="bg-white p-4 rounded-lg shadow">
              <h3 className="font-semibold text-gray-800">{skill.name}</h3>
              <p className="text-gray-600 text-sm mt-2">{skill.description}</p>
              <button
                onClick={() => handleTriggerSkill(skill.id)}
                disabled={loading}
                className="mt-4 w-full px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:bg-gray-400 flex items-center justify-center gap-2"
              >
                <Play className="w-4 h-4" />
                {loading ? 'Running...' : 'Run'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

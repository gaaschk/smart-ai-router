import { useState } from 'react';
import { Save } from 'lucide-react';

export function SettingsPage() {
  const [settings, setSettings] = useState({
    smartRouterUrl: 'http://localhost:8001',
    gbrainUrl: 'http://localhost:8002',
    theme: 'light',
  });

  const handleSave = async () => {
    // TODO: Save settings to backend
    console.log('Saving settings:', settings);
  };

  return (
    <div className="p-6">
      <h2 className="text-2xl font-bold text-gray-800 mb-6">Settings</h2>

      <div className="bg-white rounded-lg shadow max-w-2xl">
        <div className="p-6 space-y-6">
          {/* Smart Router Connection */}
          <div>
            <label className="block text-sm font-semibold text-gray-800 mb-2">
              Smart Router URL
            </label>
            <input
              type="text"
              value={settings.smartRouterUrl}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  smartRouterUrl: e.target.value,
                })
              }
              className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-sm text-gray-500 mt-1">
              URL where smart-ai-router is running
            </p>
          </div>

          {/* GBrain Connection */}
          <div>
            <label className="block text-sm font-semibold text-gray-800 mb-2">
              GBrain URL
            </label>
            <input
              type="text"
              value={settings.gbrainUrl}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  gbrainUrl: e.target.value,
                })
              }
              className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-sm text-gray-500 mt-1">
              URL where GBrain is running
            </p>
          </div>

          {/* Theme */}
          <div>
            <label className="block text-sm font-semibold text-gray-800 mb-2">
              Theme
            </label>
            <select
              value={settings.theme}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  theme: e.target.value,
                })
              }
              className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="light">Light</option>
              <option value="dark">Dark</option>
              <option value="auto">Auto</option>
            </select>
          </div>
        </div>

        {/* Save Button */}
        <div className="border-t border-gray-200 p-6 flex justify-end">
          <button
            onClick={handleSave}
            className="px-6 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 flex items-center gap-2"
          >
            <Save className="w-4 h-4" />
            Save Settings
          </button>
        </div>
      </div>
    </div>
  );
}

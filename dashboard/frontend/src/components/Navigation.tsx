import React from 'react';
import {
  MessageCircle,
  BarChart3,
  Brain,
  Zap,
  Users,
  Settings,
  Wifi,
  WifiOff,
} from 'lucide-react';

interface NavigationProps {
  currentPage: string;
  onPageChange: (page: any) => void;
  isConnected: boolean;
}

export function Navigation({ currentPage, onPageChange, isConnected }: NavigationProps) {
  const navItems = [
    { id: 'chat', label: 'Chat', icon: MessageCircle },
    { id: 'analytics', label: 'Analytics', icon: BarChart3 },
    { id: 'memory', label: 'Memory', icon: Brain },
    { id: 'skills', label: 'Skills', icon: Zap },
    { id: 'users', label: 'Users', icon: Users },
    { id: 'settings', label: 'Settings', icon: Settings },
  ];

  return (
    <nav className="w-64 bg-white shadow-md flex flex-col">
      {/* Header */}
      <div className="p-6 border-b border-gray-200">
        <h1 className="text-2xl font-bold text-gray-800">Dashboard</h1>
        <p className="text-sm text-gray-500 mt-1">Smart Router & GBrain</p>
      </div>

      {/* Connection Status */}
      <div className="px-6 py-3 border-b border-gray-200">
        <div className="flex items-center gap-2">
          {isConnected ? (
            <>
              <Wifi className="w-4 h-4 text-green-500" />
              <span className="text-sm text-green-600">Connected</span>
            </>
          ) : (
            <>
              <WifiOff className="w-4 h-4 text-red-500" />
              <span className="text-sm text-red-600">Disconnected</span>
            </>
          )}
        </div>
      </div>

      {/* Navigation Items */}
      <ul className="flex-1 py-4">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentPage === item.id;

          return (
            <li key={item.id}>
              <button
                onClick={() => onPageChange(item.id)}
                className={`w-full flex items-center gap-3 px-6 py-3 transition-colors ${
                  isActive
                    ? 'bg-blue-50 text-blue-600 border-l-4 border-blue-600'
                    : 'text-gray-700 hover:bg-gray-50'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="font-medium">{item.label}</span>
              </button>
            </li>
          );
        })}
      </ul>

      {/* Footer */}
      <div className="p-4 border-t border-gray-200 text-center">
        <p className="text-xs text-gray-500">v0.1.0</p>
      </div>
    </nav>
  );
}

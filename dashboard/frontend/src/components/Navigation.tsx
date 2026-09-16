import { useNavigate, useLocation } from 'react-router-dom';
import {
  MessageCircle,
  BarChart3,
  Brain,
  Zap,
  Users,
  Settings,
  Wifi,
  WifiOff,
  LogOut,
} from 'lucide-react';
import { User } from '../hooks/useAuth';

interface NavigationProps {
  isConnected: boolean;
  user: User | null;
  onLogout: () => void;
}

export function Navigation({ isConnected, user, onLogout }: NavigationProps) {
  const navigate = useNavigate();
  const location = useLocation();

  const navItems = [
    { path: '/', label: 'Chat', icon: MessageCircle },
    { path: '/analytics', label: 'Analytics', icon: BarChart3 },
    { path: '/memory', label: 'Memory', icon: Brain },
    { path: '/skills', label: 'Skills', icon: Zap },
    ...(user?.role === 'admin'
      ? [{ path: '/users', label: 'Users', icon: Users }]
      : []),
    { path: '/settings', label: 'Settings', icon: Settings },
  ];

  const handleLogout = () => {
    onLogout();
    navigate('/login');
  };

  return (
    <nav className="w-64 bg-white shadow-md flex flex-col">
      {/* Header */}
      <div className="p-6 border-b border-gray-200">
        <h1 className="text-2xl font-bold text-gray-800">Dashboard</h1>
        <p className="text-sm text-gray-500 mt-1">Smart Router & GBrain</p>
      </div>

      {/* User Info */}
      <div className="px-6 py-4 border-b border-gray-200">
        <p className="text-sm font-medium text-gray-800">{user?.name}</p>
        <p className="text-xs text-gray-500">{user?.email}</p>
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
          const isActive = location.pathname === item.path;

          return (
            <li key={item.path}>
              <button
                onClick={() => navigate(item.path)}
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

      {/* Logout Button */}
      <div className="p-4 border-t border-gray-200">
        <button
          onClick={handleLogout}
          className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-red-50 text-red-600 rounded-lg hover:bg-red-100 transition font-medium text-sm"
        >
          <LogOut className="w-4 h-4" />
          Logout
        </button>
      </div>

      {/* Footer */}
      <div className="p-4 border-t border-gray-200 text-center">
        <p className="text-xs text-gray-500">v0.1.0</p>
      </div>
    </nav>
  );
}

import { useState, useEffect } from 'react';
import { Navigation } from './components/Navigation';
import { ChatPage } from './pages/ChatPage';
import { AnalyticsPage } from './pages/AnalyticsPage';
import { MemoryPage } from './pages/MemoryPage';
import { SkillsPage } from './pages/SkillsPage';
import { UsersPage } from './pages/UsersPage';
import { SettingsPage } from './pages/SettingsPage';
import { getSocket } from './lib/socket';

type PageType = 'chat' | 'analytics' | 'memory' | 'skills' | 'users' | 'settings';

export function App() {
  const [currentPage, setCurrentPage] = useState<PageType>('chat');
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    const socket = getSocket();
    const onConnect = () => setIsConnected(true);
    const onDisconnect = () => setIsConnected(false);

    socket.on('connect', onConnect);
    socket.on('disconnect', onDisconnect);
    if (socket.connected) setIsConnected(true);

    return () => {
      socket.off('connect', onConnect);
      socket.off('disconnect', onDisconnect);
    };
  }, []);

  const renderPage = () => {
    switch (currentPage) {
      case 'chat':
        return <ChatPage />;
      case 'analytics':
        return <AnalyticsPage />;
      case 'memory':
        return <MemoryPage />;
      case 'skills':
        return <SkillsPage />;
      case 'users':
        return <UsersPage />;
      case 'settings':
        return <SettingsPage />;
      default:
        return <ChatPage />;
    }
  };

  return (
    <div className="flex h-screen bg-gray-100">
      <Navigation 
        currentPage={currentPage} 
        onPageChange={setCurrentPage}
        isConnected={isConnected}
      />
      <main className="flex-1 overflow-auto">
        {renderPage()}
      </main>
    </div>
  );
}

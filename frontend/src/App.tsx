import React, { useState, useCallback } from 'react';
import { Sidebar } from './components/Sidebar';
import { CounterBar } from './components/CounterBar';
import { AlertsSection } from './components/AlertsSection';
import { AlertMap } from './components/AlertMap';
import { AlertDetailPane } from './components/AlertDetailPane';
import { useWebSocket } from './hooks/useWebSocket';
import type { Alert } from './types/alert';
import './styles/main.css';

// Determine WebSocket URL based on environment
const getWebSocketUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // In development, Vite proxies /ws to the backend
  // In production, we connect directly
  if (import.meta.env.DEV) {
    return `${protocol}//${window.location.host}/ws`;
  }
  return `${protocol}//${window.location.host}/ws`;
};

const App: React.FC = () => {
  const [activeSection, setActiveSection] = useState('alerts');
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [selectedMapAlert, setSelectedMapAlert] = useState<Alert | null>(null);
  const [mapDetailOpen, setMapDetailOpen] = useState(false);

  const handleNewAlert = useCallback((alert: Alert) => {
    console.log('New alert received:', alert.event_name);
    // Could play sound, show notification, etc.
  }, []);

  const handleBulkAlerts = useCallback(() => {
    setLastChecked(new Date());
  }, []);

  const { connected, alerts } = useWebSocket({
    url: getWebSocketUrl(),
    onAlert: handleNewAlert,
    onBulkAlerts: handleBulkAlerts,
  });

  const formatLastChecked = () => {
    if (!lastChecked) return 'Never';
    return lastChecked.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
    });
  };

  const handleMapAlertClick = (alert: Alert) => {
    setSelectedMapAlert(alert);
    setMapDetailOpen(true);
  };

  return (
    <div className="app-layout">
      <Sidebar
        activeSection={activeSection}
        onSectionChange={setActiveSection}
      />

      <div className="main-content">
        <header className="header">
          <div className="header-top">
            <h1 className="header-title">Weather Dashboard</h1>
            <div className="header-status">
              <div className="status-item">
                <span className={`status-dot ${connected ? 'connected' : ''}`}></span>
                <span>{connected ? 'Connected' : 'Disconnected'}</span>
              </div>
              <div className="status-item">
                <i className="fas fa-clock"></i>
                <span>Last Update: {formatLastChecked()}</span>
              </div>
              <div className="status-item">
                <i className="fas fa-exclamation-circle"></i>
                <span>{alerts.length} Active Alerts</span>
              </div>
            </div>
          </div>
          <CounterBar alerts={alerts} />
        </header>

        <div className="content-area">
          {activeSection === 'alerts' && (
            <AlertsSection alerts={alerts} />
          )}

          {activeSection === 'map' && (
            <div className="section active" style={{ height: '100%', position: 'relative' }}>
              <h2 className="section-title">Alert Map</h2>
              <AlertMap
                alerts={alerts}
                onAlertClick={handleMapAlertClick}
                selectedAlert={selectedMapAlert}
              />
              <AlertDetailPane
                alert={selectedMapAlert}
                isOpen={mapDetailOpen}
                onClose={() => setMapDetailOpen(false)}
              />
            </div>
          )}

          {activeSection === 'lsr' && (
            <div className="section active">
              <h2 className="section-title">Local Storm Reports</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Storm reports section coming soon...</p>
            </div>
          )}

          {activeSection === 'spc' && (
            <div className="section active">
              <h2 className="section-title">SPC Outlooks</h2>
              <p style={{ color: 'var(--text-secondary)' }}>SPC outlooks section coming soon...</p>
            </div>
          )}

          {activeSection === 'md' && (
            <div className="section active">
              <h2 className="section-title">Mesoscale Discussions</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Mesoscale discussions section coming soon...</p>
            </div>
          )}

          {activeSection === 'afd' && (
            <div className="section active">
              <h2 className="section-title">Area Forecast Discussions</h2>
              <p style={{ color: 'var(--text-secondary)' }}>AFD section coming soon...</p>
            </div>
          )}

          {activeSection === 'gusts' && (
            <div className="section active">
              <h2 className="section-title">Top Wind Gusts</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Wind gusts section coming soon...</p>
            </div>
          )}

          {activeSection === 'snow-emergency' && (
            <div className="section active">
              <h2 className="section-title">Snow Emergencies</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Snow emergency section coming soon...</p>
            </div>
          )}

          {activeSection === 'nwws-feed' && (
            <div className="section active">
              <h2 className="section-title">NWWS Products</h2>
              <p style={{ color: 'var(--text-secondary)' }}>NWWS feed section coming soon...</p>
            </div>
          )}

          {activeSection === 'daily-recap' && (
            <div className="section active">
              <h2 className="section-title">Daily Recap</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Daily recap section coming soon...</p>
            </div>
          )}

          {activeSection === 'settings' && (
            <div className="section active">
              <h2 className="section-title">Settings</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Settings section coming soon...</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default App;

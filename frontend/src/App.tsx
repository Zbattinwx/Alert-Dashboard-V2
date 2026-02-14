import React, { useState, useCallback } from 'react';
import { Routes, Route } from 'react-router-dom';
import { Sidebar } from './components/Sidebar';
import { CounterBar } from './components/CounterBar';
import { AlertsSection } from './components/AlertsSection';
import { AlertMap } from './components/AlertMap';
import { AlertDetailPane } from './components/AlertDetailPane';
import { StormReportsSection } from './components/StormReportsSection';
import { ODOTSection } from './components/ODOTSection';
import { SPCSection } from './components/SPCSection';
import { WindGustsSection } from './components/WindGustsSection';
import { AssistantPanel } from './components/AssistantPanel';
import { NewAlertNotification } from './components/NewAlertNotification';
import { SettingsSection } from './components/SettingsSection';
import { NWWSProductsSection } from './components/NWWSProductsSection';
import { AFDSection } from './components/AFDSection';
import { OBSOverlay } from './components/OBSOverlay';
import { ChaseMode } from './components/ChaseMode';
import { useWebSocket } from './hooks/useWebSocket';
import type { Alert } from './types/alert';
import type { ChaserPosition } from './types/chaser';
import { apiUrl, wsUrl } from './utils/api';
import './styles/main.css';

// Main Dashboard Component
const Dashboard: React.FC = () => {
  const [activeSection, setActiveSection] = useState('alerts');
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [selectedMapAlert, setSelectedMapAlert] = useState<Alert | null>(null);
  const [mapDetailOpen, setMapDetailOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [newAlertToShow, setNewAlertToShow] = useState<Alert | null>(null);
  const [chasers, setChasers] = useState<ChaserPosition[]>([]);

  const handleNewAlert = useCallback((alert: Alert) => {
    console.log('New alert received:', alert.event_name);
    // Show the new alert notification
    setNewAlertToShow(alert);
  }, []);

  const handleBulkAlerts = useCallback(() => {
    setLastChecked(new Date());
  }, []);

  const handleChaserPosition = useCallback((data: ChaserPosition) => {
    setChasers(prev => {
      const idx = prev.findIndex(c => c.client_id === data.client_id);
      if (idx >= 0) {
        const updated = [...prev];
        updated[idx] = data;
        return updated;
      }
      return [...prev, data];
    });
  }, []);

  const handleChaserDisconnect = useCallback((data: { client_id: string }) => {
    setChasers(prev => prev.filter(c => c.client_id !== data.client_id));
  }, []);

  const { connected, alerts } = useWebSocket({
    url: wsUrl(),
    onAlert: handleNewAlert,
    onBulkAlerts: handleBulkAlerts,
    onChaserPosition: handleChaserPosition,
    onChaserDisconnect: handleChaserDisconnect,
  });

  // Fetch existing chasers on mount
  React.useEffect(() => {
    fetch(apiUrl('/api/chasers'))
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.chasers) setChasers(data.chasers);
      })
      .catch(() => {});
  }, []);

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
    <>
      <div className="app-layout">
        <Sidebar
          activeSection={activeSection}
          onSectionChange={setActiveSection}
        />

        <div className="main-content">
          <header className="header">
            <div className="header-top">
              <h1 className="header-title">The Battin Front</h1>
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
              <div className="section active" style={{ height: '100%' }}>
                <h2 className="section-title">Alert Map</h2>
                <AlertMap
                  alerts={alerts}
                  onAlertClick={handleMapAlertClick}
                  selectedAlert={selectedMapAlert}
                  chasers={chasers}
                />
              </div>
            )}

          {activeSection === 'lsr' && (
            <StormReportsSection />
          )}

          {activeSection === 'odot' && (
            <ODOTSection />
          )}

          {activeSection === 'spc' && (
            <SPCSection />
          )}

          {activeSection === 'md' && (
            <div className="section active">
              <h2 className="section-title">Mesoscale Discussions</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Mesoscale discussions section coming soon...</p>
            </div>
          )}

          {activeSection === 'afd' && (
            <AFDSection />
          )}

          {activeSection === 'gusts' && (
            <WindGustsSection />
          )}

          {activeSection === 'snow-emergency' && (
            <div className="section active">
              <h2 className="section-title">Snow Emergencies</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Snow emergency section coming soon...</p>
            </div>
          )}

          {activeSection === 'nwws-feed' && (
            <NWWSProductsSection />
          )}

          {activeSection === 'daily-recap' && (
            <div className="section active">
              <h2 className="section-title">Daily Recap</h2>
              <p style={{ color: 'var(--text-secondary)' }}>Daily recap section coming soon...</p>
            </div>
          )}

          {activeSection === 'settings' && (
            <SettingsSection />
          )}
        </div>
      </div>
    </div>

      {/* Alert Detail Pane - rendered at root level to avoid z-index stacking context issues */}
      <AlertDetailPane
        alert={selectedMapAlert}
        isOpen={mapDetailOpen}
        onClose={() => setMapDetailOpen(false)}
      />

      {/* AI Assistant Panel */}
      <AssistantPanel
        isOpen={assistantOpen}
        onToggle={() => setAssistantOpen(!assistantOpen)}
      />

      {/* New Alert Notification (slide-in widget) */}
      <NewAlertNotification
        alert={newAlertToShow}
        onDismiss={() => setNewAlertToShow(null)}
      />
    </>
  );
};

// App with routing
const App: React.FC = () => {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/chase" element={<ChaseMode />} />
      <Route path="/obs" element={<OBSOverlay />} />
    </Routes>
  );
};

export default App;

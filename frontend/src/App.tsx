import React, { useState, useCallback, useRef } from 'react';
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
import { MetarSection } from './components/MetarSection';
import { EventStatsSection } from './components/EventStatsSection';
import { AssistantPanel } from './components/AssistantPanel';
import { NewAlertNotification } from './components/NewAlertNotification';
import { NewMDNotification } from './components/NewMDNotification';
import { SettingsSection } from './components/SettingsSection';
import { NWWSProductsSection } from './components/NWWSProductsSection';
import { SocialMediaSection } from './components/SocialMediaSection';
import { ComposeModal } from './components/social/ComposeModal';
import { AFDSection } from './components/AFDSection';
import RadarSection from './components/RadarSection';
import { OBSOverlay } from './components/OBSOverlay';
import { ChaseMode } from './components/ChaseMode';
import { AlertMapGraphic } from './components/AlertMapGraphic';
import { AlertGraphicsSection } from './components/AlertGraphicsSection';
import { useWebSocket } from './hooks/useWebSocket';
import { useAlertChimes } from './hooks/useAlertChimes';
import type { Alert, AgentNotification } from './types/alert';
import type { MesoscaleDiscussion } from './types/spc';
import type { ChaserPosition } from './types/chaser';
import type { RadarFrame, RadarBinaryFrame, RadarStatus, StormCell, LightningFlash, MCSSystem } from './types/radar';
import { apiUrl, wsUrl } from './utils/api';
import './styles/main.css';
import './styles/social.css';
import './styles/radar.css';

interface BrandInfo {
  name: string;
  logo: string;
  website_url: string | null;
}

// Main Dashboard Component
const Dashboard: React.FC = () => {
  const kiosk = new URLSearchParams(window.location.search).get('kiosk');
  const [activeSection, setActiveSection] = useState(kiosk ?? 'alerts');
  const [brand, setBrand] = useState<BrandInfo>({
    name: 'The Battin Front',
    logo: 'tbf_logo.png',
    website_url: 'https://www.thebattinfront.com',
  });
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [selectedMapAlert, setSelectedMapAlert] = useState<Alert | null>(null);
  const [mapDetailOpen, setMapDetailOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [newAlertToShow, setNewAlertToShow] = useState<Alert | null>(null);
  const [newMDToShow, setNewMDToShow] = useState<MesoscaleDiscussion | null>(null);
  const [chasers, setChasers] = useState<ChaserPosition[]>([]);
  const [shareAlert, setShareAlert] = useState<Alert | null>(null);
  const [radarFrame, setRadarFrame] = useState<RadarBinaryFrame | null>(null);
  const [radarFrames, setRadarFrames] = useState<Record<string, RadarBinaryFrame>>({});
  const [radarStatus, setRadarStatus] = useState<RadarStatus | null>(null);
  const [stormCells, setStormCells] = useState<StormCell[]>([]);
  const [mcsSystems, setMcsSystems] = useState<MCSSystem[]>([]);
  const [lightningFlashes, setLightningFlashes] = useState<LightningFlash[]>([]);
  const [agentNotifications, setAgentNotifications] = useState<AgentNotification[]>([]);
  const [focusedCellId, setFocusedCellId] = useState<string | null>(null);

  // Graphics generation queue: { alert, radarFrame snapshot }
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [graphicsQueue, setGraphicsQueue] = useState<{ alert: Alert; radarFrame: RadarBinaryFrame | null }[]>([]);
  // Always keep a ref to the latest REFLECTIVITY frame (for social graphics queue)
  const reflectivityFrameRef = useRef<RadarBinaryFrame | null>(null);

  const { chimesEnabled, toggleChimes, playForAlert, playEventType, soundConfig, refreshSoundConfig } = useAlertChimes();

  // Binary radar frame handler (main live path — replaces old image_url based handler)
  const handleRadarBinaryFrame = React.useCallback((frame: RadarBinaryFrame) => {
    setRadarFrame(frame);
    setRadarFrames(prev => ({ ...prev, [frame.site]: frame }));
    if (frame.product === 'reflectivity') {
      reflectivityFrameRef.current = frame;
    }
  }, []);

  // JSON metadata handler (kept for status pill / non-binary clients)
  const handleRadarFrame = React.useCallback((_frame: RadarFrame) => {
    // Binary frame carries all needed data; this JSON path is for status only
  }, []);

  const handleNewAlert = useCallback((alert: Alert) => {
    console.log('New alert received:', alert.event_name);
    setNewAlertToShow(alert);
    playForAlert(alert, 'new');
    // Broadcast graphic is auto-generated by the backend (_auto_generate_broadcast_graphic).
    // No longer enqueue the old AlertMapGraphic style here.
  }, [playForAlert]);

  const handleAlertUpdate = useCallback((alert: Alert) => {
    playForAlert(alert, 'update');
  }, [playForAlert]);

  const handleNewMD = useCallback((md: MesoscaleDiscussion) => {
    console.log('New MD received:', md.md_number);
    setNewMDToShow(md);
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

  const handleAgentNotification = useCallback((notification: AgentNotification) => {
    setAgentNotifications(prev => [...prev, notification]);
  }, []);

  const { connected, alerts } = useWebSocket({
    url: wsUrl(),
    onAlert: handleNewAlert,
    onAlertUpdate: handleAlertUpdate,
    onBulkAlerts: handleBulkAlerts,
    onMD: handleNewMD,
    onChaserPosition: handleChaserPosition,
    onChaserDisconnect: handleChaserDisconnect,
    onRadarBinaryFrame: handleRadarBinaryFrame,
    onRadarFrame: handleRadarFrame,
    onRadarStatus: setRadarStatus,
    onStormCells: setStormCells,
    onMcsSystems: setMcsSystems,
    onAgentNotification: handleAgentNotification,
    onLightningStrikes: (flashes) => setLightningFlashes(prev => {
      // Keep rolling 15-minute window on the frontend too
      const cutoff = Date.now() - 15 * 60 * 1000;
      return [...prev.filter(f => new Date(f.timestamp).getTime() >= cutoff), ...flashes];
    }),
  });

  // Fetch brand config and apply CSS overrides on mount
  React.useEffect(() => {
    fetch(apiUrl('/api/brand'))
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) return;
        // Brand-driven browser tab title (follows the active brand, e.g. ONW).
        document.title = `${data.name} - Alert Dashboard`;
        setBrand({
          name: data.name,
          // logo_url is a root-absolute path ("/api/brand/logo"); route it
          // through apiUrl so it gets the base prefix (e.g. "/v2/api/brand/logo")
          // when served behind a reverse proxy, instead of hitting the host root.
          logo: data.logo_url ? apiUrl(data.logo_url) : data.logo,
          website_url: data.website_url,
        });
        if (data.css_overrides) {
          const root = document.documentElement;
          Object.entries(data.css_overrides as Record<string, string>).forEach(([key, value]) => {
            root.style.setProperty(key, value);
          });
        }
      })
      .catch(() => {});
  }, []);

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

  if (kiosk) {
    return (
      <div style={{ width: '100vw', height: '100vh', background: '#000', overflow: 'hidden' }}>
        {kiosk === 'radar' && (
          <RadarSection
            radarFrame={radarFrame}
            radarFrames={radarFrames}
            radarStatus={radarStatus}
            stormCells={stormCells}
            mcsSystems={mcsSystems}
            alerts={alerts}
            lightningFlashes={lightningFlashes}
            focusedCellId={focusedCellId}
          />
        )}
      </div>
    );
  }

  return (
    <>
      <div className="app-layout">
        <Sidebar
          activeSection={activeSection}
          onSectionChange={setActiveSection}
          brandLogo={brand.logo}
          brandName={brand.name}
          brandWebsite={brand.website_url}
        />

        <div className="main-content">
          <header className="header">
            <div className="header-top">
              <h1 className="header-title">{brand.name}</h1>
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
              <AlertsSection alerts={alerts} onShareAlert={setShareAlert} />
            )}

            {activeSection === 'map' && (
              <div className="section active" style={{ height: '100%' }}>
                <h2 className="section-title">Alert Map</h2>
                <AlertMap
                  alerts={alerts}
                  onAlertClick={handleMapAlertClick}
                  selectedAlert={selectedMapAlert}
                  chasers={chasers}
                  radarFrame={radarFrame}
                  stormCells={stormCells}
                />
              </div>
            )}

          {activeSection === 'radar' && (
            <RadarSection
              radarFrame={radarFrame}
              radarFrames={radarFrames}
              radarStatus={radarStatus}
              stormCells={stormCells}
              mcsSystems={mcsSystems}
              alerts={alerts}
              lightningFlashes={lightningFlashes}
              focusedCellId={focusedCellId}
            />
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



          {activeSection === 'afd' && (
            <AFDSection />
          )}

          {activeSection === 'gusts' && (
            <WindGustsSection />
          )}

          {activeSection === 'metar' && (
            <MetarSection />
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

          {activeSection === 'social' && (
            <SocialMediaSection alerts={alerts} />
          )}

          {activeSection === 'settings' && (
            <SettingsSection
              chimesEnabled={chimesEnabled}
              onToggleChimes={toggleChimes}
              playEventType={playEventType}
              soundConfig={soundConfig}
              refreshSoundConfig={refreshSoundConfig}
            />
          )}

          {activeSection === 'event-stats' && (
            <EventStatsSection />
          )}

          {activeSection === 'alert-graphics' && (
            <AlertGraphicsSection />
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
        agentNotifications={agentNotifications}
        onNavigateToCell={(cellId) => {
          setFocusedCellId(cellId);
          setActiveSection('radar');
          setAssistantOpen(false);
        }}
      />

      {/* New Alert Notification (slide-in widget) */}
      <NewAlertNotification
        alert={newAlertToShow}
        onDismiss={() => setNewAlertToShow(null)}
      />

      {/* New MD Notification */}
      <NewMDNotification
        md={newMDToShow}
        onDismiss={() => setNewMDToShow(null)}
      />

      {/* Social Media Compose Modal (triggered from alert card share buttons) */}
      <ComposeModal
        isOpen={!!shareAlert}
        onClose={() => setShareAlert(null)}
        alert={shareAlert}
      />

      {/* Hidden graphic renderer – fixed off-screen so Leaflet can measure container size */}
      <div style={{ position: 'fixed', left: '-9999px', top: 0, width: '1024px', height: '640px', overflow: 'hidden', pointerEvents: 'none' }}>
        {graphicsQueue.map(item => (
          <AlertMapGraphic
            key={item.alert.product_id}
            alert={item.alert}
            radarFrame={item.radarFrame}
            onCapture={async (dataUrl, productId) => {
              // Remove from queue
              setGraphicsQueue(prev => prev.filter(q => q.alert.product_id !== productId));
              // Save to backend
              try {
                await fetch(apiUrl('/api/alert-graphics/save'), {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({
                    product_id: productId,
                    event_name: item.alert.event_name,
                    image_data: dataUrl,
                  }),
                });
              } catch (err) {
                console.error('Failed to save alert graphic:', err);
              }
            }}
          />
        ))}
      </div>
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

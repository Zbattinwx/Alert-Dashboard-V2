import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { MapContainer, TileLayer, Polygon, CircleMarker, Popup, useMap } from 'react-leaflet';
import { useWebSocket } from '../hooks/useWebSocket';
import { useGeoLocation } from '../hooks/useGeoLocation';
import { distanceMiles, bearing, bearingToCardinal, pointInPolygon, distanceToPolygon } from '../utils/geoUtils';
import { getAlertStyle, PHENOMENON_NAMES } from '../types/alert';
import type { Alert } from '../types/alert';
import { apiUrl, wsUrl } from '../utils/api';
import 'leaflet/dist/leaflet.css';

// SPC risk colors
const SPC_RISK_COLORS: Record<string, string> = {
  'HIGH': '#FF00FF',
  'MDT': '#FF0000',
  'ENH': '#FFA500',
  'SLGT': '#FFFF00',
  'MRGL': '#00FF00',
  'TSTM': '#76D7C4',
  'NONE': '#444',
};

// Recenter map on GPS position
const RecenterMap: React.FC<{ lat: number; lon: number; follow: boolean }> = ({ lat, lon, follow }) => {
  const map = useMap();
  useEffect(() => {
    if (follow) {
      map.setView([lat, lon], map.getZoom(), { animate: true });
    }
  }, [lat, lon, follow, map]);
  return null;
};

interface NearbyAlert {
  alert: Alert;
  distanceMi: number;
  bearingDeg: number;
  cardinal: string;
  insidePolygon: boolean;
}

export const ChaseMode: React.FC = () => {
  const [chaserName, setChaserName] = useState(() =>
    localStorage.getItem('chase_name') || ''
  );
  const [nameSet, setNameSet] = useState(() => !!localStorage.getItem('chase_name'));
  const [followGps, setFollowGps] = useState(true);
  const [spcRisk, setSpcRisk] = useState<{ risk: string; level: string } | null>(null);
  const [insideWarnings, setInsideWarnings] = useState<Set<string>>(new Set());
  const [radarTileUrl, setRadarTileUrl] = useState<string | null>(null);
  const prevInsideRef = useRef<Set<string>>(new Set());
  const lastPositionSentRef = useRef<number>(0);

  const geo = useGeoLocation();

  const handleNewAlert = useCallback(() => {}, []);

  const { connected, alerts, sendMessage } = useWebSocket({
    url: wsUrl(),
    onAlert: handleNewAlert,
  });

  // Send GPS position to server (throttled to every 5 seconds)
  useEffect(() => {
    if (!nameSet || geo.lat === null || geo.lon === null) return;

    const now = Date.now();
    if (now - lastPositionSentRef.current < 5000) return;
    lastPositionSentRef.current = now;

    sendMessage('chaser_position_update', {
      name: chaserName,
      lat: geo.lat,
      lon: geo.lon,
      heading: geo.heading,
      speed: geo.speedMph,
      accuracy: geo.accuracy,
    });
  }, [geo.lat, geo.lon, geo.heading, nameSet, chaserName, sendMessage]);

  // Fetch SPC risk at current position
  useEffect(() => {
    if (geo.lat === null || geo.lon === null) return;

    const fetchRisk = async () => {
      try {
        const resp = await fetch(apiUrl(`/api/spc/risk-at-point?lat=${geo.lat}&lon=${geo.lon}`));
        if (resp.ok) {
          const data = await resp.json();
          setSpcRisk(data);
        }
      } catch {
        // Silently fail - SPC risk is a nice-to-have
      }
    };

    fetchRisk();
    const interval = setInterval(fetchRisk, 60000); // Refresh every minute
    return () => clearInterval(interval);
  }, [geo.lat, geo.lon]);

  // Fetch latest RainViewer radar tile URL
  useEffect(() => {
    const fetchRadarFrame = async () => {
      try {
        const resp = await fetch('https://api.rainviewer.com/public/weather-maps.json');
        if (resp.ok) {
          const data = await resp.json();
          const frames = data?.radar?.past;
          if (frames && frames.length > 0) {
            const latest = frames[frames.length - 1];
            setRadarTileUrl(`${data.host}${latest.path}/256/{z}/{x}/{y}/6/1_1.png`);
          }
        }
      } catch {
        // Radar overlay is optional
      }
    };

    fetchRadarFrame();
    const interval = setInterval(fetchRadarFrame, 300000); // Refresh every 5 minutes
    return () => clearInterval(interval);
  }, []);

  // Calculate nearby alerts sorted by distance
  const nearbyAlerts = useMemo((): NearbyAlert[] => {
    if (geo.lat === null || geo.lon === null) return [];

    return alerts
      .map(alert => {
        let distMi = Infinity;
        let insidePolygon = false;

        if (alert.polygon && alert.polygon.length > 0) {
          // Check point-in-polygon
          insidePolygon = pointInPolygon(geo.lat!, geo.lon!, alert.polygon as number[][]);
          distMi = distanceToPolygon(geo.lat!, geo.lon!, alert.polygon as number[][]);
        } else if (alert.centroid) {
          distMi = distanceMiles(geo.lat!, geo.lon!, alert.centroid[0], alert.centroid[1]);
        }

        const brng = alert.centroid
          ? bearing(geo.lat!, geo.lon!, alert.centroid[0], alert.centroid[1])
          : 0;

        return {
          alert,
          distanceMi: distMi,
          bearingDeg: brng,
          cardinal: bearingToCardinal(brng),
          insidePolygon,
        };
      })
      .sort((a, b) => a.distanceMi - b.distanceMi);
  }, [alerts, geo.lat, geo.lon]);

  // Polygon entry detection with alert
  useEffect(() => {
    const currentInside = new Set<string>();
    for (const na of nearbyAlerts) {
      if (na.insidePolygon) {
        currentInside.add(na.alert.product_id);
      }
    }

    // Check for newly entered polygons
    for (const id of currentInside) {
      if (!prevInsideRef.current.has(id)) {
        // Just entered a new polygon - vibrate and play alert tone
        if (navigator.vibrate) {
          navigator.vibrate([200, 100, 200, 100, 200]);
        }
        try {
          const ctx = new AudioContext();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.frequency.value = 880;
          osc.type = 'square';
          gain.gain.value = 0.3;
          osc.start();
          osc.stop(ctx.currentTime + 0.5);
        } catch {
          // Audio may be blocked
        }
      }
    }

    setInsideWarnings(currentInside);
    prevInsideRef.current = currentInside;
  }, [nearbyAlerts]);

  // Name entry screen
  if (!nameSet) {
    return (
      <div className="chase-name-screen">
        <div className="chase-name-card">
          <h2>Chase Mode</h2>
          <p>Enter your name to begin tracking</p>
          <input
            type="text"
            value={chaserName}
            onChange={e => setChaserName(e.target.value)}
            placeholder="Your name"
            className="chase-name-input"
            autoFocus
          />
          <button
            className="chase-name-submit"
            disabled={!chaserName.trim()}
            onClick={() => {
              localStorage.setItem('chase_name', chaserName.trim());
              setNameSet(true);
            }}
          >
            Start Chasing
          </button>
        </div>
      </div>
    );
  }

  // GPS loading/error state
  if (geo.loading) {
    return (
      <div className="chase-name-screen">
        <div className="chase-name-card">
          <h2>Acquiring GPS...</h2>
          <p>Please allow location access</p>
          <div className="chase-gps-spinner"></div>
        </div>
      </div>
    );
  }

  if (geo.error) {
    return (
      <div className="chase-name-screen">
        <div className="chase-name-card">
          <h2>GPS Error</h2>
          <p>{geo.error}</p>
          <p style={{ fontSize: '0.8rem', color: '#888', marginTop: '8px' }}>
            Make sure location services are enabled and the page has permission.
          </p>
        </div>
      </div>
    );
  }

  const alertsInside = nearbyAlerts.filter(na => na.insidePolygon);

  return (
    <div className="chase-layout">
      {/* Warning banner when inside a polygon */}
      {insideWarnings.size > 0 && (
        <div className="chase-warning-banner">
          <i className="fas fa-exclamation-triangle"></i>
          YOU ARE INSIDE {insideWarnings.size > 1 ? `${insideWarnings.size} WARNING AREAS` : 'A WARNING AREA'}
          <span className="chase-warning-names">
            {alertsInside.map(na => na.alert.event_name).join(' | ')}
          </span>
        </div>
      )}

      {/* Status bar */}
      <div className="chase-status-bar">
        <div className="chase-status-left">
          <span className={`chase-status-dot ${connected ? 'connected' : ''}`}></span>
          <span className="chase-name-label">{chaserName}</span>
        </div>
        <div className="chase-status-right">
          {geo.speedMph !== null && geo.speedMph > 1 && (
            <span className="chase-speed">{Math.round(geo.speedMph)} mph</span>
          )}
          {geo.accuracy && (
            <span className="chase-accuracy">
              GPS {geo.accuracy < 10 ? 'precise' : geo.accuracy < 50 ? 'good' : 'weak'}
            </span>
          )}
        </div>
      </div>

      {/* Map */}
      <div className="chase-map-container">
        {geo.lat !== null && geo.lon !== null && (
          <MapContainer
            center={[geo.lat, geo.lon]}
            zoom={10}
            style={{ height: '100%', width: '100%' }}
            zoomControl={false}
          >
            <TileLayer
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
              attribution='&copy; OpenStreetMap'
            />

            {/* RainViewer radar overlay */}
            {radarTileUrl && (
              <TileLayer
                url={radarTileUrl}
                opacity={0.5}
                zIndex={10}
              />
            )}

            <RecenterMap lat={geo.lat} lon={geo.lon} follow={followGps} />

            {/* Your position - pulsing blue dot */}
            <CircleMarker
              center={[geo.lat, geo.lon]}
              radius={8}
              pathOptions={{
                color: '#00CED1',
                fillColor: '#00CED1',
                fillOpacity: 0.9,
                weight: 3,
              }}
            >
              <Popup>{chaserName}'s Position</Popup>
            </CircleMarker>

            {/* Accuracy ring */}
            {geo.accuracy && geo.accuracy > 20 && (
              <CircleMarker
                center={[geo.lat, geo.lon]}
                radius={Math.min(geo.accuracy / 5, 40)}
                pathOptions={{
                  color: '#00CED1',
                  fillColor: '#00CED1',
                  fillOpacity: 0.1,
                  weight: 1,
                  dashArray: '4',
                }}
              />
            )}

            {/* Alert polygons */}
            {alerts.map(alert => {
              if (!alert.polygon || alert.polygon.length === 0) return null;
              const style = getAlertStyle(alert.phenomenon, alert.significance);
              const positions = (alert.polygon as number[][]).map(
                (coord) => [coord[0], coord[1]] as [number, number]
              );
              return (
                <Polygon
                  key={alert.product_id}
                  positions={positions}
                  pathOptions={{
                    color: style.backgroundColor,
                    fillColor: style.backgroundColor,
                    fillOpacity: 0.3,
                    weight: 2,
                  }}
                >
                  <Popup>
                    <strong style={{ color: style.backgroundColor }}>{alert.event_name}</strong>
                    <br />
                    {alert.display_locations}
                  </Popup>
                </Polygon>
              );
            })}
          </MapContainer>
        )}

        {/* Map controls */}
        <button
          className={`chase-follow-btn ${followGps ? 'active' : ''}`}
          onClick={() => setFollowGps(!followGps)}
          title={followGps ? 'Stop following GPS' : 'Follow GPS'}
        >
          <i className="fas fa-crosshairs"></i>
        </button>
      </div>

      {/* Info panel */}
      <div className="chase-info-panel">
        {/* SPC Risk Card */}
        {spcRisk && spcRisk.risk !== 'NONE' && (
          <div
            className="chase-spc-card"
            style={{ borderLeftColor: SPC_RISK_COLORS[spcRisk.risk] || '#444' }}
          >
            <div className="chase-spc-label">SPC RISK</div>
            <div
              className="chase-spc-level"
              style={{ color: SPC_RISK_COLORS[spcRisk.risk] || '#888' }}
            >
              {spcRisk.risk}
            </div>
          </div>
        )}

        {/* Alerts count */}
        <div className="chase-alerts-header">
          <span>{nearbyAlerts.length} Active Alert{nearbyAlerts.length !== 1 ? 's' : ''}</span>
        </div>

        {/* Nearby alerts list */}
        {nearbyAlerts.length === 0 ? (
          <div className="chase-no-alerts">No active alerts nearby</div>
        ) : (
          <div className="chase-alerts-list">
            {nearbyAlerts.map(na => {
              const style = getAlertStyle(na.alert.phenomenon, na.alert.significance);
              const phenomName = PHENOMENON_NAMES[na.alert.phenomenon] || na.alert.event_name;

              return (
                <div
                  key={na.alert.product_id}
                  className={`chase-alert-item ${na.insidePolygon ? 'inside' : ''}`}
                  style={{ borderLeftColor: style.backgroundColor }}
                >
                  <div className="chase-alert-top">
                    <span
                      className="chase-alert-badge"
                      style={{ backgroundColor: style.backgroundColor, color: style.textColor }}
                    >
                      {na.alert.phenomenon}
                    </span>
                    <span className="chase-alert-name">{phenomName}</span>
                    <span className="chase-alert-dist">
                      {na.insidePolygon ? (
                        <strong style={{ color: '#ff4444' }}>INSIDE</strong>
                      ) : na.distanceMi < 100 ? (
                        `${na.distanceMi.toFixed(1)} mi ${na.cardinal}`
                      ) : (
                        `${Math.round(na.distanceMi)} mi ${na.cardinal}`
                      )}
                    </span>
                  </div>

                  <div className="chase-alert-details">
                    {na.alert.display_locations && (
                      <span className="chase-alert-loc">{na.alert.display_locations}</span>
                    )}
                  </div>

                  {/* Threat info */}
                  <div className="chase-alert-threats">
                    {na.alert.threat?.tornado_detection && (
                      <span className="chase-threat tornado">
                        TORNADO {na.alert.threat.tornado_detection}
                      </span>
                    )}
                    {na.alert.threat?.max_wind_gust_mph && (
                      <span className="chase-threat">
                        {na.alert.threat.max_wind_gust_mph} mph wind
                      </span>
                    )}
                    {na.alert.threat?.max_hail_size_inches && (
                      <span className="chase-threat">
                        {na.alert.threat.max_hail_size_inches}" hail
                      </span>
                    )}
                    {na.alert.threat?.storm_motion && na.alert.threat.storm_motion.speed_mph && (
                      <span className="chase-threat motion">
                        Moving {na.alert.threat.storm_motion.direction_from} at {na.alert.threat.storm_motion.speed_mph} mph
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};

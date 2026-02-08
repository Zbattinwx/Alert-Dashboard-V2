import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { MapContainer, TileLayer, Marker, Popup, useMap } from 'react-leaflet';
import L from 'leaflet';
import type {
  ODOTCamera,
  RoadSensor,
  CamerasResponse,
  ColdSensorsResponse,
  CamerasInAlertsResponse,
} from '../types/odot';
import 'leaflet/dist/leaflet.css';

// Create custom icons
const cameraIcon = L.divIcon({
  html: '<i class="fas fa-camera" style="color: #7dcfff; font-size: 16px;"></i>',
  className: 'odot-map-icon',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

const cameraInAlertIcon = L.divIcon({
  html: '<i class="fas fa-camera" style="color: #ff0000; font-size: 18px; text-shadow: 0 0 6px #ff0000;"></i>',
  className: 'odot-map-icon',
  iconSize: [24, 24],
  iconAnchor: [12, 12],
});

const sensorIcon = L.divIcon({
  html: '<i class="fas fa-thermometer-half" style="color: #9ece6a; font-size: 16px;"></i>',
  className: 'odot-map-icon',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

const coldSensorIcon = L.divIcon({
  html: '<i class="fas fa-thermometer-quarter" style="color: #7dcfff; font-size: 16px;"></i>',
  className: 'odot-map-icon',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

const freezingSensorIcon = L.divIcon({
  html: '<i class="fas fa-snowflake" style="color: #00bfff; font-size: 16px;"></i>',
  className: 'odot-map-icon',
  iconSize: [20, 20],
  iconAnchor: [10, 10],
});

// Component to fit map bounds
const FitBoundsToItems: React.FC<{ cameras: ODOTCamera[]; sensors: RoadSensor[] }> = ({
  cameras,
  sensors,
}) => {
  const map = useMap();

  useEffect(() => {
    const points: L.LatLngExpression[] = [];

    cameras.forEach((c) => {
      if (c.latitude && c.longitude) {
        points.push([c.latitude, c.longitude]);
      }
    });

    sensors.forEach((s) => {
      if (s.latitude && s.longitude) {
        points.push([s.latitude, s.longitude]);
      }
    });

    if (points.length > 0) {
      const bounds = L.latLngBounds(points);
      map.fitBounds(bounds, { padding: [50, 50] });
    }
  }, [cameras, sensors, map]);

  return null;
};

// Live camera image component with auto-refresh
const LiveCameraImage: React.FC<{ url: string; alt: string }> = ({ url, alt }) => {
  const [imgSrc, setImgSrc] = useState(`${url}?t=${Date.now()}`);

  useEffect(() => {
    const interval = setInterval(() => {
      setImgSrc(`${url}?t=${Date.now()}`);
    }, 5000); // Refresh every 5 seconds

    return () => clearInterval(interval);
  }, [url]);

  return (
    <img
      src={imgSrc}
      alt={alt}
      style={{ width: '100%', maxWidth: '500px', borderRadius: '4px', marginTop: '5px' }}
      onError={(e) => {
        (e.target as HTMLImageElement).style.display = 'none';
      }}
    />
  );
};

export const ODOTSection: React.FC = () => {
  const [cameras, setCameras] = useState<ODOTCamera[]>([]);
  const [coldSensors, setColdSensors] = useState<RoadSensor[]>([]);
  const [camerasInAlerts, setCamerasInAlerts] = useState<ODOTCamera[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [freezingCount, setFreezingCount] = useState(0);
  const [coldThreshold, setColdThreshold] = useState(40);
  // freezingThreshold stored but not currently displayed in UI
  const [, setFreezingThreshold] = useState(32);
  const [mapReady, setMapReady] = useState(false);
  const [showCameras, setShowCameras] = useState(true);
  const [showSensors, setShowSensors] = useState(true);
  const [selectedCamera, setSelectedCamera] = useState<ODOTCamera | null>(null);

  // Default center: Ohio
  const defaultCenter: L.LatLngExpression = [40.4173, -82.9071];
  const defaultZoom = 7;

  // Fetch all ODOT data
  const fetchData = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);

    try {
      const [camerasRes, coldRes, alertCamsRes] = await Promise.all([
        fetch(`/api/odot/cameras?refresh=${refresh}`),
        fetch(`/api/odot/cold-sensors?refresh=${refresh}`),
        fetch(`/api/odot/cameras-in-alerts?refresh=${refresh}`),
      ]);

      if (!camerasRes.ok || !coldRes.ok || !alertCamsRes.ok) {
        throw new Error('Failed to fetch ODOT data');
      }

      const camerasData: CamerasResponse = await camerasRes.json();
      const coldData: ColdSensorsResponse = await coldRes.json();
      const alertCamsData: CamerasInAlertsResponse = await alertCamsRes.json();

      setCameras(camerasData.cameras);
      setColdSensors(coldData.sensors);
      setFreezingCount(coldData.freezing_count);
      setColdThreshold(coldData.cold_threshold);
      setFreezingThreshold(coldData.freezing_threshold);
      setCamerasInAlerts(alertCamsData.cameras);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch ODOT data');
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch and periodic refresh
  useEffect(() => {
    fetchData();

    // Refresh every 5 minutes
    const interval = setInterval(() => fetchData(), 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [fetchData]);

  // Get sensor icon based on temperature
  const getSensorIcon = (sensor: RoadSensor) => {
    if (sensor.is_freezing) return freezingSensorIcon;
    if (sensor.is_cold) return coldSensorIcon;
    return sensorIcon;
  };

  // Check if all sensors are cold
  const allSensorsAreCold = useMemo(() => {
    return coldSensors.length > 0 && coldSensors.every((s) => s.is_cold);
  }, [coldSensors]);

  return (
    <div className="odot-section">
      {/* Header */}
      <div className="odot-header">
        <h2 className="section-title">
          <i className="fas fa-road"></i> ODOT Cameras & Sensors
        </h2>
        <div className="odot-controls">
          <label className="odot-toggle">
            <input
              type="checkbox"
              checked={showCameras}
              onChange={(e) => setShowCameras(e.target.checked)}
            />
            <span>Cameras</span>
          </label>
          <label className="odot-toggle">
            <input
              type="checkbox"
              checked={showSensors}
              onChange={(e) => setShowSensors(e.target.checked)}
            />
            <span>Sensors</span>
          </label>
          <button onClick={() => fetchData(true)} className="odot-refresh-btn" disabled={loading}>
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i>
            Refresh
          </button>
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="odot-error">
          <i className="fas fa-exclamation-triangle"></i>
          Error loading ODOT data: {error}
        </div>
      )}

      {/* Stats bar */}
      <div className="odot-stats-bar">
        <div className="odot-stat">
          <i className="fas fa-camera"></i>
          <span>{cameras.length} Cameras</span>
        </div>
        <div className="odot-stat">
          <i className="fas fa-thermometer-half"></i>
          <span>{coldSensors.length} Cold Sensors</span>
        </div>
        {freezingCount > 0 && (
          <div className="odot-stat freezing">
            <i className="fas fa-snowflake"></i>
            <span>{freezingCount} Freezing</span>
          </div>
        )}
        {camerasInAlerts.length > 0 && (
          <div className="odot-stat alert">
            <i className="fas fa-exclamation-triangle"></i>
            <span>{camerasInAlerts.length} in Alerts</span>
          </div>
        )}
      </div>

      {/* Main content */}
      <div className="odot-content">
        {/* Map */}
        <div className="odot-map-container">
          <MapContainer
            center={defaultCenter}
            zoom={defaultZoom}
            style={{ height: '100%', width: '100%', borderRadius: 'var(--radius-md)' }}
            whenReady={() => setMapReady(true)}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            />

            {mapReady && <FitBoundsToItems cameras={cameras} sensors={coldSensors} />}

            {/* Cameras */}
            {showCameras &&
              cameras.map((camera) => {
                const isInAlert = camerasInAlerts.some((c) => c.id === camera.id);
                return (
                  <Marker
                    key={`camera-${camera.id}`}
                    position={[camera.latitude, camera.longitude]}
                    icon={isInAlert ? cameraInAlertIcon : cameraIcon}
                    eventHandlers={{
                      click: () => setSelectedCamera(camera),
                    }}
                  />
                );
              })}

            {/* Cold Sensors */}
            {showSensors &&
              coldSensors.map((sensor) => (
                <Marker
                  key={`sensor-${sensor.id}`}
                  position={[sensor.latitude, sensor.longitude]}
                  icon={getSensorIcon(sensor)}
                >
                  <Popup>
                    <div className="odot-popup">
                      <h4>
                        {sensor.is_freezing ? (
                          <i className="fas fa-snowflake" style={{ color: '#00bfff' }}></i>
                        ) : (
                          <i className="fas fa-thermometer-quarter" style={{ color: '#7dcfff' }}></i>
                        )}
                        {' '}{sensor.location}
                      </h4>
                      <div className="odot-popup-temps">
                        <div className={`odot-temp ${sensor.is_freezing ? 'freezing' : 'cold'}`}>
                          <span className="label">Pavement:</span>
                          <span className="value">{sensor.pavement_temp}°F</span>
                          {sensor.is_freezing && <span className="warning">FREEZING</span>}
                        </div>
                        {sensor.air_temp !== null && (
                          <div className="odot-temp">
                            <span className="label">Air:</span>
                            <span className="value">{sensor.air_temp}°F</span>
                          </div>
                        )}
                      </div>
                      {sensor.wind_speed !== null && (
                        <p className="odot-popup-wind">
                          Wind: {sensor.wind_speed} MPH {sensor.wind_direction || ''}
                        </p>
                      )}
                    </div>
                  </Popup>
                </Marker>
              ))}
          </MapContainer>
        </div>

        {/* Side panel */}
        <div className="odot-side-panel">
          {/* Cameras in Alerts */}
          {camerasInAlerts.length > 0 && (
            <div className="odot-panel-section">
              <h3>
                <i className="fas fa-exclamation-triangle"></i> Cameras in Alerts
              </h3>
              <div className="odot-alert-cameras">
                {camerasInAlerts.map((camera) => (
                  <div
                    key={camera.id}
                    className="odot-alert-camera-card"
                    onClick={() => setSelectedCamera(camera)}
                  >
                    <div className="camera-header">
                      <i className="fas fa-camera"></i>
                      <span className="camera-location">{camera.location}</span>
                    </div>
                    <div className="camera-alert-type">{camera.alert_name}</div>
                    <img
                      src={`${camera.image_url}?t=${Date.now()}`}
                      alt={camera.location}
                      className="camera-thumbnail"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Cold Sensors Table */}
          <div className="odot-panel-section">
            <h3>
              <i className="fas fa-thermometer-quarter"></i> Cold Pavement Sensors
              {allSensorsAreCold && coldSensors.length > 5 && (
                <span className="cold-message">All sensors reporting cold!</span>
              )}
            </h3>
            {coldSensors.length === 0 ? (
              <div className="odot-empty">
                <i className="fas fa-sun"></i>
                <p>No cold pavement detected</p>
                <p className="odot-empty-sub">All sensors above {coldThreshold}°F</p>
              </div>
            ) : (
              <div className="odot-cold-sensors-list">
                {coldSensors.slice(0, 20).map((sensor) => (
                  <div
                    key={sensor.id}
                    className={`odot-cold-sensor-item ${sensor.is_freezing ? 'freezing' : 'cold'}`}
                  >
                    <div className="sensor-icon">
                      {sensor.is_freezing ? (
                        <i className="fas fa-snowflake"></i>
                      ) : (
                        <i className="fas fa-thermometer-quarter"></i>
                      )}
                    </div>
                    <div className="sensor-info">
                      <div className="sensor-location">{sensor.location}</div>
                      <div className="sensor-details">
                        <span className="pavement-temp">
                          Pavement: {sensor.pavement_temp}°F
                        </span>
                        {sensor.air_temp !== null && (
                          <span className="air-temp">Air: {sensor.air_temp}°F</span>
                        )}
                      </div>
                    </div>
                    <div className="sensor-status">
                      {sensor.is_freezing ? (
                        <span className="status-freezing">ICE POSSIBLE</span>
                      ) : (
                        <span className="status-cold">COLD</span>
                      )}
                    </div>
                  </div>
                ))}
                {coldSensors.length > 20 && (
                  <div className="odot-more-sensors">
                    +{coldSensors.length - 20} more sensors
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Selected Camera Modal */}
      {selectedCamera && (
        <div className="odot-camera-modal-overlay" onClick={() => setSelectedCamera(null)}>
          <div className="odot-camera-modal" onClick={(e) => e.stopPropagation()}>
            <div className="odot-camera-modal-header">
              <h3>
                <i className="fas fa-camera"></i> {selectedCamera.location}
              </h3>
              <button onClick={() => setSelectedCamera(null)}>
                <i className="fas fa-times"></i>
              </button>
            </div>
            <div className="odot-camera-modal-body">
              {camerasInAlerts.some((c) => c.id === selectedCamera.id) && (
                <div className="odot-camera-alert-banner">
                  <i className="fas fa-exclamation-triangle"></i>
                  This camera is inside an active weather alert
                </div>
              )}
              <LiveCameraImage url={selectedCamera.image_url} alt={selectedCamera.location} />
              <p className="odot-camera-coords">
                {selectedCamera.latitude.toFixed(4)}, {selectedCamera.longitude.toFixed(4)}
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

import React, { useEffect, useMemo, useState, useCallback } from 'react';
import { MapContainer, TileLayer, Polygon, Popup, Marker, useMap } from 'react-leaflet';
import L from 'leaflet';
import type { Alert } from '../types/alert';
import { getAlertStyle, PHENOMENON_NAMES } from '../types/alert';
import type { ChaserPosition } from '../types/chaser';
import { apiUrl } from '../utils/api';
import 'leaflet/dist/leaflet.css';

// Fix for default marker icons in Leaflet with webpack/vite
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png',
});

interface AlertMapProps {
  alerts: Alert[];
  onAlertClick?: (alert: Alert) => void;
  selectedAlert?: Alert | null;
  chasers?: ChaserPosition[];
}

// Create a custom icon for chaser markers
const createChaserIcon = (name: string, heading: number | null) => {
  const rotation = heading !== null ? heading : 0;
  return L.divIcon({
    className: 'chaser-marker-icon',
    html: `
      <div style="position:relative;width:32px;height:32px;">
        <div style="
          width:14px;height:14px;
          background:#00CED1;
          border:2px solid white;
          border-radius:50%;
          position:absolute;
          top:50%;left:50%;
          transform:translate(-50%,-50%);
          box-shadow:0 0 8px rgba(0,206,209,0.6);
        "></div>
        ${heading !== null ? `<div style="
          position:absolute;top:0;left:50%;
          transform:translateX(-50%) rotate(${rotation}deg);
          transform-origin:center 16px;
          width:0;height:0;
          border-left:4px solid transparent;
          border-right:4px solid transparent;
          border-bottom:8px solid #00CED1;
        "></div>` : ''}
        <div class="chaser-marker-label">${name}</div>
      </div>
    `,
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  });
};

// Zone data from API
interface ZoneData {
  zone_id: string;
  geometry: number[][][];
  alert: {
    product_id: string;
    phenomenon: string;
    significance: string;
    event_name: string;
    headline?: string;
    expiration_time?: string;
    sender_office?: string;
    display_locations?: string;
  };
}

interface AlertType {
  phenomenon: string;
  significance: string;
  event_name: string;
  count: number;
}

interface MapZonesResponse {
  zones: ZoneData[];
  alert_types: AlertType[];
  total_zones: number;
}

// Polygon alerts that should always render on top (storm-based)
const POLYGON_ALERT_PHENOMENA = new Set([
  'TO', 'TOR', 'SV', 'SVR', 'FF', 'FFW', 'FFS', 'SVS', 'SQ',
]);

// Component to fit map bounds
const FitBounds: React.FC<{ zones: ZoneData[]; polygonAlerts: Alert[] }> = ({ zones, polygonAlerts }) => {
  const map = useMap();

  useEffect(() => {
    const allPoints: L.LatLngExpression[] = [];

    // Add zone points
    for (const zone of zones) {
      for (const polygon of zone.geometry) {
        for (const coord of polygon) {
          allPoints.push([coord[0], coord[1]]);
        }
      }
    }

    // Add polygon alert points
    for (const alert of polygonAlerts) {
      if (alert.polygon) {
        const extractPoints = (data: unknown): void => {
          if (!Array.isArray(data)) return;
          if (data.length === 2 && typeof data[0] === 'number' && typeof data[1] === 'number') {
            allPoints.push([data[0], data[1]] as L.LatLngExpression);
            return;
          }
          for (const item of data) {
            if (Array.isArray(item)) extractPoints(item);
          }
        };
        extractPoints(alert.polygon);
      }
    }

    if (allPoints.length > 0) {
      const bounds = L.latLngBounds(allPoints);
      map.fitBounds(bounds, { padding: [50, 50] });
    }
  }, [zones, polygonAlerts, map]);

  return null;
};

// Component to zoom to selected alert
const ZoomToAlert: React.FC<{ alert: Alert | null }> = ({ alert }) => {
  const map = useMap();

  useEffect(() => {
    if (!alert?.polygon) return;

    const points: L.LatLngExpression[] = [];
    const extractPoints = (data: unknown): void => {
      if (!Array.isArray(data)) return;
      if (data.length === 2 && typeof data[0] === 'number' && typeof data[1] === 'number') {
        points.push([data[0], data[1]] as L.LatLngExpression);
        return;
      }
      for (const item of data) {
        if (Array.isArray(item)) extractPoints(item);
      }
    };
    extractPoints(alert.polygon);

    if (points.length > 0) {
      const bounds = L.latLngBounds(points);
      map.fitBounds(bounds, { padding: [100, 100], animate: true });
    }
  }, [alert, map]);

  return null;
};

export const AlertMap: React.FC<AlertMapProps> = ({
  alerts,
  onAlertClick,
  selectedAlert,
  chasers = [],
}) => {
  const [mapReady, setMapReady] = useState(false);
  const [zoneData, setZoneData] = useState<MapZonesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeFilter, setActiveFilter] = useState<string | null>(null);

  // Default center: Ohio region
  const defaultCenter: L.LatLngExpression = [39.9612, -82.9988];
  const defaultZoom = 7;

  // Fetch zone data from API
  const fetchZoneData = useCallback(async () => {
    try {
      const response = await fetch(apiUrl('/api/map/zones'));
      if (response.ok) {
        const data = await response.json();
        setZoneData(data);
      }
    } catch (error) {
      console.error('Failed to fetch zone data:', error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchZoneData();
    // Refresh every 30 seconds
    const interval = setInterval(fetchZoneData, 30000);
    return () => clearInterval(interval);
  }, [fetchZoneData]);

  // Separate polygon alerts (TOR, SVR, FFW) from the alerts prop
  const polygonAlerts = useMemo(() => {
    return alerts.filter(a =>
      a.polygon &&
      a.polygon.length > 0 &&
      POLYGON_ALERT_PHENOMENA.has(a.phenomenon)
    );
  }, [alerts]);

  // Filter zones based on active filter
  const filteredZones = useMemo(() => {
    if (!zoneData?.zones) return [];
    if (!activeFilter) return zoneData.zones;
    return zoneData.zones.filter(z => z.alert.phenomenon === activeFilter);
  }, [zoneData, activeFilter]);

  // Get color for a zone based on its alert
  const getZoneColor = (zone: ZoneData) => {
    const style = getAlertStyle(zone.alert.phenomenon, zone.alert.significance);
    return style.backgroundColor;
  };

  // Get color for a polygon alert
  const getPolygonColor = (alert: Alert) => {
    const style = getAlertStyle(alert.phenomenon, alert.significance);
    return style.backgroundColor;
  };

  // Format time for popup
  const formatTime = (isoString: string | null | undefined) => {
    if (!isoString) return '';
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  };

  // Normalize polygon data for Leaflet
  const normalizePolygon = (polygon: unknown): [number, number][][] => {
    const result: [number, number][][] = [];

    const isCoordinate = (arr: unknown): arr is [number, number] => {
      return Array.isArray(arr) && arr.length === 2 &&
        typeof arr[0] === 'number' && typeof arr[1] === 'number';
    };

    const isRing = (arr: unknown): arr is number[][] => {
      return Array.isArray(arr) && arr.length > 0 && isCoordinate(arr[0]);
    };

    const extractPolygons = (data: unknown): void => {
      if (!Array.isArray(data) || data.length === 0) return;
      if (isRing(data)) {
        result.push(data.map(coord => [coord[0], coord[1]] as [number, number]));
        return;
      }
      for (const item of data) {
        if (Array.isArray(item)) extractPolygons(item);
      }
    };

    extractPolygons(polygon);
    return result;
  };

  // Find full alert object for a zone
  const findAlertForZone = (zone: ZoneData): Alert | undefined => {
    return alerts.find(a => a.product_id === zone.alert.product_id);
  };

  // Toggle filter
  const toggleFilter = (phenomenon: string) => {
    setActiveFilter(prev => prev === phenomenon ? null : phenomenon);
  };

  return (
    <div className="map-container" style={{ height: '100%', width: '100%', position: 'relative' }}>
      <MapContainer
        center={defaultCenter}
        zoom={defaultZoom}
        style={{ height: '100%', width: '100%', borderRadius: 'var(--radius-md)' }}
        whenReady={() => setMapReady(true)}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        />

        {mapReady && <FitBounds zones={filteredZones} polygonAlerts={polygonAlerts} />}
        {mapReady && <ZoomToAlert alert={selectedAlert || null} />}

        {/* Render zone fills - each zone only once with its winning alert */}
        {filteredZones.map((zone) => {
          const color = getZoneColor(zone);
          const fullAlert = findAlertForZone(zone);
          const isSelected = selectedAlert?.product_id === zone.alert.product_id;

          return zone.geometry.map((polygon, polygonIndex) => (
            <Polygon
              key={`zone-${zone.zone_id}-${polygonIndex}`}
              positions={polygon.map(coord => [coord[0], coord[1]] as [number, number])}
              pathOptions={{
                color: color,
                fillColor: color,
                fillOpacity: isSelected ? 0.5 : 0.3,
                weight: isSelected ? 2 : 1,
              }}
              eventHandlers={{
                click: () => fullAlert && onAlertClick?.(fullAlert),
              }}
            >
              <Popup>
                <div style={{ minWidth: '200px' }}>
                  <h4 style={{ margin: '0 0 8px 0', color: color }}>
                    {zone.alert.event_name}
                  </h4>
                  <p style={{ margin: '0 0 4px 0', fontSize: '0.8rem', color: '#888' }}>
                    {zone.zone_id}
                  </p>
                  {zone.alert.display_locations && (
                    <p style={{ margin: '0 0 8px 0', fontSize: '0.85rem' }}>
                      {zone.alert.display_locations}
                    </p>
                  )}
                  <p style={{ margin: '0', fontSize: '0.8rem', color: '#666' }}>
                    <strong>Expires:</strong> {formatTime(zone.alert.expiration_time)}<br />
                    <strong>Office:</strong> {zone.alert.sender_office || 'Unknown'}
                  </p>
                  {fullAlert && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onAlertClick?.(fullAlert);
                      }}
                      style={{
                        marginTop: '8px',
                        padding: '4px 12px',
                        backgroundColor: color,
                        color: getAlertStyle(zone.alert.phenomenon, zone.alert.significance).textColor,
                        border: 'none',
                        borderRadius: '4px',
                        cursor: 'pointer',
                        fontSize: '0.8rem',
                      }}
                    >
                      View Details
                    </button>
                  )}
                </div>
              </Popup>
            </Polygon>
          ));
        })}

        {/* Render polygon alerts on top (TOR, SVR, FFW) */}
        {(!activeFilter || POLYGON_ALERT_PHENOMENA.has(activeFilter)) && polygonAlerts.map((alert) => {
          const color = getPolygonColor(alert);
          const isSelected = selectedAlert?.product_id === alert.product_id;
          const polygons = normalizePolygon(alert.polygon);

          return polygons.map((positions, polygonIndex) => (
            <Polygon
              key={`polygon-${alert.product_id}-${polygonIndex}`}
              positions={positions}
              pathOptions={{
                color: color,
                fillColor: color,
                fillOpacity: isSelected ? 0.6 : 0.45,
                weight: isSelected ? 4 : 3,
              }}
              eventHandlers={{
                click: () => onAlertClick?.(alert),
              }}
            >
              <Popup>
                <div style={{ minWidth: '200px' }}>
                  <h4 style={{ margin: '0 0 8px 0', color: color }}>
                    {alert.event_name}
                  </h4>
                  <p style={{ margin: '0 0 8px 0', fontSize: '0.85rem' }}>
                    {alert.display_locations || alert.affected_areas.join(', ')}
                  </p>
                  <p style={{ margin: '0', fontSize: '0.8rem', color: '#666' }}>
                    <strong>Expires:</strong> {formatTime(alert.expiration_time)}<br />
                    <strong>Office:</strong> {alert.sender_name || alert.sender_office || 'Unknown'}
                  </p>
                  {alert.threat.tornado_detection && (
                    <p style={{
                      margin: '8px 0 0 0',
                      padding: '4px 8px',
                      backgroundColor: '#ff0000',
                      color: 'white',
                      borderRadius: '4px',
                      fontSize: '0.8rem',
                      fontWeight: 'bold',
                    }}>
                      TORNADO {alert.threat.tornado_detection}
                    </p>
                  )}
                  {alert.threat.max_wind_gust_mph && (
                    <p style={{ margin: '8px 0 0 0', fontSize: '0.8rem' }}>
                      <strong>Wind:</strong> {alert.threat.max_wind_gust_mph} mph
                      {alert.threat.max_hail_size_inches && (
                        <> | <strong>Hail:</strong> {alert.threat.max_hail_size_inches}"</>
                      )}
                    </p>
                  )}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onAlertClick?.(alert);
                    }}
                    style={{
                      marginTop: '8px',
                      padding: '4px 12px',
                      backgroundColor: color,
                      color: getAlertStyle(alert.phenomenon, alert.significance).textColor,
                      border: 'none',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '0.8rem',
                    }}
                  >
                    View Details
                  </button>
                </div>
              </Popup>
            </Polygon>
          ));
        })}

        {/* Chaser markers */}
        {chasers.map(chaser => (
          <Marker
            key={`chaser-${chaser.client_id}`}
            position={[chaser.lat, chaser.lon]}
            icon={createChaserIcon(chaser.name, chaser.heading)}
          >
            <Popup>
              <div style={{ minWidth: '140px' }}>
                <h4 style={{ margin: '0 0 4px 0', color: '#00CED1' }}>{chaser.name}</h4>
                {chaser.speed !== null && (
                  <p style={{ margin: '0 0 2px 0', fontSize: '0.8rem' }}>
                    <strong>Speed:</strong> {Math.round(chaser.speed)} mph
                  </p>
                )}
                <p style={{ margin: '0', fontSize: '0.75rem', color: '#888' }}>
                  Last update: {new Date(chaser.last_update).toLocaleTimeString()}
                </p>
              </div>
            </Popup>
          </Marker>
        ))}
      </MapContainer>

      {/* Filter controls */}
      {zoneData && zoneData.alert_types.length > 0 && (
        <div style={{
          position: 'absolute',
          top: '10px',
          left: '50px',
          right: '50px',
          backgroundColor: 'var(--bg-secondary)',
          padding: '10px 12px',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--border-color)',
          zIndex: 1000,
          display: 'flex',
          flexWrap: 'wrap',
          gap: '6px',
          justifyContent: 'center',
        }}>
          <button
            onClick={() => setActiveFilter(null)}
            style={{
              padding: '6px 12px',
              backgroundColor: !activeFilter ? 'var(--primary-color)' : 'var(--bg-tertiary)',
              color: !activeFilter ? 'white' : 'var(--text-secondary)',
              border: '1px solid var(--border-color)',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '0.8rem',
              fontWeight: 500,
            }}
          >
            All Alerts
          </button>
          {zoneData.alert_types.map(type => {
            const style = getAlertStyle(type.phenomenon, type.significance);
            const isActive = activeFilter === type.phenomenon;
            return (
              <button
                key={type.phenomenon}
                onClick={() => toggleFilter(type.phenomenon)}
                style={{
                  padding: '6px 12px',
                  backgroundColor: isActive ? style.backgroundColor : 'var(--bg-tertiary)',
                  color: isActive ? style.textColor : 'var(--text-secondary)',
                  border: `1px solid ${isActive ? style.backgroundColor : 'var(--border-color)'}`,
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontSize: '0.8rem',
                  fontWeight: 500,
                  opacity: isActive ? 1 : 0.8,
                }}
              >
                {PHENOMENON_NAMES[type.phenomenon]?.replace(' Warning', '').replace(' Advisory', '') || type.event_name}
              </button>
            );
          })}
        </div>
      )}

      {/* Legend */}
      <div style={{
        position: 'absolute',
        bottom: '20px',
        left: '10px',
        backgroundColor: 'var(--bg-secondary)',
        padding: '10px 14px',
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-color)',
        zIndex: 1000,
        fontSize: '0.8rem',
      }}>
        {loading ? (
          <div style={{ color: 'var(--text-secondary)' }}>Loading zones...</div>
        ) : (
          <>
            <div style={{ fontWeight: 600, marginBottom: '4px' }}>
              {filteredZones.length} zone{filteredZones.length !== 1 ? 's' : ''}
              {activeFilter && ` (${PHENOMENON_NAMES[activeFilter]?.replace(' Warning', '').replace(' Advisory', '') || activeFilter})`}
            </div>
            {polygonAlerts.length > 0 && (
              <div style={{ color: 'var(--text-secondary)', fontSize: '0.75rem' }}>
                + {polygonAlerts.length} storm polygon{polygonAlerts.length !== 1 ? 's' : ''}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

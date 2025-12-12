import React, { useEffect, useMemo, useState } from 'react';
import { MapContainer, TileLayer, Polygon, Popup, useMap } from 'react-leaflet';
import L from 'leaflet';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';
import 'leaflet/dist/leaflet.css';

// Fix for default marker icons in Leaflet with webpack/vite
// We don't use markers but this prevents potential errors
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
}

// Component to handle map fitting to bounds when alerts change
const FitBounds: React.FC<{ alerts: Alert[] }> = ({ alerts }) => {
  const map = useMap();

  useEffect(() => {
    // Get all polygons and fit bounds to them
    const validAlerts = alerts.filter(a => a.polygon && a.polygon.length > 0);
    if (validAlerts.length === 0) return;

    const allPoints: L.LatLngExpression[] = [];
    validAlerts.forEach(alert => {
      if (alert.polygon) {
        alert.polygon.forEach(ring => {
          ring.forEach(coord => {
            // Backend sends [lat, lon] format (already converted from GeoJSON)
            allPoints.push([coord[0], coord[1]] as L.LatLngExpression);
          });
        });
      }
    });

    if (allPoints.length > 0) {
      const bounds = L.latLngBounds(allPoints);
      map.fitBounds(bounds, { padding: [50, 50] });
    }
  }, [alerts, map]);

  return null;
};

// Component to zoom to a selected alert
const ZoomToAlert: React.FC<{ alert: Alert | null }> = ({ alert }) => {
  const map = useMap();

  useEffect(() => {
    if (!alert?.polygon || alert.polygon.length === 0) return;

    const allPoints: L.LatLngExpression[] = [];
    alert.polygon.forEach(ring => {
      ring.forEach(coord => {
        allPoints.push([coord[0], coord[1]] as L.LatLngExpression);
      });
    });

    if (allPoints.length > 0) {
      const bounds = L.latLngBounds(allPoints);
      map.fitBounds(bounds, { padding: [100, 100], animate: true });
    }
  }, [alert, map]);

  return null;
};

export const AlertMap: React.FC<AlertMapProps> = ({
  alerts,
  onAlertClick,
  selectedAlert,
}) => {
  const [mapReady, setMapReady] = useState(false);

  // Default center: Ohio region
  const defaultCenter: L.LatLngExpression = [39.9612, -82.9988];
  const defaultZoom = 7;

  // Filter alerts that have polygon data
  const alertsWithPolygons = useMemo(() => {
    return alerts.filter(alert => alert.polygon && alert.polygon.length > 0);
  }, [alerts]);

  // Get color for an alert polygon
  const getPolygonColor = (alert: Alert) => {
    const style = getAlertStyle(alert.phenomenon);
    return style.backgroundColor;
  };

  // Format the time for popup
  const formatTime = (isoString: string | null) => {
    if (!isoString) return '';
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  };

  return (
    <div className="map-container" style={{ height: '100%', width: '100%' }}>
      <MapContainer
        center={defaultCenter}
        zoom={defaultZoom}
        style={{ height: '100%', width: '100%', borderRadius: 'var(--radius-md)' }}
        whenReady={() => setMapReady(true)}
      >
        {/* Dark tile layer for dark theme */}
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        />

        {/* Fit bounds to all alerts on load */}
        {mapReady && <FitBounds alerts={alertsWithPolygons} />}

        {/* Zoom to selected alert */}
        {mapReady && <ZoomToAlert alert={selectedAlert || null} />}

        {/* Render alert polygons - each polygon separately for proper rendering */}
        {alertsWithPolygons.map((alert) => {
          const color = getPolygonColor(alert);
          const isSelected = selectedAlert?.product_id === alert.product_id;

          // Render each polygon ring as a separate Polygon component
          // Backend sends array of polygons, each polygon is array of [lat, lon] coords
          return alert.polygon!.map((ring, ringIndex) => {
            const positions = ring.map(coord => [coord[0], coord[1]] as [number, number]);

            return (
              <Polygon
                key={`${alert.product_id}-${ringIndex}`}
                positions={positions}
                pathOptions={{
                  color: color,
                  fillColor: color,
                  fillOpacity: isSelected ? 0.5 : 0.3,
                  weight: isSelected ? 3 : 2,
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
                    {alert.headline && (
                      <p style={{ margin: '0 0 8px 0', fontSize: '0.9rem' }}>
                        {alert.headline}
                      </p>
                    )}
                    <p style={{ margin: '0', fontSize: '0.8rem', color: '#666' }}>
                      <strong>Issued:</strong> {formatTime(alert.issued_time)}<br />
                      <strong>Expires:</strong> {formatTime(alert.expiration_time)}<br />
                      <strong>Office:</strong> {alert.issuing_office || 'Unknown'}
                    </p>
                    {alert.threat.has_tornado && (
                      <p style={{
                        margin: '8px 0 0 0',
                        padding: '4px 8px',
                        backgroundColor: '#ff0000',
                        color: 'white',
                        borderRadius: '4px',
                        fontSize: '0.8rem',
                        fontWeight: 'bold',
                      }}>
                        TORNADO {alert.threat.tornado_observed ? 'OBSERVED' : 'POSSIBLE'}
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
                        color: getAlertStyle(alert.phenomenon).textColor,
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
            );
          });
        })}
      </MapContainer>

      {/* Map legend */}
      <div style={{
        position: 'absolute',
        bottom: '20px',
        right: '20px',
        backgroundColor: 'var(--bg-secondary)',
        padding: '8px 12px',
        borderRadius: 'var(--radius-sm)',
        border: '1px solid var(--border-color)',
        zIndex: 1000,
        fontSize: '0.75rem',
      }}>
        <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>
          Alerts: {alertsWithPolygons.length}
        </div>
        {alertsWithPolygons.length === 0 && (
          <div style={{ color: 'var(--text-secondary)' }}>
            No polygons available
          </div>
        )}
      </div>
    </div>
  );
};

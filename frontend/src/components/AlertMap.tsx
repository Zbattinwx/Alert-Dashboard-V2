import React, { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import { Map as MapGL, Marker, Popup, Source, Layer } from 'react-map-gl/maplibre';
import type { MapRef, MapLayerMouseEvent } from 'react-map-gl/maplibre';
import 'maplibre-gl/dist/maplibre-gl.css';

import type { Alert } from '../types/alert';
import { getAlertStyle, PHENOMENON_NAMES } from '../types/alert';
import type { ChaserPosition } from '../types/chaser';
import type {
  RadarBinaryFrame, StormCell, RadarProduct,
} from '../types/radar';
import {
  RADAR_PRODUCT_SHORT, THREAT_LEVEL_COLORS, THREAT_LEVEL_LABELS,
  SCORE_FACTOR_LABELS, SCORE_FACTOR_WEIGHTS,
} from '../types/radar';
import { parseRadarBinaryFrame } from '../utils/radarBinaryParser';
import { RadarGLLayer } from './RadarGLLayer';
import { apiUrl } from '../utils/api';

// Same dark base used by RadarSection.tsx
const MAP_STYLE = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';

const MIN_CELL_SCORE = 0; // show all cells when radar is on

interface AlertMapProps {
  alerts: Alert[];
  onAlertClick?: (alert: Alert) => void;
  selectedAlert?: Alert | null;
  chasers?: ChaserPosition[];
  radarFrame?: RadarBinaryFrame | null;
  stormCells?: StormCell[];
}

// API zone shape
interface ZoneData {
  zone_id: string;
  geometry: number[][][];        // outer arrays = polygons, inner = ring of [lat, lon]
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

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(iso?: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}

// Walk an arbitrarily-nested coordinate structure and flatten to [lat, lon] pairs.
function flattenCoords(data: unknown, out: [number, number][]): void {
  if (!Array.isArray(data) || data.length === 0) return;
  if (data.length === 2 && typeof data[0] === 'number' && typeof data[1] === 'number') {
    out.push([data[0] as number, data[1] as number]);
    return;
  }
  for (const item of data) if (Array.isArray(item)) flattenCoords(item, out);
}

// Convert NWS-style nested [lat, lon] polygons into a GeoJSON MultiPolygon coordinates array.
// A ring of [lat, lon] becomes a ring of [lon, lat].
function polygonToMultiCoords(polygon: unknown): number[][][][] {
  const result: number[][][][] = [];
  const isCoord = (a: unknown): a is [number, number] =>
    Array.isArray(a) && a.length === 2 && typeof a[0] === 'number' && typeof a[1] === 'number';
  const isRing = (a: unknown): a is number[][] =>
    Array.isArray(a) && a.length > 0 && isCoord(a[0]);
  const walk = (data: unknown) => {
    if (!Array.isArray(data) || data.length === 0) return;
    if (isRing(data)) {
      result.push([data.map(c => [c[1], c[0]])]);  // single-ring polygon
      return;
    }
    for (const item of data) if (Array.isArray(item)) walk(item);
  };
  walk(polygon);
  return result;
}

// Convert zone geometry ([lat, lon] rings) into GeoJSON MultiPolygon coords ([lon, lat]).
function zoneGeometryToMultiCoords(geometry: number[][][]): number[][][][] {
  return geometry.map(ring => [ring.map(c => [c[1], c[0]])]);
}

// ── Storm-cell helpers ───────────────────────────────────────────────────────

interface CellMarkerProps {
  cell: StormCell;
  selected: boolean;
  onClick: () => void;
}

function CellMarker({ cell, selected, onClick }: CellMarkerProps) {
  const color = THREAT_LEVEL_COLORS[cell.threat_level] || '#888';
  const size = Math.max(32, Math.min(48, 28 + cell.severity_score / 5));
  const trendArrow = cell.trend === 'strengthening' ? '▲' : cell.trend === 'weakening' ? '▼' : '';

  return (
    <div
      onClick={onClick}
      className={cell.rotation_detected ? 'cell-circle cell-pulse' : 'cell-circle'}
      style={{
        width: size, height: size, borderRadius: '50%',
        background: `radial-gradient(circle, ${color}cc, ${color}88)`,
        border: `2px solid ${selected ? '#fff' : color}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: '#fff', fontWeight: 'bold',
        fontSize: size > 38 ? 14 : 12,
        textShadow: '0 0 4px rgba(0,0,0,0.8)',
        boxShadow: selected ? `0 0 0 3px ${color}66, 0 0 8px ${color}aa` : `0 0 8px ${color}66`,
        cursor: 'pointer',
        position: 'relative',
      }}
    >
      {cell.severity_score}
      {trendArrow && <span className="cell-trend">{trendArrow}</span>}
      <div className="cell-indicators">
        {cell.rotation_detected && <span className="cell-indicator cell-rotation" title="Rotation">↻</span>}
        {cell.tvs_detected && <span className="cell-indicator cell-tvs" title="TVS">T</span>}
        {cell.hail_indicated && <span className="cell-indicator cell-hail" title="Hail">●</span>}
        {cell.debris_signature && <span className="cell-indicator cell-debris" title="Debris">⚠</span>}
      </div>
    </div>
  );
}

function CellPopupBody({ cell }: { cell: StormCell }) {
  const color = THREAT_LEVEL_COLORS[cell.threat_level];
  return (
    <div className="cell-popup">
      <div className="cell-popup-header" style={{ borderColor: color }}>
        <span className="cell-id">{cell.cell_id}</span>
        <span className="cell-score" style={{ color }}>
          {cell.severity_score} — {THREAT_LEVEL_LABELS[cell.threat_level]}
        </span>
      </div>
      <div className="cell-popup-stats">
        <div className="cell-stat"><span className="cell-stat-label">Max Reflectivity</span><span className="cell-stat-value">{cell.max_reflectivity_dbz.toFixed(1)} dBZ</span></div>
        <div className="cell-stat"><span className="cell-stat-label">Area</span><span className="cell-stat-value">{cell.area_km2.toFixed(1)} km²</span></div>
        <div className="cell-stat"><span className="cell-stat-label">Motion</span><span className="cell-stat-value">{cell.motion_direction_deg.toFixed(0)}° at {cell.motion_speed_kph.toFixed(0)} km/h</span></div>
        <div className="cell-stat"><span className="cell-stat-label">Trend</span><span className={`cell-stat-value trend-${cell.trend}`}>{cell.trend.charAt(0).toUpperCase() + cell.trend.slice(1)}</span></div>
      </div>
      <div className="cell-popup-flags">
        {cell.rotation_detected && <span className="cell-flag flag-rotation">Rotation {cell.rotation_velocity_ms ? `(${cell.rotation_velocity_ms} m/s)` : ''}</span>}
        {cell.tvs_detected && <span className="cell-flag flag-tvs">TVS</span>}
        {cell.hail_indicated && <span className="cell-flag flag-hail">Hail {cell.hail_max_dbz ? `(${cell.hail_max_dbz.toFixed(0)} dBZ)` : ''}</span>}
        {cell.debris_signature && <span className="cell-flag flag-debris">Debris</span>}
      </div>
      <div className="cell-popup-breakdown">
        <div className="breakdown-title">Score Breakdown</div>
        {Object.entries(SCORE_FACTOR_LABELS).map(([key, label]) => {
          const score = cell.score_breakdown[key] ?? 0;
          const weight = SCORE_FACTOR_WEIGHTS[key] ?? 0;
          const weighted = Math.round(score * weight / 100);
          return (
            <div key={key} className="breakdown-row">
              <span className="breakdown-label">{label}</span>
              <div className="breakdown-bar-bg">
                <div className="breakdown-bar" style={{ width: `${score}%`, background: score > 70 ? '#ff4444' : score > 40 ? '#ffaa00' : '#44aa44' }} />
              </div>
              <span className="breakdown-value">{weighted}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Chaser icon ──────────────────────────────────────────────────────────────

function ChaserIcon({ name, heading }: { name: string; heading: number | null }) {
  const rotation = heading ?? 0;
  return (
    <div style={{ position: 'relative', width: 32, height: 32 }}>
      <div style={{
        width: 14, height: 14,
        background: '#00CED1',
        border: '2px solid white',
        borderRadius: '50%',
        position: 'absolute', top: '50%', left: '50%',
        transform: 'translate(-50%, -50%)',
        boxShadow: '0 0 8px rgba(0,206,209,0.6)',
      }} />
      {heading !== null && (
        <div style={{
          position: 'absolute', top: 0, left: '50%',
          transform: `translateX(-50%) rotate(${rotation}deg)`,
          transformOrigin: 'center 16px',
          width: 0, height: 0,
          borderLeft: '4px solid transparent',
          borderRight: '4px solid transparent',
          borderBottom: '8px solid #00CED1',
        }} />
      )}
      <div className="chaser-marker-label">{name}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

type PopupTarget =
  | { kind: 'zone'; lng: number; lat: number; zone: ZoneData; alert: Alert | undefined }
  | { kind: 'alert'; lng: number; lat: number; alert: Alert }
  | { kind: 'chaser'; lng: number; lat: number; chaser: ChaserPosition }
  | { kind: 'cell'; lng: number; lat: number; cell: StormCell };

const INITIAL_VIEW = { longitude: -82.9988, latitude: 39.9612, zoom: 7 };

export const AlertMap: React.FC<AlertMapProps> = ({
  alerts,
  onAlertClick,
  selectedAlert,
  chasers = [],
  radarFrame = null,
  stormCells = [],
}) => {
  const mapRef = useRef<MapRef>(null);
  const radarLayerRef = useRef<RadarGLLayer | null>(null);

  const [mapLoaded, setMapLoaded] = useState(false);
  const [zoneData, setZoneData] = useState<MapZonesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeFilter, setActiveFilter] = useState<string | null>(null);
  const [radarEnabled, setRadarEnabled] = useState(false);
  const [radarProduct, setRadarProduct] = useState<RadarProduct>('reflectivity');
  const [radarFrameData, setRadarFrameData] = useState<RadarBinaryFrame | null>(radarFrame ?? null);
  const [usStates, setUsStates] = useState<GeoJSON.FeatureCollection | null>(null);
  const [popup, setPopup] = useState<PopupTarget | null>(null);

  // ── State outlines for context ─────────────────────────────────────────────
  useEffect(() => {
    fetch('/us-states.json')
      .then(r => r.json())
      .then(setUsStates)
      .catch(err => console.error('Failed to load US states', err));
  }, []);

  // ── Zone polling (live state of all active zone-based alerts) ──────────────
  const fetchZoneData = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/map/zones'));
      if (res.ok) setZoneData(await res.json());
    } catch (e) {
      console.error('Failed to fetch zone data', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchZoneData();
    const t = setInterval(fetchZoneData, 30_000);
    return () => clearInterval(t);
  }, [fetchZoneData]);

  // ── Fetch latest radar frame on product change or when radar is enabled ────
  useEffect(() => {
    if (!radarEnabled) return;
    let cancelled = false;
    (async () => {
      try {
        const metaRes = await fetch(apiUrl(`/api/radar/frame/${radarProduct}`));
        if (!metaRes.ok) return;
        const metaList = await metaRes.json();
        const meta = Array.isArray(metaList) ? metaList[0] : metaList;
        if (!meta?.frame_id || !meta?.site) return;
        const binRes = await fetch(apiUrl(`/api/radar/binary/${meta.site}/${radarProduct}/${meta.frame_id}`));
        if (!binRes.ok) return;
        const buf = await binRes.arrayBuffer();
        if (!cancelled) setRadarFrameData(parseRadarBinaryFrame(buf));
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [radarEnabled, radarProduct]);

  // ── Promote live WebSocket frames into local state when matching product ──
  useEffect(() => {
    if (radarFrame && radarFrame.product === radarProduct && radarEnabled) {
      setRadarFrameData(radarFrame);
    }
  }, [radarFrame, radarProduct, radarEnabled]);

  // ── Add/remove the WebGL radar custom layer when toggled ───────────────────
  useEffect(() => {
    if (!mapLoaded) return;
    const map = mapRef.current?.getMap();
    if (!map) return;

    // Find the first symbol (label) layer so the radar paints below city names
    const firstSymbolId = map.getStyle().layers.find(
      (l: { type: string }) => l.type === 'symbol'
    )?.id;

    const layerId = 'alert-map-radar';
    if (radarEnabled && !radarLayerRef.current) {
      const layer = new RadarGLLayer(layerId);
      layer.setOpacity(0.55);
      map.addLayer(layer as unknown as Parameters<typeof map.addLayer>[0], firstSymbolId);
      radarLayerRef.current = layer;
    } else if (!radarEnabled && radarLayerRef.current) {
      if (map.getLayer(layerId)) map.removeLayer(layerId);
      radarLayerRef.current = null;
    }
  }, [radarEnabled, mapLoaded]);

  // ── Push frame data into the radar layer ───────────────────────────────────
  useEffect(() => {
    const layer = radarLayerRef.current;
    const map = mapRef.current?.getMap();
    if (!layer) return;
    layer.setFrame(radarFrameData);
    if (map) map.triggerRepaint();
  }, [radarFrameData]);

  // ── Tear down radar layer on unmount ───────────────────────────────────────
  useEffect(() => {
    return () => {
      const map = mapRef.current?.getMap();
      if (radarLayerRef.current && map?.getLayer(radarLayerRef.current.id)) {
        map.removeLayer(radarLayerRef.current.id);
      }
      radarLayerRef.current = null;
    };
  }, []);

  // ── Filter polygon alerts and zones ────────────────────────────────────────
  // Any active warning/advisory with a parsed LAT...LON polygon renders as a
  // polygon overlay (TOR, SVR, FFW, FL.W with polygon, SPS, etc.). Watches
  // (significance A) are excluded — their polygons are convective outline
  // boxes, not precise warning areas, so they render as zone fills instead.
  const polygonAlerts = useMemo(() => {
    return alerts.filter(a =>
      a.polygon &&
      a.polygon.length > 0 &&
      a.significance !== 'A'
    );
  }, [alerts]);

  const filteredZones = useMemo(() => {
    if (!zoneData?.zones) return [];
    if (!activeFilter) return zoneData.zones;
    return zoneData.zones.filter(z => z.alert.phenomenon === activeFilter);
  }, [zoneData, activeFilter]);

  // ── GeoJSON sources ────────────────────────────────────────────────────────
  const zoneGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => ({
    type: 'FeatureCollection',
    features: filteredZones.map(z => {
      const style = getAlertStyle(z.alert.phenomenon, z.alert.significance);
      const isSelected = selectedAlert?.product_id === z.alert.product_id;
      return {
        type: 'Feature',
        geometry: { type: 'MultiPolygon', coordinates: zoneGeometryToMultiCoords(z.geometry) },
        properties: {
          zone_id: z.zone_id,
          product_id: z.alert.product_id,
          color: style.backgroundColor,
          selected: isSelected,
        },
      } as GeoJSON.Feature;
    }),
  }), [filteredZones, selectedAlert?.product_id]);

  const polygonGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => {
    const visible = polygonAlerts.filter(a => !activeFilter || a.phenomenon === activeFilter);
    return {
      type: 'FeatureCollection',
      features: visible.map(a => {
        const style = getAlertStyle(a.phenomenon, a.significance);
        const isSelected = selectedAlert?.product_id === a.product_id;
        return {
          type: 'Feature',
          geometry: { type: 'MultiPolygon', coordinates: polygonToMultiCoords(a.polygon) },
          properties: {
            product_id: a.product_id,
            color: style.backgroundColor,
            selected: isSelected,
          },
        } as GeoJSON.Feature;
      }),
    };
  }, [polygonAlerts, activeFilter, selectedAlert?.product_id]);

  // Cell track histories + forecasts as line features
  const cellTracksGeoJSON = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!radarEnabled) return { type: 'FeatureCollection', features: [] };
    const features: GeoJSON.Feature[] = [];
    for (const cell of stormCells) {
      const color = THREAT_LEVEL_COLORS[cell.threat_level] || '#888';
      if (cell.track_history.length > 1) {
        features.push({
          type: 'Feature',
          geometry: {
            type: 'LineString',
            coordinates: cell.track_history.map(p => [p.lon, p.lat]),
          },
          properties: { kind: 'history', color },
        });
      }
      if (cell.forecast_track.length > 0) {
        features.push({
          type: 'Feature',
          geometry: {
            type: 'LineString',
            coordinates: [[cell.lon, cell.lat], ...cell.forecast_track.map(p => [p.lon, p.lat])],
          },
          properties: { kind: 'forecast', color },
        });
      }
    }
    return { type: 'FeatureCollection', features };
  }, [stormCells, radarEnabled]);

  const mapCells = useMemo(() => {
    if (!radarEnabled) return [];
    return stormCells.filter(c => c.severity_score >= MIN_CELL_SCORE);
  }, [stormCells, radarEnabled]);

  // ── Fit bounds on first data load + when filter changes ────────────────────
  const lastFitKeyRef = useRef<string>('');
  useEffect(() => {
    if (!mapLoaded) return;
    const map = mapRef.current?.getMap();
    if (!map) return;

    const points: [number, number][] = [];
    for (const z of filteredZones) {
      for (const ring of z.geometry) {
        for (const c of ring) points.push([c[1], c[0]]); // [lon, lat]
      }
    }
    for (const a of polygonAlerts) {
      const flat: [number, number][] = [];
      flattenCoords(a.polygon, flat);
      for (const c of flat) points.push([c[1], c[0]]);   // [lon, lat]
    }

    if (points.length === 0) return;
    // Avoid re-fitting on every render — only when the data shape changes
    const key = `${filteredZones.length}:${polygonAlerts.length}:${activeFilter ?? ''}`;
    if (key === lastFitKeyRef.current) return;
    lastFitKeyRef.current = key;

    let minLng = points[0][0], maxLng = points[0][0], minLat = points[0][1], maxLat = points[0][1];
    for (const [lng, lat] of points) {
      if (lng < minLng) minLng = lng; if (lng > maxLng) maxLng = lng;
      if (lat < minLat) minLat = lat; if (lat > maxLat) maxLat = lat;
    }
    map.fitBounds([[minLng, minLat], [maxLng, maxLat]], { padding: 50, duration: 600 });
  }, [filteredZones, polygonAlerts, activeFilter, mapLoaded]);

  // ── Fly to selected alert when it changes ─────────────────────────────────
  useEffect(() => {
    if (!mapLoaded || !selectedAlert?.polygon) return;
    const map = mapRef.current?.getMap();
    if (!map) return;
    const flat: [number, number][] = [];
    flattenCoords(selectedAlert.polygon, flat);
    if (flat.length === 0) return;
    let minLng = flat[0][1], maxLng = flat[0][1], minLat = flat[0][0], maxLat = flat[0][0];
    for (const [lat, lng] of flat) {
      if (lng < minLng) minLng = lng; if (lng > maxLng) maxLng = lng;
      if (lat < minLat) minLat = lat; if (lat > maxLat) maxLat = lat;
    }
    map.fitBounds([[minLng, minLat], [maxLng, maxLat]], { padding: 100, duration: 800 });
  }, [selectedAlert, mapLoaded]);

  // ── Click handler: hit-test zone/polygon layers ────────────────────────────
  const handleMapClick = useCallback((e: MapLayerMouseEvent) => {
    const features = e.features ?? [];
    // Polygon alerts (TOR/SVR/FFW) take priority since they render on top
    const polyHit = features.find(f => f.layer?.id === 'alert-fill');
    if (polyHit) {
      const id = polyHit.properties?.product_id;
      const alert = polygonAlerts.find(a => a.product_id === id);
      if (alert) {
        setPopup({ kind: 'alert', lng: e.lngLat.lng, lat: e.lngLat.lat, alert });
        onAlertClick?.(alert);
      }
      return;
    }
    const zoneHit = features.find(f => f.layer?.id === 'zone-fill');
    if (zoneHit) {
      const productId = zoneHit.properties?.product_id;
      const zone = filteredZones.find(z => z.alert.product_id === productId);
      const alert = alerts.find(a => a.product_id === productId);
      if (zone) {
        setPopup({ kind: 'zone', lng: e.lngLat.lng, lat: e.lngLat.lat, zone, alert });
        if (alert) onAlertClick?.(alert);
      }
      return;
    }
  }, [polygonAlerts, filteredZones, alerts, onAlertClick]);

  const toggleFilter = (phenomenon: string) =>
    setActiveFilter(prev => prev === phenomenon ? null : phenomenon);

  // ─────────────────────────────────────────────────────────────────────────

  return (
    <div className="map-container" style={{ height: '100%', width: '100%', position: 'relative' }}>
      <MapGL
        ref={mapRef}
        initialViewState={INITIAL_VIEW}
        style={{ width: '100%', height: '100%', borderRadius: 'var(--radius-md)' }}
        mapStyle={MAP_STYLE}
        onLoad={() => setMapLoaded(true)}
        onClick={handleMapClick}
        interactiveLayerIds={['zone-fill', 'alert-fill']}
        attributionControl={false}
      >
        {/* US state outlines */}
        {usStates && (
          <Source id="us-states" type="geojson" data={usStates}>
            <Layer
              id="us-states-line"
              type="line"
              paint={{ 'line-color': 'rgba(255, 255, 255, 0.4)', 'line-width': 1.5 }}
            />
          </Source>
        )}

        {/* Zone fills (watches + significance != A renders as zone) */}
        <Source id="zones" type="geojson" data={zoneGeoJSON}>
          <Layer
            id="zone-fill"
            type="fill"
            paint={{
              'fill-color': ['get', 'color'],
              'fill-opacity': ['case', ['get', 'selected'], 0.5, 0.3],
            }}
          />
          <Layer
            id="zone-outline"
            type="line"
            paint={{
              'line-color': ['get', 'color'],
              'line-width': ['case', ['get', 'selected'], 2, 1],
              'line-opacity': 0.9,
            }}
          />
        </Source>

        {/* Polygon alerts on top of zones (TOR, SVR, FFW, etc.) */}
        <Source id="polygon-alerts" type="geojson" data={polygonGeoJSON}>
          <Layer
            id="alert-fill"
            type="fill"
            paint={{
              'fill-color': ['get', 'color'],
              'fill-opacity': ['case', ['get', 'selected'], 0.55, 0.4],
            }}
          />
          <Layer
            id="alert-outline"
            type="line"
            paint={{
              'line-color': '#ffffff',
              'line-width': ['case', ['get', 'selected'], 4, 3],
              'line-opacity': ['case', ['get', 'selected'], 1.0, 0.8],
            }}
          />
        </Source>

        {/* Storm cell tracks (history + forecast) */}
        {radarEnabled && (
          <Source id="cell-tracks" type="geojson" data={cellTracksGeoJSON}>
            <Layer
              id="cell-history-line"
              type="line"
              filter={['==', ['get', 'kind'], 'history']}
              paint={{
                'line-color': ['get', 'color'],
                'line-width': 2,
                'line-opacity': 0.5,
                'line-dasharray': [6, 4],
              }}
            />
            <Layer
              id="cell-forecast-line"
              type="line"
              filter={['==', ['get', 'kind'], 'forecast']}
              paint={{
                'line-color': ['get', 'color'],
                'line-width': 2,
                'line-opacity': 0.4,
                'line-dasharray': [3, 6],
              }}
            />
          </Source>
        )}

        {/* Chaser markers */}
        {chasers.map(c => (
          <Marker
            key={`chaser-${c.client_id}`}
            longitude={c.lon}
            latitude={c.lat}
            anchor="center"
            onClick={(e) => {
              e.originalEvent.stopPropagation();
              setPopup({ kind: 'chaser', lng: c.lon, lat: c.lat, chaser: c });
            }}
          >
            <ChaserIcon name={c.name} heading={c.heading} />
          </Marker>
        ))}

        {/* Storm-cell markers (only when radar overlay is on) */}
        {mapCells.map(cell => (
          <Marker
            key={`cell-${cell.cell_id}`}
            longitude={cell.lon}
            latitude={cell.lat}
            anchor="center"
            onClick={(e) => {
              e.originalEvent.stopPropagation();
              setPopup({ kind: 'cell', lng: cell.lon, lat: cell.lat, cell });
            }}
          >
            <CellMarker cell={cell} selected={popup?.kind === 'cell' && popup.cell.cell_id === cell.cell_id} onClick={() => {}} />
          </Marker>
        ))}

        {/* Popups */}
        {popup && (
          <Popup
            longitude={popup.lng}
            latitude={popup.lat}
            anchor="bottom"
            offset={popup.kind === 'cell' || popup.kind === 'chaser' ? 20 : 0}
            onClose={() => setPopup(null)}
            closeOnClick={false}
            maxWidth="340px"
            className={popup.kind === 'cell' ? 'cell-popup-container' : undefined}
          >
            {popup.kind === 'zone' && (() => {
              const color = getAlertStyle(popup.zone.alert.phenomenon, popup.zone.alert.significance).backgroundColor;
              return (
                <div style={{ minWidth: 200 }}>
                  <h4 style={{ margin: '0 0 8px 0', color }}>{popup.zone.alert.event_name}</h4>
                  <p style={{ margin: '0 0 4px 0', fontSize: '0.8rem', color: 'var(--text-muted)' }}>{popup.zone.zone_id}</p>
                  {popup.zone.alert.display_locations && (
                    <p style={{ margin: '0 0 8px 0', fontSize: '0.85rem' }}>{popup.zone.alert.display_locations}</p>
                  )}
                  <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    <strong>Expires:</strong> {formatTime(popup.zone.alert.expiration_time)}<br />
                    <strong>Office:</strong> {popup.zone.alert.sender_office || 'Unknown'}
                  </p>
                  {popup.alert && (
                    <button
                      onClick={(e) => { e.stopPropagation(); onAlertClick?.(popup.alert!); }}
                      style={{
                        marginTop: 8, padding: '4px 12px',
                        background: color,
                        color: getAlertStyle(popup.zone.alert.phenomenon, popup.zone.alert.significance).textColor,
                        border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: '0.8rem',
                      }}
                    >View Details</button>
                  )}
                </div>
              );
            })()}

            {popup.kind === 'alert' && (() => {
              const a = popup.alert;
              const color = getAlertStyle(a.phenomenon, a.significance).backgroundColor;
              return (
                <div style={{ minWidth: 200 }}>
                  <h4 style={{ margin: '0 0 8px 0', color }}>{a.event_name}</h4>
                  <p style={{ margin: '0 0 8px 0', fontSize: '0.85rem' }}>
                    {a.display_locations || a.affected_areas.join(', ')}
                  </p>
                  <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    <strong>Expires:</strong> {formatTime(a.expiration_time)}<br />
                    <strong>Office:</strong> {a.sender_name || a.sender_office || 'Unknown'}
                  </p>
                  {a.threat.tornado_detection && (
                    <p style={{ margin: '8px 0 0 0', padding: '4px 8px', background: '#ff0000', color: 'white', borderRadius: 4, fontSize: '0.8rem', fontWeight: 'bold' }}>
                      TORNADO {a.threat.tornado_detection}
                    </p>
                  )}
                  {a.threat.max_wind_gust_mph && (
                    <p style={{ margin: '8px 0 0 0', fontSize: '0.8rem' }}>
                      <strong>Wind:</strong> {a.threat.max_wind_gust_mph} mph
                      {a.threat.max_hail_size_inches && <> | <strong>Hail:</strong> {a.threat.max_hail_size_inches}"</>}
                    </p>
                  )}
                  <button
                    onClick={(e) => { e.stopPropagation(); onAlertClick?.(a); }}
                    style={{
                      marginTop: 8, padding: '4px 12px', background: color,
                      color: getAlertStyle(a.phenomenon, a.significance).textColor,
                      border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: '0.8rem',
                    }}
                  >View Details</button>
                </div>
              );
            })()}

            {popup.kind === 'chaser' && (
              <div style={{ minWidth: 140 }}>
                <h4 style={{ margin: '0 0 4px 0', color: '#00CED1' }}>{popup.chaser.name}</h4>
                {popup.chaser.speed !== null && (
                  <p style={{ margin: '0 0 2px 0', fontSize: '0.8rem' }}>
                    <strong>Speed:</strong> {Math.round(popup.chaser.speed)} mph
                  </p>
                )}
                <p style={{ margin: 0, fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  Last update: {new Date(popup.chaser.last_update).toLocaleTimeString()}
                </p>
              </div>
            )}

            {popup.kind === 'cell' && <CellPopupBody cell={popup.cell} />}
          </Popup>
        )}
      </MapGL>

      {/* Radar toggle controls */}
      <div className="radar-toggle-controls">
        <button
          className={`radar-toggle-btn ${radarEnabled ? 'active' : ''}`}
          onClick={() => setRadarEnabled(v => !v)}
          title="Toggle radar overlay"
        >
          <i className="fa fa-satellite-dish" /> Radar
        </button>
        {radarEnabled && (
          <div className="radar-mini-product-btns">
            {(['reflectivity', 'velocity', 'cross_correlation_ratio'] as RadarProduct[]).map((p) => (
              <button
                key={p}
                className={`radar-mini-product-btn ${radarProduct === p ? 'active' : ''}`}
                onClick={() => setRadarProduct(p)}
              >
                {RADAR_PRODUCT_SHORT[p]}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Filter controls */}
      {zoneData && zoneData.alert_types.length > 0 && (
        <div style={{
          position: 'absolute', top: 10, left: 50, right: 50,
          backgroundColor: 'var(--bg-secondary)',
          padding: '10px 12px', borderRadius: 'var(--radius-md)',
          border: '1px solid var(--border-color)',
          zIndex: 1000, display: 'flex', flexWrap: 'wrap', gap: 6, justifyContent: 'center',
        }}>
          <button
            onClick={() => setActiveFilter(null)}
            style={{
              padding: '6px 12px',
              backgroundColor: !activeFilter ? 'var(--primary-color)' : 'var(--bg-tertiary)',
              color: !activeFilter ? 'white' : 'var(--text-secondary)',
              border: '1px solid var(--border-color)',
              borderRadius: 6, cursor: 'pointer',
              fontSize: '0.8rem', fontWeight: 500,
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
                  borderRadius: 6, cursor: 'pointer',
                  fontSize: '0.8rem', fontWeight: 500,
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
        position: 'absolute', bottom: 20, left: 10,
        backgroundColor: 'var(--bg-secondary)',
        padding: '10px 14px', borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-color)',
        zIndex: 1000, fontSize: '0.8rem',
      }}>
        {loading ? (
          <div style={{ color: 'var(--text-secondary)' }}>Loading zones...</div>
        ) : (
          <>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
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

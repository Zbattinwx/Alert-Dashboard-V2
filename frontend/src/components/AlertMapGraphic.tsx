import React, { useRef, useEffect, useImperativeHandle, forwardRef, useState } from 'react';
import { MapContainer, TileLayer, Polygon, Polyline, ImageOverlay, GeoJSON, useMap } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import type { Alert } from '../types/alert';
import type { RadarFrame } from '../types/radar';
import { getAlertStyle } from '../types/alert';

// ─── Contrast helper ─────────────────────────────────────────────────────────
// Calculates readable text color (black or white) from any background hex.
// Overrides the manually-set textColor as a safety net.

function readableTextColor(bgHex: string): string {
  const hex = bgHex.replace('#', '');
  if (hex.length < 6) return '#ffffff';
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  // Perceived luminance (WCAG formula)
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.55 ? '#000000' : '#ffffff';
}

function textShadow(textColor: string): string {
  // Contrast shadow so text pops on any background
  return textColor === '#000000'
    ? '0 1px 2px rgba(255,255,255,0.4)'
    : '0 1px 3px rgba(0,0,0,0.6)';
}

// ─── Direction helpers ────────────────────────────────────────────────────────
// direction_from is the meteorological FROM direction (where storm came from).
// The storm is moving TOWARD the opposite direction.

const OPPOSITE_CARDINAL: Record<string, string> = {
  N: 'S', NNE: 'SSW', NE: 'SW', ENE: 'WSW',
  E: 'W', ESE: 'WNW', SE: 'NW', SSE: 'NNW',
  S: 'N', SSW: 'NNE', SW: 'NE', WSW: 'ENE',
  W: 'E', WNW: 'ESE', NW: 'SE', NNW: 'SSE',
};

const CARDINAL_TO_DEG: Record<string, number> = {
  N: 0, NNE: 22.5, NE: 45, ENE: 67.5,
  E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
  S: 180, SSW: 202.5, SW: 225, WSW: 247.5,
  W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
};

/** Returns the cardinal direction the storm is moving TOWARD */
function towardDirection(dirFrom: string): string {
  return OPPOSITE_CARDINAL[dirFrom.toUpperCase()] ?? dirFrom;
}

/** Returns the bearing (degrees) the storm is moving TOWARD */
function towardBearing(dirFrom: string): number | null {
  const toward = towardDirection(dirFrom);
  return CARDINAL_TO_DEG[toward] ?? null;
}

// ─── Geo helpers ─────────────────────────────────────────────────────────────

function destinationPoint(
  lat: number, lon: number, bearingDeg: number, distanceKm: number,
): [number, number] {
  const R = 6371;
  const d = distanceKm / R;
  const φ1 = lat * Math.PI / 180;
  const λ1 = lon * Math.PI / 180;
  const θ = bearingDeg * Math.PI / 180;
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ));
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(d) * Math.cos(φ1), Math.cos(d) - Math.sin(φ1) * Math.sin(φ2));
  return [φ2 * 180 / Math.PI, λ2 * 180 / Math.PI];
}

function flattenPolygon(poly: any[]): [number, number][] {
  if (poly.length > 0 && Array.isArray(poly[0][0])) {
    return poly.flat(1) as [number, number][];
  }
  return poly as [number, number][];
}

function polygonBounds(poly: any[]): [[number, number], [number, number]] {
  const flatPoly = flattenPolygon(poly);
  const lats = flatPoly.map(p => p[0]);
  const lons = flatPoly.map(p => p[1]);
  return [
    [Math.min(...lats), Math.min(...lons)],
    [Math.max(...lats), Math.max(...lons)],
  ];
}

/**
 * Extend the polygon bounds symmetrically to show geographic context.
 * Adds a buffer proportional to polygon size (min 0.25°, max 0.7°) on all sides
 * so the polygon is always centered and fully visible with surrounding context.
 */
function extendedBounds(
  poly: any[],
  _motionBearingDeg: number | null,
): [[number, number], [number, number]] {
  const flatPoly = flattenPolygon(poly);
  const lats = flatPoly.map(p => p[0]);
  const lons = flatPoly.map(p => p[1]);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);

  const latSpan = maxLat - minLat;
  const lonSpan = maxLon - minLon;
  // Buffer: 40% of polygon span, clamped between 0.25° and 0.7°
  const latBuf = Math.min(0.7, Math.max(0.25, latSpan * 0.4));
  const lonBuf = Math.min(0.7, Math.max(0.25, lonSpan * 0.4));

  return [
    [minLat - latBuf, minLon - lonBuf],
    [maxLat + latBuf, maxLon + lonBuf],
  ];
}

function polygonCentroid(poly: any[]): [number, number] {
  const flatPoly = flattenPolygon(poly);
  const lats = flatPoly.map(p => p[0]);
  const lons = flatPoly.map(p => p[1]);
  return [
    (Math.min(...lats) + Math.max(...lats)) / 2,
    (Math.min(...lons) + Math.max(...lons)) / 2,
  ];
}

// ─── RainViewer radar layer ───────────────────────────────────────────────────
// Fetches the latest radar timestamp from RainViewer API (~2-5 min latency).
// Falls back to IEM NEXRAD composite if the fetch fails.

function RainViewerLayer() {
  // Start with IEM as the initial URL; swap to RainViewer once the API responds.
  // This way tiles start loading immediately and the capture always gets radar.
  const IEM_URL = 'https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png';
  const [tileUrl, setTileUrl] = useState<string>(IEM_URL);
  const [opacity, setOpacity] = useState(0.7);

  useEffect(() => {
    fetch('https://api.rainviewer.com/public/weather-maps.json')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        const past = data?.radar?.past;
        const host = data?.host ?? 'https://tilecache.rainviewer.com';
        if (past && past.length > 0) {
          const latest = past[past.length - 1];
          // Use the path field directly — it already contains the full tile URL template
          // including the correct color scheme and format supported by this radar frame.
          setTileUrl(`${host}${latest.path}`);
          setOpacity(0.75);
        }
      })
      .catch(() => {}); // keep IEM as fallback on error
  }, []);

  return (
    <TileLayer
      key={tileUrl} // force remount when URL changes so tiles refresh
      url={tileUrl}
      opacity={opacity}
      zIndex={2}
      crossOrigin="anonymous"
    />
  );
}

// ─── Zoom calculator ─────────────────────────────────────────────────────────
// Compute the Leaflet zoom level that makes `bounds` fill the 1024×MAP_H
// container. Uses known pixel dimensions so it never depends on Leaflet
// measuring an off-screen container (which always returns the wrong size).

const GRAPHIC_W = 1024;
const GRAPHIC_MAP_H = 440; // H(640) - HEADER(52) - THREATS(56) - FOOTER(92)

function computeZoom(bounds: [[number, number], [number, number]]): number {
  const latMid = (bounds[0][0] + bounds[1][0]) / 2;
  const cosLat = Math.cos(latMid * Math.PI / 180);
  const latSpanM = Math.abs(bounds[1][0] - bounds[0][0]) * 111_000;
  const lonSpanM = Math.abs(bounds[1][1] - bounds[0][1]) * 111_000 * cosLat;
  // Bind by whichever axis is tighter relative to the container
  const mPerPx = Math.max(latSpanM / GRAPHIC_MAP_H, lonSpanM / GRAPHIC_W);
  // Leaflet: mPerPx at zoom z = 156543 * cos(lat) / 2^z  → solve for z
  const zoom = Math.log2(156_543.03 * cosLat / mPerPx);
  return Math.floor(zoom);
}

// ─── Set view on mount ───────────────────────────────────────────────────────
// Uses setView(center, computedZoom) instead of fitBounds so it works
// even when Leaflet reports container size as 0 (off-screen rendering).

function SetView({ bounds }: { bounds: [[number, number], [number, number]] }) {
  const map = useMap();
  useEffect(() => {
    const center: [number, number] = [
      (bounds[0][0] + bounds[1][0]) / 2,
      (bounds[0][1] + bounds[1][1]) / 2,
    ];
    const zoom = computeZoom(bounds);

    const apply = () => map.setView(center, zoom, { animate: false });
    apply();
    const t1 = setTimeout(apply, 500);
    const t2 = setTimeout(apply, 2000);

    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

const CreateLabelsPane = () => {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane('labelsPane')) {
      const pane = map.createPane('labelsPane');
      pane.style.zIndex = '600';
      pane.style.pointerEvents = 'none';
    }
  }, [map]);
  return null;
};

// ─── County name formatting ───────────────────────────────────────────────────
// display_locations: "Crawford County, OH; Marion County, OH; ..."
// Output badge labels: "CRAWFORD, OH", "MARION, OH", ...

function parseCountyBadges(displayLocations: string): string[] {
  if (!displayLocations) return [];
  return displayLocations
    .split(';')
    .map(s => s.trim().replace(/\s+County\s*,/i, ','))
    .filter(Boolean);
}

// ─── Dimensions ──────────────────────────────────────────────────────────────

const W = 1024;
const H = 640;
const HEADER_H = 52;
const THREATS_H = 56;
const FOOTER_H = 92;
const MAP_H = H - HEADER_H - THREATS_H - FOOTER_H;

// ─── Styles ──────────────────────────────────────────────────────────────────

function pillStyle(bgDark: string, bgLight: string): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    padding: '5px 14px',
    borderRadius: '4px',
    backgroundColor: bgDark,
    border: `1px solid ${bgLight}`,
    color: '#ffffff',
    fontSize: '13px',
    fontWeight: 600,
    letterSpacing: '0.4px',
    whiteSpace: 'nowrap',
    gap: '6px',
  };
}

function countyBadge(borderColor: string): React.CSSProperties {
  return {
    padding: '4px 14px',
    borderRadius: '4px',
    backgroundColor: 'rgba(255,255,255,0.07)',
    border: `1px solid ${borderColor}55`,
    color: '#e2e8f0',
    fontSize: '12px',
    fontWeight: 600,
    letterSpacing: '0.4px',
  };
}

// ─── Capture ─────────────────────────────────────────────────────────────────

async function captureElement(el: HTMLDivElement): Promise<string | null> {
  try {
    const html2canvas = (await import('html2canvas')).default;
    const canvas = await html2canvas(el, {
      width: W,
      height: H,
      scale: 1,
      useCORS: true,
      allowTaint: true,
      logging: false,
      backgroundColor: '#1a1d2e',
    });
    return canvas.toDataURL('image/png');
  } catch (err) {
    console.error('AlertMapGraphic capture failed:', err);
    return null;
  }
}

// ─── Component ───────────────────────────────────────────────────────────────

export interface AlertMapGraphicHandle {
  capture: () => Promise<string | null>;
}

interface Props {
  alert: Alert;
  radarFrame?: RadarFrame | null;
  onCapture?: (dataUrl: string, productId: string) => void;
}

export const AlertMapGraphic = forwardRef<AlertMapGraphicHandle, Props>(
  ({ alert, radarFrame, onCapture }, ref) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const [usStates, setUsStates] = useState<any>(null);

    useEffect(() => {
      fetch('/us-states.json')
        .then(res => res.json())
        .then(data => setUsStates(data))
        .catch(() => {});
    }, []);

    // Tornado Emergency is the most severe warning the NWS issues — the header
    // turns crimson-magenta and leads with the words so the graphic is
    // unmistakable on air. Prefer the structured flag; fall back to the headline.
    const isTornadoEmergency =
      alert.threat.tornado_emergency === true ||
      (alert.headline || '').toUpperCase().includes('TORNADO EMERGENCY');

    const style = getAlertStyle(alert.phenomenon, alert.significance);
    const headerBg = isTornadoEmergency ? '#d6008c' : style.backgroundColor;
    const headerTextColor = readableTextColor(headerBg);
    const headerTextShadow = textShadow(headerTextColor);

    // Threat data
    const wind = alert.threat.max_wind_gust_mph;
    const windLabel = alert.threat.wind_damage_threat;
    const hail = alert.threat.max_hail_size_inches;
    const hailLabel = alert.threat.hail_damage_threat;
    const motion = alert.threat.storm_motion;

    // Motion: direction_from is WHERE THE STORM CAME FROM.
    // Display and arrow both show WHERE IT'S GOING (the opposite).
    const dirFrom = motion?.direction_from ?? null;
    const motionToLabel = dirFrom ? towardDirection(dirFrom) : null;
    const motionBearing = dirFrom ? towardBearing(dirFrom) : null;

    // Arrow: drawn along the TO bearing, shaft centered on the polygon centroid
    const polygon = alert.polygon;
    const centroid = polygon ? polygonCentroid(polygon) : null;
    const bounds = polygon ? polygonBounds(polygon) : null;
    const viewBounds = polygon ? extendedBounds(polygon, motionBearing) : null;

    // Diagnostic: log all polygon vertices so we can verify coordinates match the warning area.
    // Compare these to the NWS product at https://alerts.weather.gov to confirm correct parsing.
    if (polygon && centroid) {
      const flatPoly = flattenPolygon(polygon);
      console.group(`[AlertMapGraphic] ${alert.event_name} — ${alert.display_locations || alert.product_id}`);
      console.log(`centroid: ${centroid[0].toFixed(4)}°N, ${centroid[1].toFixed(4)}°W`);
      console.log(`bounds: SW [${bounds![0][0].toFixed(4)}, ${bounds![0][1].toFixed(4)}]  NE [${bounds![1][0].toFixed(4)}, ${bounds![1][1].toFixed(4)}]`);
      console.log(`viewBounds: SW [${viewBounds![0][0].toFixed(4)}, ${viewBounds![0][1].toFixed(4)}]  NE [${viewBounds![1][0].toFixed(4)}, ${viewBounds![1][1].toFixed(4)}]`);
      console.log(`polygon vertices (${flatPoly.length}):`);
      flatPoly.forEach((v, i) => console.log(`  [${i}] lat=${v[0].toFixed(4)}, lon=${v[1].toFixed(4)}`));
      console.groupEnd();
    } else {
      console.warn(`[AlertMapGraphic] ${alert.event_name} — NO POLYGON DATA`, alert.product_id);
    }
    let arrowLines: [number, number][][] = [];

    if (centroid && bounds && motionBearing !== null) {
      const deg = motionBearing; // storm is moving TOWARD this bearing
      // Use the smaller polygon dimension so the arrow stays proportional
      // regardless of whether the polygon is wide or tall.
      const latSpanKm = Math.abs(bounds[1][0] - bounds[0][0]) * 111;
      const lonSpanKm = Math.abs(bounds[1][1] - bounds[0][1]) * 85; // ~85 km/° lon at 40°N
      const polygonKm = Math.min(latSpanKm, lonSpanKm);
      // Clamp shaft: at least 12 km, at most 22 km
      const shaft = Math.min(Math.max(polygonKm * 0.45, 12), 22);
      const wing = shaft * 0.38;
      // Shaft: start behind centroid (FROM side), end ahead (TO side)
      const start = destinationPoint(centroid[0], centroid[1], deg + 180, shaft * 0.35);
      const end   = destinationPoint(centroid[0], centroid[1], deg,       shaft * 0.65);
      arrowLines = [
        [start, end],
        [end, destinationPoint(end[0], end[1], deg - 150, wing)],
        [end, destinationPoint(end[0], end[1], deg + 150, wing)],
      ];
    }

    // County badges from display_locations
    const countyBadges = parseCountyBadges(alert.display_locations || '');
    const displayList = countyBadges.length > 0 ? countyBadges : alert.affected_areas;

    // Expiration
    const formatExpires = (iso: string | null) => {
      if (!iso) return '';
      const d = new Date(iso);
      return d.toLocaleString('en-US', {
        hour: 'numeric', minute: '2-digit', hour12: true,
        month: '2-digit', day: '2-digit', year: 'numeric',
      });
    };

    useImperativeHandle(ref, () => ({
      capture: () => containerRef.current ? captureElement(containerRef.current) : Promise.resolve(null),
    }));

    // Auto-capture after tile load delay
    useEffect(() => {
      if (!onCapture) return;
      const id = setTimeout(async () => {
        if (!containerRef.current) return;
        const dataUrl = await captureElement(containerRef.current);
        if (dataUrl) onCapture(dataUrl, alert.product_id);
      }, 14000); // 14s: RainViewer API fetch + 4s SetView retries + tile load
      return () => clearTimeout(id);
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const hasThreat = wind || hail || (motion?.speed_mph && motionToLabel);

    return (
      <div
        ref={containerRef}
        style={{
          width: `${W}px`,
          height: `${H}px`,
          overflow: 'hidden',
          position: 'relative',
          fontFamily: "'Inter', 'Roboto', 'Segoe UI', sans-serif",
          backgroundColor: '#1a1d2e',
          flexShrink: 0,
        }}
      >
        {/* ── Header bar ── */}
        <div style={{
          height: `${HEADER_H}px`,
          backgroundColor: headerBg,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '0 24px',
        }}>
          <span style={{
            color: headerTextColor,
            textShadow: headerTextShadow,
            fontSize: '19px',
            fontWeight: isTornadoEmergency ? 800 : 700,
            letterSpacing: '1px',
            textTransform: 'uppercase',
          }}>
            {isTornadoEmergency ? '⚠ TORNADO EMERGENCY — ' : ''}
            {alert.event_name.toUpperCase()}
            {alert.expiration_time
              ? ` — EXPIRES: ${formatExpires(alert.expiration_time)}`
              : ''}
          </span>
        </div>

        {/* ── Threat pills ── */}
        <div style={{
          height: `${THREATS_H}px`,
          backgroundColor: '#0d1117',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '10px',
          padding: '0 20px',
        }}>
          {wind && (
            <div style={pillStyle('#0f3d22', '#1a5c33')}>
              <i className="fas fa-wind" style={{ fontSize: 13 }}></i>
              WIND: {wind} MPH{windLabel ? ` ${windLabel.toUpperCase()}` : ''}
            </div>
          )}
          {hail && (
            <div style={pillStyle('#0f3d22', '#1a5c33')}>
              <i className="fas fa-circle" style={{ fontSize: 9 }}></i>
              HAIL: {hail} INCHES{hailLabel ? ` ${hailLabel.toUpperCase()}` : ''}
            </div>
          )}
          {motion?.speed_mph && motionToLabel && (
            <div style={pillStyle('#3d1a6e', '#5a2899')}>
              <i className="fas fa-compass" style={{ fontSize: 13 }}></i>
              MOTION: {motionToLabel} {Math.round(motion.speed_mph)} MPH
            </div>
          )}
          {!hasThreat && (
            <span style={{ color: '#94a3b8', fontSize: 13, fontStyle: 'italic' }}>
              {alert.headline || alert.event_name}
            </span>
          )}
        </div>

        {/* ── Map ── */}
        <div style={{ height: `${MAP_H}px`, position: 'relative' }}>
          {viewBounds && bounds && centroid && polygon ? (
            <MapContainer
              center={centroid}
              zoom={computeZoom(viewBounds)}
              style={{ width: '100%', height: '100%' }}
              zoomControl={false}
              attributionControl={false}
              dragging={false}
              touchZoom={false}
              doubleClickZoom={false}
              scrollWheelZoom={false}
              keyboard={false}
            >
              <SetView bounds={viewBounds} />
              <CreateLabelsPane />
              <TileLayer
                url="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
                attribution="Esri"
              />
              <TileLayer
                url="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
                pane="labelsPane"
              />
              {usStates && (
                <GeoJSON 
                  data={usStates} 
                  pane="labelsPane"
                  style={{ color: 'rgba(255, 255, 255, 0.4)', weight: 1.5, fill: false }}
                />
              )}
              {radarFrame?.image_url ? (
                <ImageOverlay
                  url={radarFrame.image_url}
                  bounds={[
                    [radarFrame.bounds.south, radarFrame.bounds.west],
                    [radarFrame.bounds.north, radarFrame.bounds.east],
                  ]}
                  opacity={0.80}
                  zIndex={1}
                  crossOrigin="anonymous"
                />
              ) : (
                // Fallback: RainViewer near-realtime radar (~2-5 min latency)
                <RainViewerLayer />
              )}
              {/* Polygon and arrow above radar overlay */}
              <Polygon
                positions={polygon as any}
                pathOptions={{ color: '#ff8c00', weight: 3, fillOpacity: 0.08, fillColor: '#ff8c00' }}
              />
              {arrowLines.map((line, i) => (
                <Polyline
                  key={i}
                  positions={line}
                  pathOptions={{ color: '#ff8c00', weight: 3 }}
                />
              ))}
            </MapContainer>
          ) : (
            <div style={{
              width: '100%', height: '100%',
              backgroundColor: '#0d1117',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: '#64748b', fontSize: 14,
            }}>
              No geographic data available
            </div>
          )}
        </div>

        {/* ── Footer – counties ── */}
        <div style={{
          height: `${FOOTER_H}px`,
          backgroundColor: '#0a0d14',
          borderTop: `2px solid ${style.backgroundColor}44`,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '7px',
          padding: '8px 20px',
        }}>
          <span style={{
            fontSize: '10px',
            fontWeight: 600,
            color: '#64748b',
            letterSpacing: '1.8px',
            textTransform: 'uppercase',
          }}>
            <i className="fas fa-map-marker-alt" style={{ marginRight: 5 }}></i>
            Affected Counties
          </span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px', justifyContent: 'center' }}>
            {displayList.slice(0, 10).map((label, i) => (
              <span key={i} style={countyBadge(style.backgroundColor)}>
                {label.toUpperCase()}
              </span>
            ))}
            {displayList.length > 10 && (
              <span style={countyBadge('#444')}>
                +{displayList.length - 10} MORE
              </span>
            )}
          </div>
        </div>
      </div>
    );
  },
);

AlertMapGraphic.displayName = 'AlertMapGraphic';

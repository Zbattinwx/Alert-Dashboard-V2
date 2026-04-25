import { Marker, Popup, Polyline } from 'react-leaflet';
import L from 'leaflet';
import type { StormCell } from '../types/radar';
import {
  THREAT_LEVEL_COLORS,
  THREAT_LEVEL_LABELS,
  SCORE_FACTOR_LABELS,
  SCORE_FACTOR_WEIGHTS,
} from '../types/radar';

interface StormCellMarkersProps {
  cells: StormCell[];
  showTracks?: boolean;
  showForecasts?: boolean;
  onCellClick?: (cell: StormCell) => void;
}

function createCellIcon(cell: StormCell): L.DivIcon {
  const color = THREAT_LEVEL_COLORS[cell.threat_level] || '#888';
  const score = cell.severity_score;
  const size = Math.max(32, Math.min(48, 28 + score / 5));

  let indicators = '';
  if (cell.rotation_detected) {
    indicators += '<span class="cell-indicator cell-rotation" title="Rotation Detected">&#x21BB;</span>';
  }
  if (cell.tvs_detected) {
    indicators += '<span class="cell-indicator cell-tvs" title="TVS">T</span>';
  }
  if (cell.hail_indicated) {
    indicators += '<span class="cell-indicator cell-hail" title="Hail">&#x25CF;</span>';
  }
  if (cell.debris_signature) {
    indicators += '<span class="cell-indicator cell-debris" title="Debris Signature">&#x26A0;</span>';
  }

  const pulseClass = cell.rotation_detected ? ' cell-pulse' : '';
  const trendArrow = cell.trend === 'strengthening' ? '&#x25B2;' : cell.trend === 'weakening' ? '&#x25BC;' : '';

  return L.divIcon({
    className: 'storm-cell-marker',
    html: `
      <div class="cell-circle${pulseClass}" style="
        width: ${size}px;
        height: ${size}px;
        background: radial-gradient(circle, ${color}cc, ${color}88);
        border: 2px solid ${color};
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #fff;
        font-weight: bold;
        font-size: ${size > 38 ? 14 : 12}px;
        text-shadow: 0 0 4px rgba(0,0,0,0.8);
        box-shadow: 0 0 8px ${color}66;
        position: relative;
      ">
        ${score}
        ${trendArrow ? `<span class="cell-trend">${trendArrow}</span>` : ''}
      </div>
      <div class="cell-indicators">${indicators}</div>
    `,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return ts;
  }
}

function CellPopup({ cell }: { cell: StormCell }) {
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
        <div className="cell-stat">
          <span className="cell-stat-label">Max Reflectivity</span>
          <span className="cell-stat-value">{cell.max_reflectivity_dbz.toFixed(1)} dBZ</span>
        </div>
        <div className="cell-stat">
          <span className="cell-stat-label">Area</span>
          <span className="cell-stat-value">{cell.area_km2.toFixed(1)} km²</span>
        </div>
        <div className="cell-stat">
          <span className="cell-stat-label">Motion</span>
          <span className="cell-stat-value">
            {cell.motion_direction_deg.toFixed(0)}° at {cell.motion_speed_kph.toFixed(0)} km/h
          </span>
        </div>
        <div className="cell-stat">
          <span className="cell-stat-label">Trend</span>
          <span className={`cell-stat-value trend-${cell.trend}`}>
            {cell.trend.charAt(0).toUpperCase() + cell.trend.slice(1)}
          </span>
        </div>
      </div>

      <div className="cell-popup-flags">
        {cell.rotation_detected && (
          <span className="cell-flag flag-rotation">
            Rotation {cell.rotation_velocity_ms ? `(${cell.rotation_velocity_ms} m/s)` : ''}
          </span>
        )}
        {cell.tvs_detected && <span className="cell-flag flag-tvs">TVS</span>}
        {cell.hail_indicated && (
          <span className="cell-flag flag-hail">
            Hail {cell.hail_max_dbz ? `(${cell.hail_max_dbz.toFixed(0)} dBZ)` : ''}
          </span>
        )}
        {cell.debris_signature && <span className="cell-flag flag-debris">Debris Signature</span>}
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
                <div
                  className="breakdown-bar"
                  style={{ width: `${score}%`, background: score > 70 ? '#ff4444' : score > 40 ? '#ffaa00' : '#44aa44' }}
                />
              </div>
              <span className="breakdown-value">{weighted}</span>
            </div>
          );
        })}
      </div>

      <div className="cell-popup-meta">
        <span>First seen: {formatTimestamp(cell.first_detected)}</span>
        <span>Updated: {formatTimestamp(cell.last_updated)}</span>
        <span>Scans tracked: {cell.scan_count}</span>
      </div>
    </div>
  );
}

export default function StormCellMarkers({
  cells,
  showTracks = true,
  showForecasts = true,
  onCellClick,
}: StormCellMarkersProps) {
  return (
    <>
      {cells.map((cell) => {
        const color = THREAT_LEVEL_COLORS[cell.threat_level] || '#888';

        return (
          <span key={cell.cell_id}>
            {/* Track history line */}
            {showTracks && cell.track_history.length > 1 && (
              <Polyline
                positions={cell.track_history.map((p) => [p.lat, p.lon])}
                pathOptions={{
                  color,
                  weight: 2,
                  opacity: 0.5,
                  dashArray: '6, 4',
                }}
              />
            )}

            {/* Forecast track line */}
            {showForecasts && cell.forecast_track.length > 0 && (
              <Polyline
                positions={[
                  [cell.lat, cell.lon],
                  ...cell.forecast_track.map((p) => [p.lat, p.lon] as [number, number]),
                ]}
                pathOptions={{
                  color,
                  weight: 2,
                  opacity: 0.4,
                  dashArray: '3, 6',
                }}
              />
            )}

            {/* Cell marker */}
            <Marker
              position={[cell.lat, cell.lon]}
              icon={createCellIcon(cell)}
              eventHandlers={{
                click: () => onCellClick?.(cell),
              }}
            >
              <Popup maxWidth={320} className="cell-popup-container">
                <CellPopup cell={cell} />
              </Popup>
            </Marker>
          </span>
        );
      })}
    </>
  );
}

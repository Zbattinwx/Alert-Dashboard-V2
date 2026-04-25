import React, { useEffect, useState, useCallback, useRef } from 'react';
import { MapContainer, TileLayer, CircleMarker, Popup, Tooltip } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import { apiUrl } from '../utils/api';

interface AsosObs {
  station: string;
  state: string;
  lat: number | null;
  lon: number | null;
  valid_time: string;
  temp_f: number | null;
  dewpoint_f: number | null;
  wind_dir_deg: number | null;
  wind_dir_cardinal: string;
  wind_speed_mph: number | null;
  wind_gust_mph: number | null;
  visibility_mi: number | null;
  altimeter_inhg: number | null;
  sky_condition: string | null;
  sky_cover: string;
  wx_codes: string | null;
}

interface ObsResponse {
  count: number;
  states: string[];
  by_state: Record<string, AsosObs[]>;
  observations: AsosObs[];
}

function tempColor(t: number | null): string {
  if (t === null) return '#888';
  if (t >= 90) return '#ff4444';
  if (t >= 80) return '#ff8800';
  if (t >= 70) return '#ffcc00';
  if (t >= 60) return '#88cc00';
  if (t >= 50) return '#44bb44';
  if (t >= 32) return '#44aaff';
  if (t >= 10) return '#aaddff';
  return '#ccccff';
}

function skyIcon(cover: string): string {
  switch (cover.toUpperCase()) {
    case 'OVC': return 'fa-cloud';
    case 'BKN': return 'fa-cloud';
    case 'SCT': return 'fa-cloud-sun';
    case 'FEW': return 'fa-cloud-sun';
    case 'VV':  return 'fa-smog';
    default:    return 'fa-sun';
  }
}

function skyColor(cover: string): string {
  switch (cover.toUpperCase()) {
    case 'OVC': return '#888';
    case 'BKN': return '#aaa';
    case 'SCT': return '#bbb';
    case 'FEW': return '#ccc';
    default:    return '#f5c518';
  }
}

function fmt(val: number | null, decimals = 0, unit = ''): string {
  if (val === null || val === undefined) return '—';
  return val.toFixed(decimals) + (unit ? ` ${unit}` : '');
}

function windStr(obs: AsosObs): string {
  if (obs.wind_speed_mph === null) return '—';
  const calm = obs.wind_speed_mph < 2;
  if (calm) return 'Calm';
  let s = `${obs.wind_dir_cardinal} ${fmt(obs.wind_speed_mph, 0)} mph`;
  if (obs.wind_gust_mph) s += ` G${fmt(obs.wind_gust_mph, 0)}`;
  return s;
}

function ageMinutes(isoTime: string): number {
  return Math.round((Date.now() - new Date(isoTime).getTime()) / 60000);
}

// Wind direction arrow as SVG
function WindArrow({ deg, speed }: { deg: number | null; speed: number | null }) {
  if (deg === null || speed === null || speed < 2) return <span style={{ color: '#888', fontSize: '0.7rem' }}>Calm</span>;
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" style={{ display: 'inline-block', verticalAlign: 'middle' }}>
      <g transform={`rotate(${deg}, 9, 9)`}>
        <line x1="9" y1="14" x2="9" y2="4" stroke="var(--text-primary)" strokeWidth="2" strokeLinecap="round" />
        <polygon points="9,2 6.5,7 11.5,7" fill="var(--text-primary)" />
      </g>
    </svg>
  );
}

export const MetarSection: React.FC = () => {
  const [data, setData] = useState<ObsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState(1);
  const [filterState, setFilterState] = useState<string>('all');
  const [sortKey, setSortKey] = useState<'station' | 'temp' | 'wind' | 'visibility'>('station');
  const [showMap, setShowMap] = useState(true);
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetch = useCallback(async (h = hours, forceRefresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ hours: String(h), force_refresh: String(forceRefresh) });
      const res = await window.fetch(apiUrl(`/api/asos/observations?${params}`));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d: ObsResponse = await res.json();
      setData(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load observations');
    } finally {
      setLoading(false);
    }
  }, [hours]);

  useEffect(() => {
    fetch(hours);
    refreshTimer.current = setInterval(() => fetch(hours), 5 * 60 * 1000);
    return () => { if (refreshTimer.current) clearInterval(refreshTimer.current); };
  }, [hours, fetch]);

  const allObs = data?.observations ?? [];
  const states = data?.states ?? [];

  const filteredObs = filterState === 'all' ? allObs : allObs.filter(o => o.state === filterState);

  const sortedObs = [...filteredObs].sort((a, b) => {
    if (sortKey === 'temp') return (b.temp_f ?? -999) - (a.temp_f ?? -999);
    if (sortKey === 'wind') return (b.wind_speed_mph ?? 0) - (a.wind_speed_mph ?? 0);
    if (sortKey === 'visibility') return (a.visibility_mi ?? 999) - (b.visibility_mi ?? 999);
    return `${a.state}${a.station}`.localeCompare(`${b.state}${b.station}`);
  });

  const mapObs = allObs.filter(o => o.lat && o.lon);

  const SortBtn = ({ k, label }: { k: typeof sortKey; label: string }) => (
    <button
      onClick={() => setSortKey(k)}
      style={{
        padding: '3px 8px', borderRadius: '4px', fontSize: '0.75rem',
        border: '1px solid var(--border-color)',
        backgroundColor: sortKey === k ? 'var(--primary-color)' : 'var(--bg-secondary)',
        color: sortKey === k ? 'white' : 'var(--text-secondary)',
        cursor: 'pointer',
      }}
    >{label}</button>
  );

  return (
    <div className="section active">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px', flexWrap: 'wrap', gap: '8px' }}>
        <h2 className="section-title" style={{ margin: 0 }}>Surface Observations (ASOS/METAR)</h2>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
          {/* Lookback */}
          <select
            value={hours}
            onChange={e => setHours(Number(e.target.value))}
            style={{ padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.8rem' }}
          >
            <option value={1}>Last 1 hour</option>
            <option value={2}>Last 2 hours</option>
            <option value={3}>Last 3 hours</option>
          </select>
          {/* State filter */}
          <select
            value={filterState}
            onChange={e => setFilterState(e.target.value)}
            style={{ padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.8rem' }}
          >
            <option value="all">All States</option>
            {states.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          {/* Map toggle */}
          <button
            onClick={() => setShowMap(p => !p)}
            style={{ padding: '4px 10px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.8rem', cursor: 'pointer' }}
          >
            <i className={`fas fa-map${showMap ? '-marked-alt' : ''}`}></i> {showMap ? 'Hide Map' : 'Show Map'}
          </button>
          {/* Refresh */}
          <button
            onClick={() => fetch(hours, true)}
            disabled={loading}
            style={{ padding: '4px 10px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.8rem', cursor: 'pointer' }}
          >
            <i className={`fas fa-sync${loading ? ' fa-spin' : ''}`}></i>
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: '12px', backgroundColor: 'rgba(255,60,60,0.12)', borderRadius: '6px', color: '#ff6060', marginBottom: '12px', fontSize: '0.85rem' }}>
          <i className="fas fa-exclamation-triangle"></i> {error}
        </div>
      )}

      {/* Map */}
      {showMap && mapObs.length > 0 && (
        <div style={{ height: '320px', borderRadius: '8px', overflow: 'hidden', marginBottom: '16px', border: '1px solid var(--border-color)' }}>
          <MapContainer
            center={[mapObs[0].lat!, mapObs[0].lon!]}
            zoom={7}
            style={{ height: '100%', width: '100%' }}
            zoomControl={true}
          >
            <TileLayer
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
              attribution='&copy; OpenStreetMap contributors &copy; CARTO'
            />
            {mapObs.map(obs => (
              <CircleMarker
                key={obs.station}
                center={[obs.lat!, obs.lon!]}
                radius={8}
                pathOptions={{
                  fillColor: tempColor(obs.temp_f),
                  fillOpacity: 0.9,
                  color: '#fff',
                  weight: 1.5,
                }}
              >
                <Tooltip direction="top" offset={[0, -8]} permanent={false}>
                  <div style={{ fontSize: '0.8rem', lineHeight: 1.4 }}>
                    <strong>{obs.station}</strong> ({obs.state})<br />
                    {obs.temp_f !== null ? `${obs.temp_f.toFixed(0)}°F` : '—'} / {obs.dewpoint_f !== null ? `${obs.dewpoint_f.toFixed(0)}°F` : '—'} dew<br />
                    {windStr(obs)}<br />
                    Vis: {fmt(obs.visibility_mi, 1, 'mi')} · {obs.sky_cover}
                  </div>
                </Tooltip>
                <Popup>
                  <div style={{ minWidth: '160px', fontSize: '0.82rem', lineHeight: 1.6 }}>
                    <strong>{obs.station}</strong> — {obs.state}<br />
                    <span style={{ fontSize: '0.72rem', color: '#888' }}>{ageMinutes(obs.valid_time)} min ago</span><br />
                    Temp: <strong>{fmt(obs.temp_f, 0, '°F')}</strong><br />
                    Dewpoint: {fmt(obs.dewpoint_f, 0, '°F')}<br />
                    Wind: {windStr(obs)}<br />
                    Visibility: {fmt(obs.visibility_mi, 1, 'mi')}<br />
                    Sky: {obs.sky_condition || obs.sky_cover}<br />
                    Altimeter: {fmt(obs.altimeter_inhg, 2, 'inHg')}<br />
                    {obs.wx_codes && <span>WX: {obs.wx_codes}</span>}
                  </div>
                </Popup>
              </CircleMarker>
            ))}
          </MapContainer>
        </div>
      )}

      {/* Sort bar */}
      <div style={{ display: 'flex', gap: '6px', marginBottom: '10px', alignItems: 'center' }}>
        <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginRight: '4px' }}>Sort:</span>
        <SortBtn k="station" label="Station" />
        <SortBtn k="temp" label="Temperature" />
        <SortBtn k="wind" label="Wind Speed" />
        <SortBtn k="visibility" label="Visibility" />
        {!loading && (
          <span style={{ marginLeft: 'auto', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
            {sortedObs.length} station{sortedObs.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {loading && !data ? (
        <div style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '40px' }}>
          <i className="fas fa-spinner fa-spin" style={{ fontSize: '1.5rem' }}></i>
          <div style={{ marginTop: '8px' }}>Loading observations...</div>
        </div>
      ) : sortedObs.length === 0 ? (
        <div style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '40px', fontSize: '0.9rem' }}>
          No observations found. Check that your monitored states are configured in Settings.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
            <thead>
              <tr style={{ borderBottom: '2px solid var(--border-color)', color: 'var(--text-secondary)', textAlign: 'left' }}>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Station</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Temp</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Dew</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Wind</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Gust</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Vis</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Sky</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Altim</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>WX</th>
                <th style={{ padding: '6px 8px', fontWeight: 600 }}>Age</th>
              </tr>
            </thead>
            <tbody>
              {sortedObs.map(obs => {
                const age = ageMinutes(obs.valid_time);
                const stale = age > 75;
                return (
                  <tr
                    key={`${obs.state}-${obs.station}`}
                    style={{
                      borderBottom: '1px solid var(--border-color)',
                      opacity: stale ? 0.5 : 1,
                      transition: 'background 0.1s',
                    }}
                    onMouseEnter={e => (e.currentTarget.style.backgroundColor = 'var(--bg-secondary)')}
                    onMouseLeave={e => (e.currentTarget.style.backgroundColor = '')}
                  >
                    <td style={{ padding: '5px 8px', fontFamily: 'monospace', fontWeight: 600, color: 'var(--text-primary)' }}>
                      <span style={{ fontSize: '0.68rem', color: 'var(--text-secondary)', marginRight: '4px' }}>{obs.state}</span>
                      {obs.station}
                    </td>
                    <td style={{ padding: '5px 8px', fontWeight: 700, color: tempColor(obs.temp_f) }}>
                      {obs.temp_f !== null ? `${obs.temp_f.toFixed(0)}°` : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', color: '#44aaff' }}>
                      {obs.dewpoint_f !== null ? `${obs.dewpoint_f.toFixed(0)}°` : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>
                      <WindArrow deg={obs.wind_dir_deg} speed={obs.wind_speed_mph} />
                      {' '}
                      {obs.wind_speed_mph !== null ? (obs.wind_speed_mph < 2 ? 'Calm' : `${obs.wind_dir_cardinal} ${obs.wind_speed_mph.toFixed(0)}`) : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', color: obs.wind_gust_mph && obs.wind_gust_mph >= 40 ? '#ff8800' : 'var(--text-primary)' }}>
                      {obs.wind_gust_mph ? `G${obs.wind_gust_mph.toFixed(0)}` : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', color: obs.visibility_mi !== null && obs.visibility_mi < 3 ? '#ff6600' : 'var(--text-primary)' }}>
                      {obs.visibility_mi !== null ? `${obs.visibility_mi.toFixed(1)}` : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>
                      <i className={`fas ${skyIcon(obs.sky_cover)}`} style={{ color: skyColor(obs.sky_cover), marginRight: '4px', fontSize: '0.75rem' }}></i>
                      {obs.sky_condition || obs.sky_cover}
                    </td>
                    <td style={{ padding: '5px 8px', fontFamily: 'monospace', fontSize: '0.78rem' }}>
                      {obs.altimeter_inhg !== null ? obs.altimeter_inhg.toFixed(2) : '—'}
                    </td>
                    <td style={{ padding: '5px 8px', fontSize: '0.75rem', color: '#aaa' }}>
                      {obs.wx_codes || '—'}
                    </td>
                    <td style={{ padding: '5px 8px', fontSize: '0.75rem', color: stale ? '#ff6060' : 'var(--text-secondary)' }}>
                      {age}m
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

import { useState, useEffect, useRef, useMemo } from 'react';
import { MapContainer, TileLayer, Polygon, CircleMarker, useMap } from 'react-leaflet';
import 'leaflet/dist/leaflet.css';
import RadarOverlay from './RadarOverlay';
import StormCellMarkers from './StormCellMarkers';
import type { RadarFrame, RadarStatus, StormCell, RadarProduct, NexradSite, LightningFlash, MCSSystem } from '../types/radar';
import { THREAT_LEVEL_COLORS, THREAT_LEVEL_LABELS, MCS_TYPE_LABELS, RADAR_PRODUCT_SHORT, RADAR_PRODUCT_LABELS } from '../types/radar';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';
import { apiUrl } from '../utils/api';

const RADAR_ALERT_TOGGLES = [
  { phenomenon: 'TO', label: 'TOR', title: 'Tornado Warning' },
  { phenomenon: 'SV', label: 'SVR', title: 'Severe Thunderstorm Warning' },
  { phenomenon: 'FF', label: 'FFW', title: 'Flash Flood Warning' },
] as const;

const PRODUCTS: RadarProduct[] = ['reflectivity', 'velocity', 'cross_correlation_ratio'];
const MIN_SCORE_FOR_MAP = 20;
const MAX_SITES = 3;
const HISTORY_COUNT = 10;
const ANIM_INTERVAL_MS = 500;

interface SiteAnimState {
  history: RadarFrame[];
  index: number;
  animating: boolean;
}

interface RadarSectionProps {
  radarFrame: RadarFrame | null;
  radarFrames?: Record<string, RadarFrame>;
  radarStatus: RadarStatus | null;
  stormCells: StormCell[];
  mcsSystems?: MCSSystem[];
  alerts?: Alert[];
  lightningFlashes?: LightningFlash[];
  focusedCellId?: string | null;
}

function MapFlyTo({ cell }: { cell: StormCell | null }) {
  const map = useMap();
  useEffect(() => {
    if (cell) map.flyTo([cell.lat, cell.lon], Math.max(map.getZoom(), 9), { duration: 1 });
  }, [cell, map]);
  return null;
}

export default function RadarSection({
  radarFrame,
  radarFrames = {},
  radarStatus,
  stormCells,
  mcsSystems = [],
  alerts = [],
  lightningFlashes = [],
  focusedCellId,
}: RadarSectionProps) {
  const [activeProduct, setActiveProduct] = useState<RadarProduct>('reflectivity');
  const [opacity, setOpacity] = useState(0.65);
  const [selectedCell, setSelectedCell] = useState<StormCell | null>(null);
  const [alertOverlay, setAlertOverlay] = useState<Set<string>>(new Set(['TO', 'SV']));
  const [showLightning, setShowLightning] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [sitePickerOpen, setSitePickerOpen] = useState(false);
  const [sitePickerMode, setSitePickerMode] = useState<'add' | 'replace'>('add');
  const [sites, setSites] = useState<NexradSite[]>([]);
  const [siteAnim, setSiteAnim] = useState<Record<string, SiteAnimState>>({});
  const [gateDbz, setGateDbz] = useState(10);
  const [gateEditing, setGateEditing] = useState(false);
  const [opacityEditing, setOpacityEditing] = useState(false);

  const activeSites = useMemo(() => (
    radarStatus?.active_sites?.length
      ? radarStatus.active_sites
      : radarStatus?.active_site ? [radarStatus.active_site] : ['KILN']
  ), [radarStatus]);

  const primarySite = activeSites[0];

  // Fetch all NEXRAD sites + gate value once
  useEffect(() => {
    fetch(apiUrl('/api/radar/sites')).then(r => r.json()).then(setSites).catch(() => {});
    fetch(apiUrl('/api/radar/gate')).then(r => r.json()).then(d => setGateDbz(d.gate_dbz ?? 10)).catch(() => {});
  }, []);

  // Select focused cell from App-level message
  useEffect(() => {
    if (!focusedCellId) return;
    setSelectedCell(stormCells.find(c => c.cell_id === focusedCellId) ?? null);
  }, [focusedCellId, stormCells]);

  // Fetch history per site on product change or sites change
  useEffect(() => {
    const load = async () => {
      const next: Record<string, SiteAnimState> = {};
      for (const site of activeSites) {
        try {
          const res = await fetch(apiUrl(`/api/radar/frames/${activeProduct}?count=${HISTORY_COUNT}&site=${site}`));
          if (res.ok) {
            const data: RadarFrame[] = await res.json();
            next[site] = {
              history: data,
              index: data.length ? data.length - 1 : 0,
              animating: siteAnim[site]?.animating ?? false,
            };
          }
        } catch {}
      }
      setSiteAnim(next);
    };
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProduct, activeSites.join(',')]);

  // Live frame updates — append to the matching site's history
  useEffect(() => {
    if (!radarFrame || radarFrame.product !== activeProduct) return;
    setSiteAnim(prev => {
      const entry = prev[radarFrame.site] ?? { history: [], index: 0, animating: false };
      // Dedupe by timestamp
      if (entry.history.some(f => f.timestamp === radarFrame.timestamp)) return prev;
      const history = [...entry.history, radarFrame].slice(-HISTORY_COUNT);
      return {
        ...prev,
        [radarFrame.site]: {
          history,
          index: entry.animating ? entry.index : history.length - 1,
          animating: entry.animating,
        },
      };
    });
  }, [radarFrame, activeProduct]);

  // Seed from any frames present in App state
  useEffect(() => {
    const incoming = Object.values(radarFrames).filter(f => f.product === activeProduct);
    if (incoming.length === 0) return;
    setSiteAnim(prev => {
      const next = { ...prev };
      for (const f of incoming) {
        if (!next[f.site] || !next[f.site].history.some(h => h.timestamp === f.timestamp)) {
          const existing = next[f.site]?.history ?? [];
          const history = [...existing, f].slice(-HISTORY_COUNT);
          next[f.site] = {
            history,
            index: history.length - 1,
            animating: next[f.site]?.animating ?? false,
          };
        }
      }
      return next;
    });
  }, [radarFrames, activeProduct]);

  // Single ticker advances every animating site
  useEffect(() => {
    const anyAnimating = Object.values(siteAnim).some(s => s.animating && s.history.length > 1);
    if (!anyAnimating) return;
    const t = setInterval(() => {
      setSiteAnim(prev => {
        const out: Record<string, SiteAnimState> = {};
        for (const [site, s] of Object.entries(prev)) {
          out[site] = s.animating && s.history.length > 1
            ? { ...s, index: (s.index + 1) % s.history.length }
            : s;
        }
        return out;
      });
    }, ANIM_INTERVAL_MS);
    return () => clearInterval(t);
  }, [siteAnim]);

  const displayedFrames: Record<string, RadarFrame | null> = useMemo(() => {
    const out: Record<string, RadarFrame | null> = {};
    for (const site of activeSites) {
      const s = siteAnim[site];
      out[site] = s && s.history[s.index] ? s.history[s.index] : null;
    }
    return out;
  }, [siteAnim, activeSites]);

  const applyGate = (v: number) => {
    fetch(apiUrl('/api/radar/gate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate_dbz: v }),
    }).catch(() => {});
  };

  const handleAddSite = async (siteId: string) => {
    try {
      await fetch(apiUrl('/api/radar/sites/add'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_id: siteId }),
      });
    } catch {}
    setSitePickerOpen(false);
  };

  const handleRemoveSite = async (siteId: string) => {
    try {
      await fetch(apiUrl('/api/radar/sites/remove'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_id: siteId }),
      });
      setSiteAnim(prev => { const n = { ...prev }; delete n[siteId]; return n; });
    } catch {}
  };

  const handleReplaceSite = async (siteId: string) => {
    try {
      await fetch(apiUrl('/api/radar/site'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_id: siteId }),
      });
      setSiteAnim({});
    } catch {}
    setSitePickerOpen(false);
  };

  const toggleAnim = (site: string) => {
    setSiteAnim(prev => {
      const e = prev[site];
      if (!e) return prev;
      return { ...prev, [site]: { ...e, animating: !e.animating } };
    });
  };

  const stepSite = (site: string, delta: number) => {
    setSiteAnim(prev => {
      const e = prev[site];
      if (!e || e.history.length === 0) return prev;
      const next = (e.index + delta + e.history.length) % e.history.length;
      return { ...prev, [site]: { ...e, index: next, animating: false } };
    });
  };

  const setSiteIndex = (site: string, index: number) => {
    setSiteAnim(prev => {
      const e = prev[site];
      if (!e) return prev;
      return { ...prev, [site]: { ...e, index, animating: false } };
    });
  };

  const sortedCells = [...stormCells].sort((a, b) => b.severity_score - a.severity_score);
  const mapCells = stormCells.filter(c => c.severity_score >= MIN_SCORE_FOR_MAP);
  const alertPolygons = alerts.filter(a =>
    a.polygon && a.polygon.length >= 3 &&
    alertOverlay.has(a.phenomenon) &&
    a.significance !== 'A'
  );

  const toggleAlertType = (p: string) => {
    setAlertOverlay(prev => {
      const n = new Set(prev);
      n.has(p) ? n.delete(p) : n.add(p);
      return n;
    });
  };

  const primaryFrame = displayedFrames[primarySite] ?? null;
  const topThreat = sortedCells[0];

  return (
    <div className="radar-v2">
      <MapContainer
        center={[39.9612, -82.9988]}
        zoom={7}
        className="radar-v2-map"
        zoomControl={false}
      >
        <TileLayer
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
          maxZoom={19}
        />
        <MapFlyTo cell={selectedCell} />
        {activeSites.map(site => (
          <RadarOverlay key={site} frame={displayedFrames[site] ?? null} opacity={opacity} />
        ))}

        {alertPolygons.map(alert => {
          const color = getAlertStyle(alert.phenomenon, alert.significance).backgroundColor;
          return (
            <Polygon
              key={alert.product_id}
              positions={alert.polygon!.map(p => [p[0], p[1]] as [number, number])}
              pathOptions={{ color, weight: 2, fillColor: color, fillOpacity: 0.15, opacity: 0.9 }}
            />
          );
        })}

        {showLightning && (() => {
          const now = Date.now();
          return lightningFlashes.map((f, i) => {
            const ageMs = now - new Date(f.timestamp).getTime();
            const ageFrac = Math.min(1, ageMs / (15 * 60 * 1000));
            const o = Math.max(0.05, 1 - ageFrac);
            const r = ageMs < 60_000 ? 5 : 3;
            return (
              <CircleMarker
                key={`lightning-${i}-${f.timestamp}`}
                center={[f.lat, f.lon]}
                radius={r}
                pathOptions={{ color: '#ffe066', fillColor: '#ffffff', fillOpacity: o, opacity: o, weight: 1 }}
              />
            );
          });
        })()}

        <StormCellMarkers cells={mapCells} onCellClick={setSelectedCell} />
      </MapContainer>

      {/* ── TOP LEFT: Site chips ── */}
      <div className="rc-panel rc-topleft">
        <div className="rc-site-chips">
          {activeSites.map((siteId, idx) => {
            const info = sites.find(s => s.id === siteId);
            return (
              <div key={siteId} className={`rc-chip ${idx === 0 ? 'primary' : ''}`} title={info?.name || siteId}>
                <i className="fa fa-satellite-dish" />
                <span className="rc-chip-id">{siteId}</span>
                {activeSites.length > 1 && (
                  <button
                    className="rc-chip-x"
                    onClick={() => handleRemoveSite(siteId)}
                    title={`Remove ${siteId}`}
                  ><i className="fa fa-times" /></button>
                )}
              </div>
            );
          })}
          {activeSites.length < MAX_SITES && (
            <button
              className="rc-chip rc-chip-add"
              onClick={() => { setSitePickerMode('add'); setSitePickerOpen(true); }}
              title="Add a radar site"
            >
              <i className="fa fa-plus" /> Add
            </button>
          )}
          <button
            className="rc-chip-switch"
            onClick={() => { setSitePickerMode('replace'); setSitePickerOpen(true); }}
            title="Replace all with one site"
          ><i className="fa fa-search" /></button>
        </div>
      </div>

      {/* ── TOP RIGHT: Product toolbar + layer toggles ── */}
      <div className="rc-panel rc-topright">
        <div className="rc-product-pill">
          {PRODUCTS.map(p => (
            <button
              key={p}
              className={`rc-product-btn ${activeProduct === p ? 'active' : ''}`}
              onClick={() => setActiveProduct(p)}
              title={RADAR_PRODUCT_LABELS[p]}
            >
              {RADAR_PRODUCT_SHORT[p]}
            </button>
          ))}
        </div>

        <div className="rc-slider-group" onMouseEnter={() => setOpacityEditing(true)} onMouseLeave={() => setOpacityEditing(false)}>
          <button className="rc-icon-btn" title={`Opacity ${Math.round(opacity * 100)}%`}>
            <i className="fa fa-adjust" />
          </button>
          {opacityEditing && (
            <div className="rc-slider-popover">
              <label>Opacity</label>
              <input
                type="range" min={0} max={100} value={Math.round(opacity * 100)}
                onChange={e => setOpacity(parseInt(e.target.value) / 100)}
              />
              <span className="rc-slider-val">{Math.round(opacity * 100)}%</span>
            </div>
          )}
        </div>

        {activeProduct === 'reflectivity' && (
          <div className="rc-slider-group" onMouseEnter={() => setGateEditing(true)} onMouseLeave={() => setGateEditing(false)}>
            <button className="rc-icon-btn" title={`Gate filter ${gateDbz} dBZ`}>
              <i className="fa fa-filter" />
            </button>
            {gateEditing && (
              <div className="rc-slider-popover">
                <label>Gate Filter</label>
                <input
                  type="range" min={-20} max={40} step={1} value={gateDbz}
                  onChange={e => setGateDbz(parseInt(e.target.value))}
                  onMouseUp={e => applyGate(parseInt((e.target as HTMLInputElement).value))}
                  onTouchEnd={e => applyGate(parseInt((e.target as HTMLInputElement).value))}
                />
                <span className="rc-slider-val">{gateDbz} dBZ</span>
              </div>
            )}
          </div>
        )}

        <div className="rc-layer-toggles">
          {RADAR_ALERT_TOGGLES.map(({ phenomenon, label, title }) => (
            <button
              key={phenomenon}
              title={title}
              className={`rc-layer-toggle ${alertOverlay.has(phenomenon) ? 'active' : ''}`}
              style={{ '--toggle-color': getAlertStyle(phenomenon, 'W').backgroundColor } as React.CSSProperties}
              onClick={() => toggleAlertType(phenomenon)}
            >{label}</button>
          ))}
          <button
            title="GOES-16 GLM Lightning (15 min)"
            className={`rc-layer-toggle ${showLightning ? 'active' : ''}`}
            style={{ '--toggle-color': '#ffe066' } as React.CSSProperties}
            onClick={() => setShowLightning(v => !v)}
          >⚡</button>
        </div>
      </div>

      {/* ── MCS banners strip (top-center, below chrome) ── */}
      {mcsSystems.length > 0 && (
        <div className="rc-mcs-strip">
          {mcsSystems.map(sys => (
            <div key={sys.system_id} className={`rc-mcs-chip mcs-type-${sys.system_type}`}>
              <i className="fa fa-align-justify" />
              <span>{MCS_TYPE_LABELS[sys.system_type]} · {sys.length_km.toFixed(0)} km</span>
              {sys.bow_echo_detected && <span className="rc-mcs-tag bow">BOW</span>}
              {sys.rear_inflow_notch && <span className="rc-mcs-tag rni">RNI</span>}
              {sys.book_end_vortices && <span className="rc-mcs-tag bev">BEV</span>}
              {sys.embedded_qlcs_mesos > 0 && (
                <span className="rc-mcs-tag qlcs">{sys.embedded_qlcs_mesos}× QLCS</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── BOTTOM LEFT: Status pill ── */}
      <div className="rc-panel rc-bottomleft">
        <div className="rc-status-pill">
          <span className="rc-status-site">{primarySite}</span>
          <span className="rc-status-sep">·</span>
          <span className="rc-status-product">{RADAR_PRODUCT_LABELS[activeProduct]}</span>
          <span className="rc-status-sep">·</span>
          <span className="rc-status-time">
            {primaryFrame ? new Date(primaryFrame.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '--:--'}
          </span>
          {primaryFrame && (
            <span className="rc-status-age">{ageLabel(primaryFrame.timestamp)}</span>
          )}
          {radarStatus?.processing && (
            <span className="rc-status-spin"><i className="fa fa-spinner fa-spin" /></span>
          )}
          {radarStatus?.error && (
            <span className="rc-status-err" title={radarStatus.error}><i className="fa fa-exclamation-triangle" /></span>
          )}
        </div>
      </div>

      {/* ── BOTTOM CENTER: Per-site scrubber stack ── */}
      <div className="rc-panel rc-bottomcenter">
        <div className="rc-scrubber-stack">
          {activeSites.map(site => {
            const s = siteAnim[site];
            const history = s?.history ?? [];
            const idx = s?.index ?? 0;
            const frame = history[idx];
            return (
              <div key={site} className="rc-scrubber-row">
                <button
                  className={`rc-anim-btn ${s?.animating ? 'active' : ''}`}
                  onClick={() => toggleAnim(site)}
                  disabled={history.length < 2}
                  title={s?.animating ? 'Pause' : 'Play'}
                >
                  <i className={`fa fa-${s?.animating ? 'pause' : 'play'}`} />
                </button>
                <button className="rc-anim-btn" onClick={() => stepSite(site, -1)} disabled={history.length < 2} title="Previous frame">
                  <i className="fa fa-step-backward" />
                </button>
                <span className="rc-scrub-site">{site}</span>
                <input
                  type="range"
                  className="rc-scrub-slider"
                  min={0}
                  max={Math.max(0, history.length - 1)}
                  value={idx}
                  onChange={e => setSiteIndex(site, parseInt(e.target.value))}
                  disabled={history.length < 2}
                />
                <button className="rc-anim-btn" onClick={() => stepSite(site, 1)} disabled={history.length < 2} title="Next frame">
                  <i className="fa fa-step-forward" />
                </button>
                <span className="rc-scrub-meta">
                  {history.length > 0 ? `${idx + 1}/${history.length}` : '0/0'}
                  {frame && <>  ·  {new Date(frame.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── RIGHT DRAWER: Storm cells ── */}
      <div className={`rc-drawer ${drawerOpen ? 'open' : ''}`}>
        <button
          className="rc-drawer-tab"
          onClick={() => setDrawerOpen(v => !v)}
          title={drawerOpen ? 'Hide cell panel' : 'Show cell panel'}
          style={topThreat ? { '--drawer-tab-color': THREAT_LEVEL_COLORS[topThreat.threat_level] } as React.CSSProperties : {}}
        >
          <i className={`fa fa-chevron-${drawerOpen ? 'right' : 'left'}`} />
          <span className="rc-drawer-tab-count">{stormCells.length}</span>
        </button>

        {drawerOpen && (
          <div className="rc-drawer-body">
            <div className="rc-drawer-header">
              <h3>Storm Cells <span className="rc-drawer-count">{stormCells.length}</span></h3>
            </div>

            {sortedCells.length === 0 ? (
              <div className="rc-drawer-empty">
                {radarStatus?.enabled ? 'No storm cells detected' : 'Radar service not active'}
              </div>
            ) : (
              <div className="rc-drawer-list">
                {sortedCells.map(cell => (
                  <div
                    key={cell.cell_id}
                    className={`rc-cell-item ${selectedCell?.cell_id === cell.cell_id ? 'selected' : ''} ${cell.mcs_system_id ? 'in-system' : ''}`}
                    onClick={() => setSelectedCell(cell)}
                  >
                    <div className="rc-cell-score" style={{ background: THREAT_LEVEL_COLORS[cell.threat_level] }}>
                      {cell.severity_score}
                    </div>
                    <div className="rc-cell-info">
                      <div className="rc-cell-id">
                        {cell.cell_id}
                        {cell.mcs_system_id && <span className="rc-cell-sys">SYS</span>}
                      </div>
                      <div className="rc-cell-tags">
                        <span className="rc-cell-dbz">{cell.max_reflectivity_dbz.toFixed(0)} dBZ</span>
                        {cell.rotation_detected && <span className="rc-cell-tag meso">MESO</span>}
                        {cell.qlcs_meso_detected && <span className="rc-cell-tag qlcs">QLCS</span>}
                        {cell.llsd_rotation_detected && <span className="rc-cell-tag llsd">LLSD</span>}
                        {cell.tvs_detected && <span className="rc-cell-tag tvs">TVS</span>}
                        {cell.hail_indicated && <span className="rc-cell-tag hail">HAIL</span>}
                        {cell.debris_signature && <span className="rc-cell-tag tds">TDS</span>}
                      </div>
                    </div>
                    <div className={`rc-cell-trend trend-${cell.trend}`}>
                      {cell.trend === 'strengthening' && '\u25B2'}
                      {cell.trend === 'weakening' && '\u25BC'}
                      {cell.trend === 'steady' && '\u2014'}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {selectedCell && (
              <div className="rc-cell-detail">
                <div className="rc-cell-detail-header">
                  <h4>{selectedCell.cell_id}</h4>
                  <button onClick={() => setSelectedCell(null)}><i className="fa fa-times" /></button>
                </div>
                <div className="rc-cell-detail-threat" style={{ color: THREAT_LEVEL_COLORS[selectedCell.threat_level] }}>
                  <span className="score">{selectedCell.severity_score}</span>
                  <span className="label">{THREAT_LEVEL_LABELS[selectedCell.threat_level]}</span>
                </div>
                {selectedCell.mcs_system_id && (
                  <div className="rc-cell-detail-sys">
                    Part of {MCS_TYPE_LABELS[mcsSystems.find(s => s.system_id === selectedCell.mcs_system_id)?.system_type ?? 'squall_line']}
                  </div>
                )}
                <div className="rc-detail-sections">
                  <DetailSection title="Structure">
                    <Row label="Max Refl" value={`${selectedCell.max_reflectivity_dbz.toFixed(1)} dBZ`} />
                    <Row label="Area" value={`${selectedCell.area_km2.toFixed(1)} km²`} />
                    {selectedCell.cell_top_km != null && <Row label="Cell Top" value={`${selectedCell.cell_top_km.toFixed(1)} km`} />}
                    {selectedCell.cell_base_km != null && <Row label="Cell Base" value={`${selectedCell.cell_base_km.toFixed(1)} km`} />}
                    {selectedCell.depth_km != null && <Row label="Depth" value={`${selectedCell.depth_km.toFixed(1)} km`} />}
                    {selectedCell.max_ref_height_km != null && <Row label="Max Ref Height" value={`${selectedCell.max_ref_height_km.toFixed(1)} km`} />}
                    {selectedCell.centroid_height_km != null && <Row label="Centroid Height" value={`${selectedCell.centroid_height_km.toFixed(1)} km`} />}
                    {selectedCell.vil_kg_m2 != null && <Row label="VIL" value={`${selectedCell.vil_kg_m2.toFixed(1)} kg/m²`} />}
                  </DetailSection>
                  <DetailSection title="Rotation">
                    {selectedCell.rotation_detected && (
                      <Row label="Mesocyclone" value={`${selectedCell.rotation_velocity_ms} m/s${selectedCell.tvs_detected ? ' (TVS!)' : ''}`} />
                    )}
                    {selectedCell.qlcs_meso_detected && !selectedCell.rotation_detected && (
                      <Row label="QLCS Meso" value={`${selectedCell.qlcs_meso_velocity_ms} m/s`} />
                    )}
                    {selectedCell.max_rot_velocity_ms != null && selectedCell.max_rot_velocity_ms > 0 && (
                      <Row label="Max Rotation" value={`${selectedCell.max_rot_velocity_ms} m/s${selectedCell.max_rot_height_km != null ? ` @ ${selectedCell.max_rot_height_km} km` : ''}`} />
                    )}
                    {selectedCell.rotation_depth_km != null && selectedCell.rotation_depth_km > 0 && (
                      <Row label="Rotation Depth" value={`${selectedCell.rotation_depth_km.toFixed(1)} km`} />
                    )}
                    {selectedCell.llsd_max_shear != null && (
                      <Row label="Low-Level Shear" value={`${(selectedCell.llsd_max_shear * 1000).toFixed(1)} × 10⁻³ /s${selectedCell.llsd_rotation_detected ? ' ⚠' : ''}${selectedCell.llsd_elevation_deg != null ? ` @ ${selectedCell.llsd_elevation_deg}°` : ''}`} />
                    )}
                    {!selectedCell.rotation_detected && !selectedCell.qlcs_meso_detected &&
                     !selectedCell.llsd_rotation_detected && (selectedCell.max_rot_velocity_ms ?? 0) <= 0 && (
                      <div className="rc-detail-none">No rotation detected</div>
                    )}
                  </DetailSection>
                  <DetailSection title="Hazards">
                    {selectedCell.hail_indicated && (
                      <Row label="Hail" value={`Indicated${selectedCell.hail_max_dbz ? ` (${selectedCell.hail_max_dbz.toFixed(0)} dBZ)` : ''}`} />
                    )}
                    {selectedCell.debris_signature && <Row label="TDS" value="Debris Detected" highlight />}
                    {!selectedCell.hail_indicated && !selectedCell.debris_signature && (
                      <div className="rc-detail-none">No hazard signatures</div>
                    )}
                  </DetailSection>
                  <DetailSection title="Motion">
                    <Row label="Direction" value={`${selectedCell.motion_direction_deg.toFixed(0)}°`} />
                    <Row label="Speed" value={`${selectedCell.motion_speed_kph.toFixed(0)} km/h`} />
                    <Row label="Trend" value={selectedCell.trend} />
                  </DetailSection>
                </div>
                <div className="rc-cell-detail-meta">
                  First seen: {new Date(selectedCell.first_detected).toLocaleTimeString()}
                  {' · '}Scans: {selectedCell.scan_count}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── SITE PICKER MODAL ── */}
      {sitePickerOpen && (
        <SitePickerModal
          sites={sites}
          activeSites={activeSites}
          mode={sitePickerMode}
          onClose={() => setSitePickerOpen(false)}
          onPick={sitePickerMode === 'add' ? handleAddSite : handleReplaceSite}
        />
      )}
    </div>
  );
}

// ──────── subcomponents ────────

function Row({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className={`rc-detail-row ${highlight ? 'highlight' : ''}`}>
      <span className="rc-detail-label">{label}</span>
      <span className="rc-detail-value">{value}</span>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rc-detail-section">
      <h5>{title}</h5>
      {children}
    </div>
  );
}

function ageLabel(ts: string): string {
  const age = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (age < 60) return `${age}s`;
  if (age < 3600) return `${Math.floor(age / 60)}m`;
  return `${Math.floor(age / 3600)}h`;
}

function SitePickerModal({
  sites, activeSites, mode, onClose, onPick,
}: {
  sites: NexradSite[];
  activeSites: string[];
  mode: 'add' | 'replace';
  onClose: () => void;
  onPick: (id: string) => void;
}) {
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [onClose]);

  const filtered = sites.filter(s => {
    const q = query.toLowerCase();
    return !q || s.id.toLowerCase().includes(q) || s.name.toLowerCase().includes(q) || s.state.toLowerCase().includes(q);
  });

  // Group by state
  const grouped = useMemo(() => {
    const m: Record<string, NexradSite[]> = {};
    for (const s of filtered) {
      (m[s.state] = m[s.state] ?? []).push(s);
    }
    return Object.entries(m).sort(([a], [b]) => a.localeCompare(b));
  }, [filtered]);

  return (
    <div className="rc-modal-backdrop" onClick={onClose}>
      <div className="rc-modal" onClick={e => e.stopPropagation()}>
        <div className="rc-modal-header">
          <i className="fa fa-search" />
          <input
            ref={inputRef}
            className="rc-modal-input"
            placeholder={mode === 'add' ? 'Search sites to add...' : 'Search sites to switch to...'}
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          <button className="rc-modal-close" onClick={onClose}><i className="fa fa-times" /></button>
        </div>
        <div className="rc-modal-hint">
          {mode === 'add' ? 'Adds as an additional overlay' : 'Replaces all active sites'}
        </div>
        <div className="rc-modal-body">
          {grouped.length === 0 && <div className="rc-modal-empty">No matching sites</div>}
          {grouped.map(([state, items]) => (
            <div key={state} className="rc-modal-group">
              <div className="rc-modal-group-header">{state}</div>
              {items.map(site => {
                const isActive = activeSites.includes(site.id);
                const disabled = mode === 'add' && isActive;
                return (
                  <button
                    key={site.id}
                    className={`rc-modal-option ${isActive ? 'active' : ''}`}
                    disabled={disabled}
                    onClick={() => onPick(site.id)}
                  >
                    <span className="rc-modal-option-id">{site.id}</span>
                    <span className="rc-modal-option-name">{site.name}</span>
                    {site.distance_km !== undefined && (
                      <span className="rc-modal-option-dist">{site.distance_km} km</span>
                    )}
                    {isActive && <span className="rc-modal-option-badge">active</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

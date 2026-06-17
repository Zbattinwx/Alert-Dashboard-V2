import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { Map as MapGL, Marker, Source, Layer } from 'react-map-gl/maplibre';
import type { MapRef, ViewStateChangeEvent, MapLayerMouseEvent } from 'react-map-gl/maplibre';
import 'maplibre-gl/dist/maplibre-gl.css';

import { RadarGLLayer } from './RadarGLLayer';
import { MRMSGLLayer, parseMRMSBinary } from './MRMSGLLayer';
import type { MRMSFrame } from './MRMSGLLayer';
import type {
  RadarFrame, RadarBinaryFrame, RadarStatus, StormCell,
  RadarProduct, NexradSite, LightningFlash, MCSSystem,
} from '../types/radar';
import {
  THREAT_LEVEL_COLORS, THREAT_LEVEL_LABELS, MCS_TYPE_LABELS,
  RADAR_PRODUCT_SHORT, RADAR_PRODUCT_LABELS,
} from '../types/radar';
import { parseRadarBinaryFrame } from '../utils/radarBinaryParser';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';
import { apiUrl } from '../utils/api';

// MRMS grid bounds (kept for reference — actual bounds come from the binary header)
// La1=54.995°N, La2=20.005°N, Lo1=-129.995°W, Lo2=-60.005°W

const RADAR_ALERT_TOGGLES = [
  { phenomenon: 'TO', label: 'TOR', title: 'Tornado Warning' },
  { phenomenon: 'SV', label: 'SVR', title: 'Severe Thunderstorm Warning' },
  { phenomenon: 'FF', label: 'FFW', title: 'Flash Flood Warning' },
] as const;

const PRODUCTS: RadarProduct[] = ['reflectivity', 'velocity', 'storm_relative_velocity', 'cross_correlation_ratio'];
const MIN_SCORE_FOR_MAP = 20;
const HISTORY_COUNT = 10;
const ANIM_INTERVAL_MS = 500;

// Stadia Alidade Smooth Dark — free for localhost / dev, gray-dark base.
// Register at https://stadiamaps.com for a free API key before deploying.
const MAP_STYLE = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';

interface SiteAnimState {
  history: RadarBinaryFrame[];
  index: number;
  animating: boolean;
}

interface RadarSectionProps {
  radarFrame: RadarBinaryFrame | null;
  radarFrames?: Record<string, RadarBinaryFrame>;
  radarStatus: RadarStatus | null;
  stormCells: StormCell[];
  mcsSystems?: MCSSystem[];
  alerts?: Alert[];
  lightningFlashes?: LightningFlash[];
  focusedCellId?: string | null;
}

function getSavedMapState() {
  try {
    const saved = localStorage.getItem('radarMapState');
    if (saved) {
      const { center, zoom } = JSON.parse(saved);
      if (center && zoom) return { longitude: center[1] ?? center[0], latitude: center[0] ?? center[1], zoom };
    }
  } catch {}
  return { longitude: -82.9988, latitude: 39.9612, zoom: 7 };
}

function distanceKm(lat1: number, lon1: number, lat2: number, lon2: number) {
  const p = 0.017453292519943295;
  const a = 0.5 - Math.cos((lat2 - lat1) * p) / 2 +
            Math.cos(lat1 * p) * Math.cos(lat2 * p) *
            (1 - Math.cos((lon2 - lon1) * p)) / 2;
  return 12742 * Math.asin(Math.sqrt(a));
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
  const mapRef = useRef<MapRef>(null);
  const layerRefs = useRef<Record<string, RadarGLLayer>>({});

  const [activeProduct, setActiveProduct] = useState<RadarProduct>('reflectivity');
  const [opacity, setOpacity] = useState(1.0);
  const [selectedCell, setSelectedCell] = useState<StormCell | null>(null);
  const [alertOverlay, setAlertOverlay] = useState<Set<string>>(new Set(['TO', 'SV']));
  const [showLightning, setShowLightning] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [sites, setSites] = useState<NexradSite[]>([]);
  const [siteAnim, setSiteAnim] = useState<Record<string, SiteAnimState>>({});
  const [gateDbz, setGateDbz] = useState(10);
  const [smooth, setSmooth] = useState(0.0);
  const [gateOpen, setGateOpen] = useState(false);
  const [opacityOpen, setOpacityOpen] = useState(false);
  const [smoothOpen, setSmoothOpen] = useState(false);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [showSiteDots, setShowSiteDots] = useState(false);
  const [showMRMS, setShowMRMS] = useState(false);
  // When MRMS composite is on the operator often wants the NEXRAD layer
  // hidden so the map isn't busy with both feeds.  Default to visible
  // because most users land here for NEXRAD; toggle separately.
  const [showNexrad, setShowNexrad] = useState(true);
  const [mrmsReady, setMrmsReady] = useState(false);
  const [mrmsHistory, setMrmsHistory] = useState<MRMSFrame[]>([]);
  const [mrmsIndex, setMrmsIndex] = useState(0);
  const [mrmsAnimating, setMrmsAnimating] = useState(false);
  const [mrmsLoading, setMrmsLoading] = useState(false);
  const mrmsLayerRef = useRef<MRMSGLLayer | null>(null);

  const initialViewState = useMemo(() => getSavedMapState(), []);

  const activeSites = useMemo(() => (
    radarStatus?.active_sites?.length
      ? radarStatus.active_sites
      : radarStatus?.active_site ? [radarStatus.active_site] : ['KILN']
  ), [radarStatus]);

  const primarySite = activeSites[0];

  // ── Fetch sites list + gate filter on mount ──────────────────────────────
  useEffect(() => {
    fetch(apiUrl('/api/radar/sites')).then(r => r.json()).then(setSites).catch(() => {});
    fetch(apiUrl('/api/radar/gate')).then(r => r.json()).then(d => setGateDbz(d.gate_dbz ?? 10)).catch(() => {});
    // Check MRMS availability (requires pygrib on the backend)
    fetch(apiUrl('/api/mrms/status'))
      .then(r => r.json())
      .then(d => setMrmsReady(d.available && d.has_data))
      .catch(() => {});
  }, []);

  // ── Load / refresh MRMS history (parallel fetch of all cached frames) ────
  const loadMRMSHistory = useCallback(async (atLiveEdge = true) => {
    if (!mrmsReady) return;
    setMrmsLoading(true);
    try {
      const metaRes = await fetch(apiUrl('/api/mrms/frames'));
      if (!metaRes.ok) return;
      const meta: { ts: string; iso: string }[] = await metaRes.json();
      if (meta.length === 0) return;

      // Parallel fetch of all frames (frames compress well, typically ~1-2 MB each)
      const results = await Promise.all(
        meta.map(async ({ ts, iso }) => {
          try {
            const r = await fetch(apiUrl(`/api/mrms/frame/${ts}`));
            if (!r.ok) return null;
            const frame = parseMRMSBinary(await r.arrayBuffer());
            frame.timestamp = iso;  // attach ISO time for scrubber display
            return frame;
          } catch { return null; }
        })
      );
      const frames = results.filter((f): f is MRMSFrame => f !== null);
      if (frames.length === 0) return;

      setMrmsHistory(frames);
      // Jump to newest frame unless the user is mid-scrub
      if (atLiveEdge) setMrmsIndex(frames.length - 1);
    } catch {}
    finally { setMrmsLoading(false); }
  }, [mrmsReady]);

  const fetchMRMS = useCallback(async () => { await loadMRMSHistory(true); }, [loadMRMSHistory]);

  // Initial load + 2-min refresh when COMP is on
  useEffect(() => {
    if (!showMRMS || !mrmsReady) return;
    loadMRMSHistory(true);
    const t = setInterval(() => loadMRMSHistory(mrmsIndex >= mrmsHistory.length - 1), 120_000);
    return () => clearInterval(t);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showMRMS, mrmsReady]);

  // Animation ticker for MRMS loop
  useEffect(() => {
    if (!mrmsAnimating || mrmsHistory.length < 2) return;
    const t = setInterval(() => {
      setMrmsIndex(i => (i + 1) % mrmsHistory.length);
    }, 500);
    return () => clearInterval(t);
  }, [mrmsAnimating, mrmsHistory.length]);

  // Push displayed MRMS frame to the WebGL layer
  useEffect(() => {
    const frame = mrmsHistory[mrmsIndex] ?? null;
    const layer = mrmsLayerRef.current;
    const map   = mapRef.current?.getMap();
    if (layer) { layer.setFrame(frame); if (map) map.triggerRepaint(); }
  }, [mrmsIndex, mrmsHistory]);

  // ── Track focused cell from App level ────────────────────────────────────
  useEffect(() => {
    if (!focusedCellId) return;
    const cell = stormCells.find(c => c.cell_id === focusedCellId);
    setSelectedCell(cell ?? null);
    if (cell && mapRef.current) {
      mapRef.current.flyTo({ center: [cell.lon, cell.lat], zoom: Math.max(mapRef.current.getZoom(), 9), duration: 1000 });
    }
  }, [focusedCellId, stormCells]);

  // ── Manage MapLibre custom layers (one per active site) ──────────────────
  useEffect(() => {
    if (!mapLoaded) return;
    const map = mapRef.current?.getMap();
    if (!map) return;

    // Insert radar before the first symbol (label) layer so city names stay on top
    const firstSymbolId = map.getStyle().layers.find(
      (l: { type: string }) => l.type === 'symbol'
    )?.id;

    // MRMS WebGL layer: add/remove based on showMRMS toggle
    const mrmsId = 'mrms-composite';
    if (showMRMS && !mrmsLayerRef.current) {
      const mrmsLayer = new MRMSGLLayer(mrmsId);
      mrmsLayer.setOpacity(opacity);
      map.addLayer(mrmsLayer as unknown as Parameters<typeof map.addLayer>[0], firstSymbolId);
      mrmsLayerRef.current = mrmsLayer;
      setTimeout(() => fetchMRMS(), 0);
    } else if (!showMRMS && mrmsLayerRef.current) {
      if (map.getLayer(mrmsId)) map.removeLayer(mrmsId);
      mrmsLayerRef.current = null;
    }

    // NEXRAD layers — only present when showNexrad is true.  When the
    // operator toggles NEXRAD off (e.g. to view the MRMS composite alone),
    // we tear the layers down completely rather than just hiding them so
    // the GL state and texture memory don't accumulate per toggle.  Re-
    // adding the layers (toggle back on) automatically gets the latest
    // frame pushed in via the displayedFrames effect, which now depends
    // on showNexrad too.
    if (showNexrad) {
      for (const site of activeSites) {
        if (!layerRefs.current[site]) {
          const layer = new RadarGLLayer(`radar-${site}`);
          layer.setOpacity(opacity);
          map.addLayer(layer as unknown as Parameters<typeof map.addLayer>[0], firstSymbolId);
          layerRefs.current[site] = layer;
        }
      }
      // Remove layers for sites no longer active
      for (const site of Object.keys(layerRefs.current)) {
        if (!activeSites.includes(site)) {
          const layer = layerRefs.current[site];
          if (map.getLayer(layer.id)) map.removeLayer(layer.id);
          delete layerRefs.current[site];
        }
      }
    } else {
      // Toggled off — remove every NEXRAD layer.
      for (const site of Object.keys(layerRefs.current)) {
        const layer = layerRefs.current[site];
        if (map.getLayer(layer.id)) map.removeLayer(layer.id);
        delete layerRefs.current[site];
      }
    }
  }, [activeSites, mapLoaded, opacity, showMRMS, showNexrad, fetchMRMS]);

  // Remove all layers on unmount
  useEffect(() => {
    return () => {
      const map = mapRef.current?.getMap();
      for (const layer of Object.values(layerRefs.current)) {
        if (map?.getLayer(layer.id)) map.removeLayer(layer.id);
      }
      layerRefs.current = {};
    };
  }, []);

  // ── Sync opacity, smooth, gateDbz to all layers immediately ─────────────
  useEffect(() => {
    const map = mapRef.current?.getMap();
    for (const layer of Object.values(layerRefs.current)) { layer.setOpacity(opacity); }
    mrmsLayerRef.current?.setOpacity(opacity);
    if (map) map.triggerRepaint();
  }, [opacity]);

  useEffect(() => {
    const map = mapRef.current?.getMap();
    for (const layer of Object.values(layerRefs.current)) { layer.setSmooth(smooth); }
    if (map) map.triggerRepaint();
  }, [smooth]);

  useEffect(() => {
    const map = mapRef.current?.getMap();
    // Client-side: update shader uniform instantly for immediate visual feedback
    for (const [site, layer] of Object.entries(layerRefs.current)) {
      const frame = displayedFrames[site];
      layer.setGateDbz(gateDbz, frame);
    }
    mrmsLayerRef.current?.setGateDbz(gateDbz);
    if (map) map.triggerRepaint();
    // Server-side: persist the value so future downloads use the same threshold
    fetch(apiUrl('/api/radar/gate'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate_dbz: gateDbz }),
    }).catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gateDbz]);

  // ── Fetch binary frame history on product/sites change ───────────────────
  useEffect(() => {
    const load = async () => {
      const next: Record<string, SiteAnimState> = {};
      for (const site of activeSites) {
        try {
          const metaRes = await fetch(apiUrl(`/api/radar/frames/${activeProduct}?count=${HISTORY_COUNT}&site=${site}`));
          if (!metaRes.ok) continue;
          const metaList: RadarFrame[] = await metaRes.json();
          const frames: RadarBinaryFrame[] = [];
          for (const meta of metaList) {
            if (!meta.frame_id) continue;
            try {
              const binRes = await fetch(apiUrl(`/api/radar/binary/${site}/${activeProduct}/${meta.frame_id}`));
              if (!binRes.ok) continue;
              frames.push(parseRadarBinaryFrame(await binRes.arrayBuffer()));
            } catch {}
          }
          if (frames.length > 0) {
            next[site] = { history: frames, index: frames.length - 1, animating: siteAnim[site]?.animating ?? false };
          }
        } catch {}
      }
      if (Object.keys(next).length > 0) setSiteAnim(next);
    };
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeProduct, activeSites.join(',')]);

  // ── Append live frames ────────────────────────────────────────────────────
  // Within a single NEXRAD volume scan the timestamp (VCP start time) is
  // constant, but our backend's chunks pipeline broadcasts multiple times per
  // volume as more chunks arrive: partial → refresh → refresh → complete.
  // Each broadcast carries the SAME timestamp but progressively more tilts.
  // If we dedup by timestamp we lock to the first (least-complete) version
  // and never see the refreshes — effectively one update per VCP (~5 min).
  // Replacing in place picks up every refresh as soon as it arrives (~60-90s).
  useEffect(() => {
    if (!radarFrame || radarFrame.product !== activeProduct) return;
    setSiteAnim(prev => {
      const entry = prev[radarFrame.site] ?? { history: [], index: 0, animating: false };
      const existingIdx = entry.history.findIndex(f => f.timestamp === radarFrame.timestamp);
      if (existingIdx >= 0) {
        // Same-volume refresh: swap in the more-complete frame at its
        // existing slot.  Index and animation state unchanged.
        const history = entry.history.slice();
        history[existingIdx] = radarFrame;
        return { ...prev, [radarFrame.site]: { ...entry, history } };
      }
      const history = [...entry.history, radarFrame].slice(-HISTORY_COUNT);
      return { ...prev, [radarFrame.site]: { history, index: entry.animating ? entry.index : history.length - 1, animating: entry.animating } };
    });
  }, [radarFrame, activeProduct]);

  useEffect(() => {
    const incoming = Object.values(radarFrames).filter(f => f.product === activeProduct);
    if (!incoming.length) return;
    setSiteAnim(prev => {
      const next = { ...prev };
      for (const f of incoming) {
        const cur = next[f.site];
        const existingIdx = cur?.history.findIndex(h => h.timestamp === f.timestamp) ?? -1;
        if (existingIdx >= 0) {
          // Same-volume refresh — replace in place.
          const history = cur.history.slice();
          history[existingIdx] = f;
          next[f.site] = { ...cur, history };
        } else {
          const existing = cur?.history ?? [];
          const history = [...existing, f].slice(-HISTORY_COUNT);
          next[f.site] = { history, index: history.length - 1, animating: cur?.animating ?? false };
        }
      }
      return next;
    });
  }, [radarFrames, activeProduct]);

  // ── Animation ticker ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!Object.values(siteAnim).some(s => s.animating && s.history.length > 1)) return;
    const t = setInterval(() => {
      setSiteAnim(prev => {
        const out: Record<string, SiteAnimState> = {};
        for (const [site, s] of Object.entries(prev)) {
          out[site] = s.animating && s.history.length > 1
            ? { ...s, index: (s.index + 1) % s.history.length } : s;
        }
        return out;
      });
    }, ANIM_INTERVAL_MS);
    return () => clearInterval(t);
  }, [siteAnim]);

  // ── Push displayed frames to MapLibre custom layers ───────────────────────
  const displayedFrames = useMemo<Record<string, RadarBinaryFrame | null>>(() => {
    const out: Record<string, RadarBinaryFrame | null> = {};
    for (const site of activeSites) {
      const s = siteAnim[site];
      out[site] = s?.history[s.index] ?? null;
    }
    return out;
  }, [siteAnim, activeSites]);

  useEffect(() => {
    const map = mapRef.current?.getMap();
    for (const [site, frame] of Object.entries(displayedFrames)) {
      const layer = layerRefs.current[site];
      if (layer) { layer.setFrame(frame); }
    }
    if (map) map.triggerRepaint();
    // showNexrad participates so toggling NEXRAD back on re-runs this effect
    // immediately after the layer-management effect re-adds the layers,
    // pushing the latest frame without waiting for the next scan.
  }, [displayedFrames, showNexrad]);

  // ── Site management ───────────────────────────────────────────────────────


  const handleReplaceSite = useCallback(async (siteId: string) => {
    // Immediately show any cached frame so the map isn't blank while downloading
    try {
      const res = await fetch(apiUrl(`/api/radar/cached/${siteId}`));
      if (res.ok) {
        const frame = parseRadarBinaryFrame(await res.arrayBuffer());
        setSiteAnim({ [siteId]: { history: [frame], index: 0, animating: false } });
      }
    } catch {}

    // Trigger the backend to download + process the latest scan for this site
    try {
      await fetch(apiUrl('/api/radar/site'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_id: siteId }),
      });
    } catch {}
  }, []);

  const handleAlertClick = useCallback((alert: Alert) => {
    if (!alert.polygon?.length || !sites.length) return;
    const [lat, lon] = alert.polygon[0];
    let nearestSite = sites[0]; let minDist = Infinity;
    for (const s of sites) { const d = distanceKm(lat, lon, s.lat, s.lon); if (d < minDist) { minDist = d; nearestSite = s; } }
    handleReplaceSite(nearestSite.id);
  }, [sites, handleReplaceSite]);

  // ── Alert filter helpers ──────────────────────────────────────────────────
  const toggleAlertType = (p: string) => {
    setAlertOverlay(prev => { const n = new Set(prev); n.has(p) ? n.delete(p) : n.add(p); return n; });
  };
  const toggleAnim = (site: string) => setSiteAnim(prev => { const e = prev[site]; if (!e) return prev; return { ...prev, [site]: { ...e, animating: !e.animating } }; });
  const stepSite = (site: string, delta: number) => setSiteAnim(prev => { const e = prev[site]; if (!e || !e.history.length) return prev; return { ...prev, [site]: { ...e, index: (e.index + delta + e.history.length) % e.history.length, animating: false } }; });
  const setSiteIndex = (site: string, index: number) => setSiteAnim(prev => { const e = prev[site]; if (!e) return prev; return { ...prev, [site]: { ...e, index, animating: false } }; });

  // ESC dismisses the site dots overlay
  useEffect(() => {
    if (!showSiteDots) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setShowSiteDots(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showSiteDots]);

  const onMoveEnd = useCallback((e: ViewStateChangeEvent) => {
    localStorage.setItem('radarMapState', JSON.stringify({
      center: [e.viewState.latitude, e.viewState.longitude],
      zoom: e.viewState.zoom,
    }));
  }, []);

  // ── Derived data ──────────────────────────────────────────────────────────
  const sortedCells   = useMemo(() => [...stormCells].sort((a, b) => b.severity_score - a.severity_score), [stormCells]);
  const mapCells      = useMemo(() => stormCells.filter(c => c.severity_score >= MIN_SCORE_FOR_MAP), [stormCells]);
  const alertPolygons = useMemo(() => alerts.filter(a => (a.polygon?.length ?? 0) >= 3 && alertOverlay.has(a.phenomenon) && a.significance !== 'A'), [alerts, alertOverlay]);
  const primaryFrame  = displayedFrames[primarySite] ?? null;
  const topThreat     = sortedCells[0];

  // GeoJSON for alert polygons
  const alertGeoJSON = useMemo(() => ({
    type: 'FeatureCollection' as const,
    features: alertPolygons.map(a => ({
      type: 'Feature' as const,
      geometry: { type: 'Polygon' as const, coordinates: [a.polygon!.map(p => [p[1] ?? p[0], p[0] ?? p[1]])] },
      properties: { color: getAlertStyle(a.phenomenon, a.significance).backgroundColor, id: a.product_id },
    })),
  }), [alertPolygons]);

  // GeoJSON for all NEXRAD site locations (shown when showSiteDots is true)
  const siteDotsGeoJSON = useMemo(() => ({
    type: 'FeatureCollection' as const,
    features: sites.map(s => ({
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] },
      properties: { id: s.id, name: s.name, active: activeSites.includes(s.id) },
    })),
  }), [sites, activeSites]);

  // GeoJSON for lightning
  const lightningGeoJSON = useMemo(() => {
    if (!showLightning) return { type: 'FeatureCollection' as const, features: [] };
    const now = Date.now();
    return {
      type: 'FeatureCollection' as const,
      features: lightningFlashes.map((f, i) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [f.lon, f.lat] },
        properties: { age: Math.min(1, (now - new Date(f.timestamp).getTime()) / (15 * 60 * 1000)), id: i },
      })),
    };
  }, [lightningFlashes, showLightning]);

  return (
    <div className="radar-v2">
      <div className="radar-v2-map">
        <MapGL
          ref={mapRef}
          initialViewState={initialViewState}
          style={{ width: '100%', height: '100%' }}
          mapStyle={MAP_STYLE}
          onLoad={() => setMapLoaded(true)}
          onMoveEnd={onMoveEnd}
          attributionControl={false}
          onClick={(e: MapLayerMouseEvent) => {
            const features = e.features ?? [];
            // Site dot click — select that site and hide dots
            const siteFeat = features.find(f => f.layer?.id === 'radar-site-dots');
            if (siteFeat?.properties?.id) {
              handleReplaceSite(siteFeat.properties.id);
              setShowSiteDots(false);
              return;
            }
            // Alert polygon click
            const alertFeature = features.find(f => f.layer?.id === 'alert-fill');
            if (alertFeature) {
              const alert = alertPolygons.find(a => a.product_id === alertFeature.properties?.id);
              if (alert) handleAlertClick(alert);
            }
          }}
          interactiveLayerIds={['alert-fill', 'radar-site-dots']}
        >
          {/* Alert polygons */}
          <Source id="alerts" type="geojson" data={alertGeoJSON}>
            <Layer
              id="alert-fill"
              type="fill"
              paint={{ 'fill-color': ['get', 'color'], 'fill-opacity': 0.15 }}
            />
            <Layer
              id="alert-outline"
              type="line"
              paint={{ 'line-color': ['get', 'color'], 'line-width': 2, 'line-opacity': 0.9 }}
            />
          </Source>

          {/* Lightning */}
          <Source id="lightning" type="geojson" data={lightningGeoJSON}>
            <Layer
              id="lightning-layer"
              type="circle"
              paint={{
                'circle-radius': ['interpolate', ['linear'], ['get', 'age'], 0, 5, 1, 3],
                'circle-color': '#ffffff',
                'circle-stroke-color': '#ffe066',
                'circle-stroke-width': 1,
                'circle-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.9, 1, 0.05],
                'circle-stroke-opacity': ['interpolate', ['linear'], ['get', 'age'], 0, 0.9, 1, 0.05],
              }}
            />
          </Source>

          {/* MRMS WebGL layer is managed imperatively in the layer lifecycle effect */}

          {/* NEXRAD site dots (visible when showSiteDots toggle is on) */}
          {showSiteDots && (
            <Source id="radar-sites" type="geojson" data={siteDotsGeoJSON}>
              <Layer
                id="radar-site-dots"
                type="circle"
                paint={{
                  'circle-radius': ['case', ['get', 'active'], 8, 5],
                  'circle-color': ['case', ['get', 'active'], '#22d3ee', 'rgba(255,255,255,0.75)'],
                  'circle-stroke-color': ['case', ['get', 'active'], '#ffffff', 'rgba(255,255,255,0.4)'],
                  'circle-stroke-width': ['case', ['get', 'active'], 2, 1],
                }}
              />
              <Layer
                id="radar-site-labels"
                type="symbol"
                layout={{
                  'text-field': ['get', 'id'],
                  'text-size': 10,
                  'text-offset': [0, 1.4],
                  'text-font': ['Open Sans Bold', 'Arial Unicode MS Bold'],
                  'text-anchor': 'top',
                }}
                paint={{
                  'text-color': ['case', ['get', 'active'], '#22d3ee', '#ffffff'],
                  'text-halo-color': '#000000',
                  'text-halo-width': 1.5,
                }}
              />
            </Source>
          )}

          {/* Storm cell markers */}
          {mapCells.map(cell => (
            <Marker
              key={cell.cell_id}
              longitude={cell.lon}
              latitude={cell.lat}
              anchor="center"
              onClick={() => setSelectedCell(cell)}
            >
              <CellIcon cell={cell} selected={selectedCell?.cell_id === cell.cell_id} />
            </Marker>
          ))}
        </MapGL>
      </div>

      {/* Top-left intentionally empty — site selection is via bottom-left Sites toggle */}

      {/* ── TOP RIGHT: Product toolbar + controls ── */}
      <div className="rc-panel rc-topright">
        <div className="rc-product-pill">
          {/* NEXRAD visibility toggle — hide the per-site Doppler layer
              (useful when MRMS composite is active and the operator wants
              a clean composite view).  Product selector keeps working
              when NEXRAD is hidden; toggling back on shows the latest
              frame for the selected product immediately. */}
          <button
            className={`rc-product-btn ${showNexrad ? 'active' : ''}`}
            title={showNexrad ? 'Hide NEXRAD radar layer' : 'Show NEXRAD radar layer'}
            onClick={() => setShowNexrad(v => !v)}
          >
            <i className={`fa ${showNexrad ? 'fa-eye' : 'fa-eye-slash'}`} />
          </button>
          {PRODUCTS.map(p => (
            <button key={p} className={`rc-product-btn ${activeProduct === p && showNexrad ? 'active' : ''}`} onClick={() => { setActiveProduct(p); if (!showNexrad) setShowNexrad(true); }} title={RADAR_PRODUCT_LABELS[p]} disabled={false}>
              {RADAR_PRODUCT_SHORT[p]}
            </button>
          ))}
          {mrmsReady && (
            <button
              className={`rc-product-btn ${showMRMS ? 'active' : ''}`}
              title="MRMS National Composite Reflectivity (updates every ~2 min)"
              onClick={() => setShowMRMS(v => !v)}
            >
              COMP
            </button>
          )}
        </div>

        {/* Opacity – click-toggle panel */}
        <div className="rc-ctrl-group">
          <button
            className={`rc-icon-btn ${opacityOpen ? 'active' : ''}`}
            title={`Opacity ${Math.round(opacity * 100)}%`}
            onClick={() => { setOpacityOpen(v => !v); setGateOpen(false); setSmoothOpen(false); }}
          ><i className="fa fa-adjust" /></button>
          {opacityOpen && (
            <div className="rc-ctrl-panel">
              <label>Opacity <span>{Math.round(opacity * 100)}%</span></label>
              <input type="range" min={0} max={100} value={Math.round(opacity * 100)}
                onChange={e => setOpacity(parseInt(e.target.value) / 100)} />
            </div>
          )}
        </div>

        {/* Gate filter – click-toggle, now 100% client-side / instant */}
        {activeProduct === 'reflectivity' && (
          <div className="rc-ctrl-group">
            <button
              className={`rc-icon-btn ${gateOpen ? 'active' : ''}`}
              title={`Gate filter ${gateDbz} dBZ`}
              onClick={() => { setGateOpen(v => !v); setOpacityOpen(false); setSmoothOpen(false); }}
            ><i className="fa fa-filter" /></button>
            {gateOpen && (
              <div className="rc-ctrl-panel">
                <label>Gate Filter <span>{gateDbz} dBZ</span></label>
                <input type="range" min={-20} max={40} step={1} value={gateDbz}
                  onChange={e => setGateDbz(parseInt(e.target.value))} />
              </div>
            )}
          </div>
        )}

        {/* Smoothing – WeatherWise-style configurable Gaussian */}
        <div className="rc-ctrl-group">
          <button
            className={`rc-icon-btn ${smoothOpen ? 'active' : ''}`}
            title={`Smoothing ${Math.round(smooth * 100)}%`}
            onClick={() => { setSmoothOpen(v => !v); setGateOpen(false); setOpacityOpen(false); }}
          ><i className="fa fa-magic" /></button>
          {smoothOpen && (
            <div className="rc-ctrl-panel">
              <label>Smoothing <span>{Math.round(smooth * 100)}%</span></label>
              <input type="range" min={0} max={100} value={Math.round(smooth * 100)}
                onChange={e => setSmooth(parseInt(e.target.value) / 100)} />
            </div>
          )}
        </div>

        <div className="rc-layer-toggles">
          {RADAR_ALERT_TOGGLES.map(({ phenomenon, label, title }) => (
            <button key={phenomenon} title={title} className={`rc-layer-toggle ${alertOverlay.has(phenomenon) ? 'active' : ''}`} style={{ '--toggle-color': getAlertStyle(phenomenon, 'W').backgroundColor } as React.CSSProperties} onClick={() => toggleAlertType(phenomenon)}>
              {label}
            </button>
          ))}
          <button title="GOES-16 GLM Lightning (15 min)" className={`rc-layer-toggle ${showLightning ? 'active' : ''}`} style={{ '--toggle-color': '#ffe066' } as React.CSSProperties} onClick={() => setShowLightning(v => !v)}>⚡</button>
        </div>
      </div>

      {/* ── MCS strip ── */}
      {mcsSystems.length > 0 && (
        <div className="rc-mcs-strip">
          {mcsSystems.map(sys => (
            <div key={sys.system_id} className={`rc-mcs-chip mcs-type-${sys.system_type}`}>
              <i className="fa fa-align-justify" />
              <span>{MCS_TYPE_LABELS[sys.system_type]} · {sys.length_km.toFixed(0)} km</span>
              {sys.bow_echo_detected && <span className="rc-mcs-tag bow">BOW</span>}
              {sys.rear_inflow_notch && <span className="rc-mcs-tag rni">RNI</span>}
              {sys.book_end_vortices && <span className="rc-mcs-tag bev">BEV</span>}
              {sys.embedded_qlcs_mesos > 0 && <span className="rc-mcs-tag qlcs">{sys.embedded_qlcs_mesos}× QLCS</span>}
            </div>
          ))}
        </div>
      )}

      {/* ── BOTTOM LEFT: WeatherWise-style info bar ── */}
      <div className="rc-infobar">
        <div className="rc-infobar-main">
          <button
            className={`rc-sites-toggle ${showSiteDots ? 'active' : ''}`}
            onClick={() => setShowSiteDots(v => !v)}
            title={showSiteDots ? 'Hide radar sites' : 'Show all radar sites on map'}
          >
            <i className="fa fa-satellite-dish" />
          </button>
          <div className="rc-infobar-site">{primarySite}</div>
          <span className="rc-infobar-sep">·</span>
          <div className="rc-infobar-product">{RADAR_PRODUCT_LABELS[activeProduct]}</div>
          {primaryFrame && (
            <>
              <span className="rc-infobar-sep">·</span>
              <div className="rc-infobar-time">
                {new Date(primaryFrame.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true })}
              </div>
              <div className={`rc-infobar-live ${radarStatus?.processing ? 'scanning' : ''}`}>
                {radarStatus?.processing ? <><i className="fa fa-spinner fa-spin" /> Scanning</> : '● Live'}
              </div>
            </>
          )}
          {radarStatus?.error && (
            <span className="rc-infobar-err" title={radarStatus.error}><i className="fa fa-exclamation-triangle" /></span>
          )}
        </div>
        {showSiteDots && (
          <div className="rc-infobar-hint">Click a site dot to switch · ESC to cancel</div>
        )}
      </div>

      {/* ── BOTTOM CENTER: Per-site scrubber + MRMS scrubber ── */}
      <div className="rc-panel rc-bottomcenter">
        <div className="rc-scrubber-stack">
          {activeSites.map(site => {
            const s = siteAnim[site];
            const history = s?.history ?? [];
            const idx = s?.index ?? 0;
            const frame = history[idx];
            return (
              <div key={site} className="rc-scrubber-row">
                <button className={`rc-anim-btn ${s?.animating ? 'active' : ''}`} onClick={() => toggleAnim(site)} disabled={history.length < 2} title={s?.animating ? 'Pause' : 'Play'}>
                  <i className={`fa fa-${s?.animating ? 'pause' : 'play'}`} />
                </button>
                <button className="rc-anim-btn" onClick={() => stepSite(site, -1)} disabled={history.length < 2} title="Previous frame"><i className="fa fa-step-backward" /></button>
                <span className="rc-scrub-site">{site}</span>
                <input type="range" className="rc-scrub-slider" min={0} max={Math.max(0, history.length - 1)} value={idx} onChange={e => setSiteIndex(site, parseInt(e.target.value))} disabled={history.length < 2} />
                <button className="rc-anim-btn" onClick={() => stepSite(site, 1)} disabled={history.length < 2} title="Next frame"><i className="fa fa-step-forward" /></button>
                <span className="rc-scrub-meta">
                  {history.length > 0 ? `${idx + 1}/${history.length}` : '0/0'}
                  {frame && <> · {new Date(frame.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>}
                </span>
              </div>
            );
          })}

          {/* MRMS history scrubber — shown when COMP is active */}
          {showMRMS && (
            <div className="rc-scrubber-row">
              <button
                className={`rc-anim-btn ${mrmsAnimating ? 'active' : ''}`}
                onClick={() => setMrmsAnimating(v => !v)}
                disabled={mrmsHistory.length < 2 || mrmsLoading}
                title={mrmsAnimating ? 'Pause' : 'Play'}
              >
                <i className={`fa fa-${mrmsLoading ? 'spinner fa-spin' : mrmsAnimating ? 'pause' : 'play'}`} />
              </button>
              <button className="rc-anim-btn" onClick={() => setMrmsIndex(i => Math.max(0, i - 1))} disabled={mrmsHistory.length < 2} title="Previous frame">
                <i className="fa fa-step-backward" />
              </button>
              <span className="rc-scrub-site" style={{ color: '#22d3ee' }}>COMP</span>
              <input
                type="range"
                className="rc-scrub-slider"
                min={0}
                max={Math.max(0, mrmsHistory.length - 1)}
                value={mrmsIndex}
                onChange={e => { setMrmsAnimating(false); setMrmsIndex(parseInt(e.target.value)); }}
                disabled={mrmsHistory.length < 2}
              />
              <button className="rc-anim-btn" onClick={() => setMrmsIndex(i => Math.min(mrmsHistory.length - 1, i + 1))} disabled={mrmsHistory.length < 2} title="Next frame">
                <i className="fa fa-step-forward" />
              </button>
              <span className="rc-scrub-meta">
                {mrmsLoading ? 'Loading…' : mrmsHistory.length > 0
                  ? `${mrmsIndex + 1}/${mrmsHistory.length}`
                  : '0/0'}
                {mrmsHistory[mrmsIndex] && (
                  <> · {new Date(mrmsHistory[mrmsIndex].timestamp ?? '').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>
                )}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ── RIGHT DRAWER: Storm cells ── */}
      <div className={`rc-drawer ${drawerOpen ? 'open' : ''}`}>
        <button className="rc-drawer-tab" onClick={() => setDrawerOpen(v => !v)} title={drawerOpen ? 'Hide cell panel' : 'Show cell panel'} style={topThreat ? { '--drawer-tab-color': THREAT_LEVEL_COLORS[topThreat.threat_level] } as React.CSSProperties : {}}>
          <i className={`fa fa-chevron-${drawerOpen ? 'right' : 'left'}`} />
          <span className="rc-drawer-tab-count">{stormCells.length}</span>
        </button>

        {drawerOpen && (
          <div className="rc-drawer-body">
            <div className="rc-drawer-header">
              <h3>Storm Cells <span className="rc-drawer-count">{stormCells.length}</span></h3>
            </div>

            {sortedCells.length === 0 ? (
              <div className="rc-drawer-empty">No cells tracked</div>
            ) : (
              <div className="rc-drawer-list">
                {sortedCells.map(cell => (
                  <div key={cell.cell_id} className={`rc-cell-item ${selectedCell?.cell_id === cell.cell_id ? 'selected' : ''} ${cell.mcs_system_id ? 'in-system' : ''}`} onClick={() => {
                    setSelectedCell(cell);
                    mapRef.current?.flyTo({ center: [cell.lon, cell.lat], zoom: Math.max(mapRef.current.getZoom(), 9), duration: 800 });
                  }}>
                    <div className="rc-cell-score" style={{ background: THREAT_LEVEL_COLORS[cell.threat_level] }}>{cell.severity_score}</div>
                    <div className="rc-cell-info">
                      <div className="rc-cell-id">{cell.cell_id}{cell.mcs_system_id && <span className="rc-cell-sys">SYS</span>}</div>
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
                      {cell.trend === 'strengthening' && '▲'}{cell.trend === 'weakening' && '▼'}{cell.trend === 'steady' && '—'}
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
                {selectedCell.mcs_system_id && <div className="rc-cell-detail-sys">Part of {MCS_TYPE_LABELS[mcsSystems.find(s => s.system_id === selectedCell.mcs_system_id)?.system_type ?? 'squall_line']}</div>}
                <div className="rc-detail-sections">
                  <DetailSection title="Structure">
                    <Row label="Max Refl" value={`${selectedCell.max_reflectivity_dbz.toFixed(1)} dBZ`} />
                    <Row label="Area" value={`${selectedCell.area_km2.toFixed(1)} km²`} />
                    {selectedCell.cell_top_km != null && <Row label="Cell Top" value={`${selectedCell.cell_top_km.toFixed(1)} km`} />}
                    {selectedCell.cell_base_km != null && <Row label="Cell Base" value={`${selectedCell.cell_base_km.toFixed(1)} km`} />}
                    {selectedCell.depth_km != null && <Row label="Depth" value={`${selectedCell.depth_km.toFixed(1)} km`} />}
                    {selectedCell.vil_kg_m2 != null && <Row label="VIL" value={`${selectedCell.vil_kg_m2.toFixed(1)} kg/m²`} />}
                  </DetailSection>
                  <DetailSection title="Rotation">
                    {selectedCell.rotation_detected && (
                      <Row
                        label="Mesocyclone"
                        value={
                          `${selectedCell.rotation_velocity_ms} m/s` +
                          (selectedCell.tvs_detected ? ' (TVS!)' : '') +
                          (selectedCell.low_level_meso_detected && selectedCell.mid_level_meso_detected
                            ? ' · low+mid-level'
                            : selectedCell.low_level_meso_detected
                              ? ' · LOW-LEVEL'
                              : selectedCell.mid_level_meso_detected
                                ? ' · mid-level only'
                                : '')
                        }
                        highlight={selectedCell.low_level_meso_detected || selectedCell.tvs_detected}
                      />
                    )}
                    {selectedCell.qlcs_meso_detected && !selectedCell.rotation_detected && <Row label="QLCS Meso" value={`${selectedCell.qlcs_meso_velocity_ms} m/s`} />}
                    {(selectedCell.max_rot_velocity_ms ?? 0) > 0 && (
                      <Row
                        label="Max Rotation"
                        value={
                          `${selectedCell.max_rot_velocity_ms} m/s` +
                          (selectedCell.max_rot_height_km != null ? ` @ ${selectedCell.max_rot_height_km} km` : '') +
                          (selectedCell.rotation_base_km != null
                            ? ` (base ${selectedCell.rotation_base_km.toFixed(1)} km)`
                            : '')
                        }
                      />
                    )}
                    {selectedCell.llsd_max_shear != null && <Row label="Low-Level Shear" value={`${(selectedCell.llsd_max_shear * 1000).toFixed(1)} × 10⁻³ /s${selectedCell.llsd_rotation_detected ? ' ⚠' : ''}`} />}
                    {selectedCell.p_rotation_model != null && (
                      <Row
                        label="ML p(rotation)"
                        value={`${selectedCell.p_rotation_model.toFixed(2)}${selectedCell.p_rotation_model >= 0.8 ? ' ⚠' : selectedCell.p_rotation_model < 0.1 ? ' ✓ low' : ''}`}
                      />
                    )}
                    {!selectedCell.rotation_detected && !selectedCell.qlcs_meso_detected && !selectedCell.llsd_rotation_detected && (selectedCell.max_rot_velocity_ms ?? 0) <= 0 && <div className="rc-detail-none">No rotation detected</div>}
                  </DetailSection>
                  <DetailSection title="Hazards">
                    {selectedCell.mesh_mm != null && selectedCell.mesh_mm >= 19 && (
                      <Row
                        label="MESH"
                        value={
                          `${selectedCell.mesh_mm.toFixed(0)} mm` +
                          (selectedCell.mesh_mm >= 76 ? ' · GIANT' :
                           selectedCell.mesh_mm >= 44 ? ' · LARGE' : '')
                        }
                        highlight={selectedCell.mesh_mm >= 44}
                      />
                    )}
                    {selectedCell.hail_indicated && !(selectedCell.mesh_mm != null && selectedCell.mesh_mm >= 19) && <Row label="Hail" value={`Indicated${selectedCell.hail_max_dbz ? ` (${selectedCell.hail_max_dbz.toFixed(0)} dBZ)` : ''}`} />}
                    {selectedCell.bwer_detected && (
                      <Row
                        label="BWER"
                        value={
                          'Bounded weak echo' +
                          (selectedCell.bwer_overhang_dbz ? ` (${selectedCell.bwer_overhang_dbz.toFixed(0)} dBZ overhang)` : '')
                        }
                        highlight
                      />
                    )}
                    {selectedCell.debris_signature && <Row label="TDS" value="Debris Detected" highlight />}
                    {!selectedCell.hail_indicated && !selectedCell.debris_signature && !selectedCell.bwer_detected && (selectedCell.mesh_mm == null || selectedCell.mesh_mm < 19) && <div className="rc-detail-none">No hazard signatures</div>}
                  </DetailSection>
                  <DetailSection title="Wind">
                    {selectedCell.max_wind_velocity_ms != null && (
                      <Row
                        label="Peak Outbound"
                        value={`${selectedCell.max_wind_velocity_ms.toFixed(0)} m/s (${(selectedCell.max_wind_velocity_ms * 1.944).toFixed(0)} kt)`}
                        highlight={selectedCell.straight_line_wind_detected}
                      />
                    )}
                    {selectedCell.straight_line_wind_detected && (
                      <Row
                        label="SEVERE swath"
                        value={`≥ 50 kt over ${'≥ 30'} km² ⚠`}
                        highlight
                      />
                    )}
                    {selectedCell.strong_wind_detected && !selectedCell.straight_line_wind_detected && (
                      <Row
                        label="Strong swath"
                        value={`≥ 35 kt over ${selectedCell.strong_wind_swath_km2?.toFixed(0) ?? '?'} km²`}
                      />
                    )}
                    {selectedCell.downburst_detected && (
                      <Row
                        label="Downburst"
                        value={selectedCell.downburst_delta_v_ms != null
                          ? `ΔV ${selectedCell.downburst_delta_v_ms.toFixed(0)} m/s`
                          : 'Divergent signature'}
                        highlight
                      />
                    )}
                    {selectedCell.rij_detected && (
                      <Row label="Rear-Inflow Jet" value="Bow echo wind threat" highlight />
                    )}
                    {!selectedCell.strong_wind_detected
                      && !selectedCell.straight_line_wind_detected
                      && !selectedCell.downburst_detected
                      && !selectedCell.rij_detected
                      && (selectedCell.max_wind_velocity_ms == null || selectedCell.max_wind_velocity_ms < 18) && (
                        <div className="rc-detail-none">No notable wind signature</div>
                    )}
                  </DetailSection>
                  <DetailSection title="Motion">
                    <Row label="Direction" value={`${selectedCell.motion_direction_deg.toFixed(0)}°`} />
                    <Row label="Speed" value={`${selectedCell.motion_speed_kph.toFixed(0)} km/h`} />
                    <Row label="Trend" value={selectedCell.trend} />
                  </DetailSection>
                </div>
                <div className="rc-cell-detail-meta">First seen: {new Date(selectedCell.first_detected).toLocaleTimeString()} · Scans: {selectedCell.scan_count}</div>
              </div>
            )}
          </div>
        )}
      </div>

    </div>
  );
}

// ── Storm cell icon for MapLibre Marker ──────────────────────────────────────

function CellIcon({ cell, selected }: { cell: StormCell; selected: boolean }) {
  const color = THREAT_LEVEL_COLORS[cell.threat_level];
  const size = 32 + Math.round((cell.severity_score / 100) * 16);
  return (
    <div
      style={{
        width: size, height: size, borderRadius: '50%',
        background: `radial-gradient(circle at 40% 35%, ${color}cc, ${color}99)`,
        border: `2px solid ${selected ? '#fff' : color}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        cursor: 'pointer', userSelect: 'none',
        boxShadow: selected ? `0 0 0 3px ${color}66` : undefined,
        fontSize: size < 38 ? 11 : 13, fontWeight: 700, color: '#fff',
        textShadow: '0 1px 2px #0008',
      }}
    >
      {cell.severity_score}
      {cell.rotation_detected && <span style={{ position: 'absolute', top: -4, right: -4, fontSize: 10 }}>↻</span>}
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Row({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className={`rc-detail-row ${highlight ? 'highlight' : ''}`}>
      <span className="rc-detail-label">{label}</span>
      <span className="rc-detail-value">{value}</span>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="rc-detail-section"><h5>{title}</h5>{children}</div>;
}



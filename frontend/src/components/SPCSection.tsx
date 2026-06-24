import React, { useEffect, useState, useCallback } from 'react';
import { MapContainer, TileLayer, GeoJSON, useMap } from 'react-leaflet';
import L from 'leaflet';
import type {
  OutlookData,
  MesoscaleDiscussion,
  Day1Response,
  MesoscaleDiscussionsResponse,
  StateImagesResponse,
} from '../types/spc';
import { RISK_COLORS, RISK_NAMES, RISK_ORDER, PROB_COLORS, PROB_NAMES, CIG_NAMES, getContrastColor } from '../types/spc';
import { apiUrl } from '../utils/api';
import 'leaflet/dist/leaflet.css';

interface DiscussionResponse {
  day: number;
  text: string;
  url: string;
  fetched_at: string;
}

// Component to fit map bounds to outlook data
const FitBoundsToOutlook: React.FC<{ geojson: GeoJSON.FeatureCollection | null }> = ({ geojson }) => {
  const map = useMap();

  useEffect(() => {
    if (geojson && geojson.features && geojson.features.length > 0) {
      try {
        const layer = L.geoJSON(geojson);
        const bounds = layer.getBounds();
        if (bounds.isValid()) {
          map.fitBounds(bounds, { padding: [50, 50] });
        }
      } catch {
        // Fallback to US center if bounds fail
        map.setView([39.8283, -98.5795], 4);
      }
    }
  }, [geojson, map]);

  return null;
};

// Style function for GeoJSON polygons
const getPolygonStyle = (feature: GeoJSON.Feature | undefined) => {
  if (!feature || !feature.properties) {
    return {
      weight: 2,
      opacity: 0.8,
      fillOpacity: 0.3,
      color: '#888888',
      fillColor: '#888888',
    };
  }

  const label = (feature.properties.LABEL || feature.properties.label || '').toUpperCase();
  const fillColor = feature.properties.fill || RISK_COLORS[label] || '#888888';
  const strokeColor = feature.properties.stroke || fillColor;

  return {
    weight: 2,
    opacity: 0.8,
    fillOpacity: 0.35,
    color: strokeColor,
    fillColor: fillColor,
    dashArray: '5, 5',
  };
};

// Style function for CIG (hatched) overlays
const getCIGPolygonStyle = (feature: GeoJSON.Feature | undefined) => {
  if (!feature || !feature.properties) {
    return { weight: 0, opacity: 0, fillOpacity: 0 };
  }

  return {
    weight: 2,
    opacity: 0.9,
    fillOpacity: 0.4,
    color: '#000000',
    fillColor: '#000000',
    dashArray: '8, 4',
    // CSS className triggers the hatching pattern
    className: 'cig-hatched',
  };
};

// Inject SVG hatching pattern into the DOM (once)
const injectHatchPattern = () => {
  if (document.getElementById('spc-hatch-pattern-svg')) return;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'spc-hatch-pattern-svg';
  svg.setAttribute('width', '0');
  svg.setAttribute('height', '0');
  svg.style.position = 'absolute';
  svg.innerHTML = `
    <defs>
      <pattern id="cig-hatch" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#000" stroke-width="2.5" stroke-opacity="0.7" />
      </pattern>
    </defs>
  `;
  document.body.appendChild(svg);
};

// Response type for Day 2 with probabilities (same structure as Day 1)
interface DayDataResponse {
  categorical: OutlookData | null;
  tornado?: OutlookData | null;
  wind?: OutlookData | null;
  hail?: OutlookData | null;
  cig_tornado?: OutlookData | null;
  cig_wind?: OutlookData | null;
  cig_hail?: OutlookData | null;
  risk_colors: Record<string, string>;
  risk_names: Record<string, string>;
}

// ── Mesoanalysis constants (module-level, no re-creation on render) ──────────
const MESO_SECTORS = [
  { id: '19', label: 'CONUS' },
  { id: '16', label: 'NE' },
  { id: '17', label: 'E. Coast' },
  { id: '20', label: 'Midwest' },
  { id: '21', label: 'Gt. Lakes' },
  { id: '18', label: 'SE' },
  { id: '15', label: 'S. Plains' },
  { id: '14', label: 'C. Plains' },
  { id: '13', label: 'N. Plains' },
  { id: '12', label: 'SW' },
  { id: '22', label: 'Gt. Basin' },
  { id: '11', label: 'NW' },
];

const MESO_CATEGORIES: Record<string, { label: string; icon: string; params: { id: string; label: string }[] }> = {
  composite: {
    label: 'Composite Parameters', icon: 'fa-layer-group',
    params: [
      { id: 'scp',   label: 'Supercell Composite' },
      { id: 'stpc',  label: 'STP (Fixed Layer)' },
      { id: 'stpc5', label: 'STP (Eff. Layer)' },
      { id: 'sigh',  label: 'Sig. Hail Param.' },
      { id: 'cpsh',  label: 'Cond. Prob. Sig. Hail' },
      { id: 'qlcs1', label: 'QLCS Tornado Param.' },
    ],
  },
  instability: {
    label: 'Instability', icon: 'fa-bolt',
    params: [
      { id: 'sbcp', label: 'SBCAPE' },
      { id: 'mucp', label: 'MUCAPE' },
      { id: 'mlcp', label: 'MLCAPE' },
      { id: 'ncap', label: 'Norm. CAPE' },
      { id: 'dcap', label: 'DCAPE' },
      { id: 'mbcp', label: 'Mixed Layer CAPE/CINH' },
    ],
  },
  shear: {
    label: 'Shear & Helicity', icon: 'fa-wind',
    params: [
      { id: 'eshr', label: 'Effective Shear' },
      { id: 'shr6', label: '0-6 km Shear' },
      { id: 'srh1', label: '0-1 km SRH' },
      { id: 'srh3', label: '0-3 km SRH' },
      { id: 'brns', label: 'BRN Shear' },
      { id: 'effh', label: 'Eff. SRH' },
    ],
  },
  thermo: {
    label: 'Thermodynamics', icon: 'fa-thermometer-half',
    params: [
      { id: 'lllr', label: '0-3 km Lapse Rate' },
      { id: 'mllr', label: '3-6 km Lapse Rate' },
      { id: 'lcls', label: 'LCL Height' },
      { id: 'lclp', label: 'LCL (surface parcel)' },
      { id: 'thea', label: 'Theta-E' },
      { id: 'thet', label: 'Theta (850 mb)' },
    ],
  },
  surface: {
    label: 'Surface & Upper Air', icon: 'fa-map-marked-alt',
    params: [
      { id: 'sfc',  label: 'Surface Analysis' },
      { id: 'ttd',  label: 'Temp / Dewpoint' },
      { id: 'sfct', label: 'Surface Temp' },
      { id: 'stor', label: 'Storm Reports' },
      { id: 'pwtr', label: 'Precipitable Water' },
      { id: 'mixr', label: 'Mixing Ratio' },
    ],
  },
};

interface MesoanalysisPanelProps {
  sector: string;
  category: string;
  onSectorChange: (s: string) => void;
  onCategoryChange: (c: string) => void;
  onLightbox: (url: string, label: string) => void;
}

const MesoanalysisPanel: React.FC<MesoanalysisPanelProps> = ({
  sector, category, onSectorChange, onCategoryChange, onLightbox,
}) => {
  const cat = MESO_CATEGORIES[category] ?? MESO_CATEGORIES.composite;

  return (
    <div style={{ flex: 1, overflowY: 'auto', paddingTop: '8px' }}>
      {/* Header row: title + sector buttons */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap', gap: '8px' }}>
        <h3 style={{ margin: 0, fontSize: '1rem', color: 'var(--text-primary)' }}>
          <i className="fas fa-layer-group" style={{ marginRight: '6px', color: 'var(--primary-color)' }}></i>
          SPC Mesoanalysis
        </h3>
        <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
          {MESO_SECTORS.map(s => (
            <button
              key={s.id}
              onClick={() => onSectorChange(s.id)}
              style={{
                padding: '3px 8px', borderRadius: '4px', fontSize: '0.75rem',
                border: '1px solid var(--border-color)', cursor: 'pointer',
                backgroundColor: sector === s.id ? 'var(--primary-color)' : 'var(--bg-secondary)',
                color: sector === s.id ? 'white' : 'var(--text-secondary)',
              }}
            >{s.label}</button>
          ))}
        </div>
      </div>

      {/* Category tabs */}
      <div style={{ display: 'flex', gap: '6px', marginBottom: '12px', flexWrap: 'wrap' }}>
        {Object.entries(MESO_CATEGORIES).map(([key, c]) => (
          <button
            key={key}
            onClick={() => onCategoryChange(key)}
            style={{
              padding: '5px 10px', borderRadius: '4px', fontSize: '0.8rem',
              border: '1px solid var(--border-color)', cursor: 'pointer',
              backgroundColor: category === key ? 'var(--primary-color)' : 'var(--bg-secondary)',
              color: category === key ? 'white' : 'var(--text-primary)',
            }}
          >
            <i className={`fas ${c.icon}`} style={{ marginRight: '4px' }}></i>{c.label}
          </button>
        ))}
      </div>

      {/* Image grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '10px' }}>
        {cat.params.map(p => {
          const url = apiUrl(`/api/proxy/mesoanalysis?sector=${sector}&param=${p.id}`);
          return (
            <div
              key={p.id}
              style={{ backgroundColor: 'var(--bg-secondary)', borderRadius: '6px', border: '1px solid var(--border-color)', overflow: 'hidden', cursor: 'pointer' }}
              onClick={() => onLightbox(url, p.label)}
            >
              <div style={{ padding: '6px 8px', fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-secondary)', borderBottom: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span>{p.label}</span>
                <i className="fas fa-expand-alt" style={{ fontSize: '0.7rem', opacity: 0.5 }}></i>
              </div>
              <img
                src={url}
                alt={p.label}
                style={{ width: '100%', display: 'block', backgroundColor: '#f0f0f0' }}
                onError={e => { (e.target as HTMLImageElement).style.opacity = '0.3'; }}
              />
            </div>
          );
        })}
      </div>

      <p style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginTop: '8px', textAlign: 'right' }}>
        Images from <a href="https://www.spc.noaa.gov/exper/mesoanalysis/" target="_blank" rel="noreferrer" style={{ color: 'var(--primary-color)' }}>SPC Mesoanalysis</a> · Click any image to expand
      </p>
    </div>
  );
};

export const SPCSection: React.FC = () => {
  const [activeDay, setActiveDay] = useState<1 | 2 | 3>(1);
  const [activeTab, setActiveTab] = useState<'categorical' | 'tornado' | 'wind' | 'hail'>('categorical');
  const [day1Data, setDay1Data] = useState<Day1Response | null>(null);
  const [day2Data, setDay2Data] = useState<DayDataResponse | null>(null);
  const [currentOutlook, setCurrentOutlook] = useState<OutlookData | null>(null);
  const [mesoscaleDiscussions, setMesoscaleDiscussions] = useState<MesoscaleDiscussion[]>([]);
  const [stateImages, setStateImages] = useState<StateImagesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedMD, setSelectedMD] = useState<MesoscaleDiscussion | null>(null);
  const [discussionText, setDiscussionText] = useState<string | null>(null);
  const [showMesoanalysis, setShowMesoanalysis] = useState(false);
  const [mesoSector, setMesoSector] = useState('19');
  const [mesoCategory, setMesoCategory] = useState('composite');
  const [mesoLightbox, setMesoLightbox] = useState<{ url: string; label: string } | null>(null);
  const [discussionExpanded, setDiscussionExpanded] = useState(false);
  const [expandedImage, setExpandedImage] = useState<{ state: string; url: string } | null>(null);
  const [currentCIG, setCurrentCIG] = useState<OutlookData | null>(null);

  // Default center: US
  const defaultCenter: L.LatLngExpression = [39.8283, -98.5795];
  const defaultZoom = 4;

  // Inject SVG hatch pattern on mount
  useEffect(() => {
    injectHatchPattern();
  }, []);

  // Fetch Day 1 data with probabilities
  const fetchDay1Data = useCallback(async (refresh = false) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/day1?include_probabilities=true&refresh=${refresh}`));
      if (!response.ok) throw new Error('Failed to fetch SPC data');
      const data: Day1Response = await response.json();
      setDay1Data(data);

      // Set initial outlook and CIG overlay based on active tab
      if (activeTab === 'categorical' && data.categorical) {
        setCurrentOutlook(data.categorical);
        setCurrentCIG(null);
      } else if (activeTab === 'tornado' && data.tornado) {
        setCurrentOutlook(data.tornado);
        setCurrentCIG(data.cig_tornado || null);
      } else if (activeTab === 'wind' && data.wind) {
        setCurrentOutlook(data.wind);
        setCurrentCIG(data.cig_wind || null);
      } else if (activeTab === 'hail' && data.hail) {
        setCurrentOutlook(data.hail);
        setCurrentCIG(data.cig_hail || null);
      } else if (data.categorical) {
        setCurrentOutlook(data.categorical);
        setCurrentCIG(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch SPC data');
    }
  }, [activeTab]);

  // Fetch Day 2 data with probabilities
  const fetchDay2Data = useCallback(async (refresh = false) => {
    try {
      // Fetch all Day 2 outlooks + CIG in parallel
      const [catRes, tornRes, windRes, hailRes, cigTornRes, cigWindRes, cigHailRes] = await Promise.all([
        fetch(apiUrl(`/api/spc/outlook/day2_categorical?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_tornado?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_wind?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_hail?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_cigtorn?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_cigwind?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_cighail?refresh=${refresh}`)),
      ]);

      const catData = catRes.ok ? await catRes.json() : null;
      const tornData = tornRes.ok ? await tornRes.json() : null;
      const windData = windRes.ok ? await windRes.json() : null;
      const hailData = hailRes.ok ? await hailRes.json() : null;
      const cigTornData = cigTornRes.ok ? await cigTornRes.json() : null;
      const cigWindData = cigWindRes.ok ? await cigWindRes.json() : null;
      const cigHailData = cigHailRes.ok ? await cigHailRes.json() : null;

      const day2: DayDataResponse = {
        categorical: catData?.outlook || null,
        tornado: tornData?.outlook || null,
        wind: windData?.outlook || null,
        hail: hailData?.outlook || null,
        cig_tornado: cigTornData?.outlook?.polygons?.length ? cigTornData.outlook : null,
        cig_wind: cigWindData?.outlook?.polygons?.length ? cigWindData.outlook : null,
        cig_hail: cigHailData?.outlook?.polygons?.length ? cigHailData.outlook : null,
        risk_colors: catData?.risk_colors || {},
        risk_names: catData?.risk_names || {},
      };

      setDay2Data(day2);

      // Set initial outlook and CIG based on active tab
      if (activeTab === 'categorical' && day2.categorical) {
        setCurrentOutlook(day2.categorical);
        setCurrentCIG(null);
      } else if (activeTab === 'tornado' && day2.tornado) {
        setCurrentOutlook(day2.tornado);
        setCurrentCIG(day2.cig_tornado || null);
      } else if (activeTab === 'wind' && day2.wind) {
        setCurrentOutlook(day2.wind);
        setCurrentCIG(day2.cig_wind || null);
      } else if (activeTab === 'hail' && day2.hail) {
        setCurrentOutlook(day2.hail);
        setCurrentCIG(day2.cig_hail || null);
      } else if (day2.categorical) {
        setCurrentOutlook(day2.categorical);
        setCurrentCIG(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch Day 2 data');
    }
  }, [activeTab]);

  // Fetch outlook for Day 3 (categorical only)
  const fetchDay3Outlook = useCallback(async (refresh = false) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/outlook/day3_categorical?refresh=${refresh}`));
      if (!response.ok) throw new Error('Failed to fetch Day 3 outlook');
      const data = await response.json();
      setCurrentOutlook(data.outlook);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch Day 3 outlook');
    }
  }, []);

  // Fetch mesoscale discussions
  const fetchMesoscaleDiscussions = useCallback(async (refresh = false) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/mesoscale-discussions?refresh=${refresh}`));
      if (!response.ok) throw new Error('Failed to fetch mesoscale discussions');
      const data: MesoscaleDiscussionsResponse = await response.json();
      setMesoscaleDiscussions(data.discussions);
    } catch (err) {
      console.error('Failed to fetch mesoscale discussions:', err);
    }
  }, []);

  // Fetch state images
  const fetchStateImages = useCallback(async (day: number) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/state-images?day=${day}`));
      if (!response.ok) throw new Error('Failed to fetch state images');
      const data: StateImagesResponse = await response.json();
      setStateImages(data);
    } catch (err) {
      console.error('Failed to fetch state images:', err);
    }
  }, []);

  // Fetch discussion text
  const fetchDiscussion = useCallback(async (day: number, refresh = false) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/discussion?day=${day}&refresh=${refresh}`));
      if (!response.ok) throw new Error('Failed to fetch discussion');
      const data: DiscussionResponse = await response.json();
      setDiscussionText(data.text);
    } catch (err) {
      console.error('Failed to fetch discussion:', err);
      setDiscussionText(null);
    }
  }, []);

  // Initial fetch and periodic refresh
  useEffect(() => {
    const fetchAll = async () => {
      setLoading(true);
      setError(null);

      await Promise.all([
        fetchDay1Data(),
        fetchMesoscaleDiscussions(),
        fetchStateImages(1),
        fetchDiscussion(1),
      ]);

      setLoading(false);
    };

    fetchAll();

    // Refresh every 10 minutes
    const interval = setInterval(() => {
      fetchDay1Data();
      fetchMesoscaleDiscussions();
    }, 10 * 60 * 1000);

    return () => clearInterval(interval);
  }, [fetchDay1Data, fetchMesoscaleDiscussions, fetchStateImages, fetchDiscussion]);

  // Handle day change
  useEffect(() => {
    if (activeDay === 1) {
      fetchDay1Data();
    } else if (activeDay === 2) {
      fetchDay2Data();
    } else {
      // Day 3 - only categorical available
      fetchDay3Outlook();
      setActiveTab('categorical');
    }
    fetchStateImages(activeDay);
    fetchDiscussion(activeDay);
    setDiscussionExpanded(false);
  }, [activeDay, fetchDay1Data, fetchDay2Data, fetchDay3Outlook, fetchStateImages, fetchDiscussion]);

  // Handle tab change (Day 1 and Day 2)
  useEffect(() => {
    // Day 3 only has categorical, so ignore tab changes for Day 3
    if (activeDay === 3) {
      setCurrentCIG(null);
      return;
    }

    const dayData = activeDay === 1 ? day1Data : day2Data;
    if (!dayData) return;

    switch (activeTab) {
      case 'categorical':
        setCurrentOutlook(dayData.categorical);
        setCurrentCIG(null);
        break;
      case 'tornado':
        setCurrentOutlook(dayData.tornado || null);
        setCurrentCIG(dayData.cig_tornado || null);
        break;
      case 'wind':
        setCurrentOutlook(dayData.wind || null);
        setCurrentCIG(dayData.cig_wind || null);
        break;
      case 'hail':
        setCurrentOutlook(dayData.hail || null);
        setCurrentCIG(dayData.cig_hail || null);
        break;
    }
  }, [activeTab, activeDay, day1Data, day2Data]);

  // Get highest risk from current outlook
  const getHighestRisk = (): { level: string; name: string; color: string } | null => {
    if (!currentOutlook || !currentOutlook.polygons.length) return null;

    let highest = currentOutlook.polygons[0];
    let highestOrder = RISK_ORDER[highest.risk_level] || -1;

    for (const polygon of currentOutlook.polygons) {
      const order = RISK_ORDER[polygon.risk_level] || -1;
      if (order > highestOrder) {
        highestOrder = order;
        highest = polygon;
      }
    }

    return {
      level: highest.risk_level,
      name: highest.risk_name,
      color: highest.color,
    };
  };

  const highestRisk = getHighestRisk();

  // Refresh handler
  const handleRefresh = async () => {
    setLoading(true);
    if (activeDay === 1) {
      await fetchDay1Data(true);
    } else if (activeDay === 2) {
      await fetchDay2Data(true);
    } else {
      await fetchDay3Outlook(true);
    }
    await fetchMesoscaleDiscussions(true);
    setLoading(false);
  };

  return (
    <div className="spc-section">
      {/* Header */}
      <div className="spc-header">
        <h2 className="section-title">
          <i className="fas fa-cloud-sun-rain"></i> SPC Outlooks
        </h2>
        <div className="spc-controls">
          {/* Day selector */}
          <div className="spc-day-selector">
            {[1, 2, 3].map((day) => (
              <button
                key={day}
                className={`spc-day-btn ${activeDay === day ? 'active' : ''}`}
                onClick={() => setActiveDay(day as 1 | 2 | 3)}
              >
                Day {day}
              </button>
            ))}
          </div>
          <button
            className={`spc-day-btn${showMesoanalysis ? ' active' : ''}`}
            onClick={() => setShowMesoanalysis(p => !p)}
            title="SPC Mesoanalysis parameter images"
          >
            <i className="fas fa-layer-group"></i> Mesoanalysis
          </button>
          <button onClick={handleRefresh} className="spc-refresh-btn" disabled={loading}>
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i>
            Refresh
          </button>
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="spc-error">
          <i className="fas fa-exclamation-triangle"></i>
          {error}
        </div>
      )}

      {!showMesoanalysis && <>
      {/* Outlook type tabs (Day 1 and Day 2) */}
      {(activeDay === 1 || activeDay === 2) && (
        <div className="spc-tabs">
          <button
            className={`spc-tab ${activeTab === 'categorical' ? 'active' : ''}`}
            onClick={() => setActiveTab('categorical')}
          >
            Categorical
          </button>
          <button
            className={`spc-tab ${activeTab === 'tornado' ? 'active' : ''}`}
            onClick={() => setActiveTab('tornado')}
          >
            <i className="fas fa-tornado"></i> Tornado
          </button>
          <button
            className={`spc-tab ${activeTab === 'wind' ? 'active' : ''}`}
            onClick={() => setActiveTab('wind')}
          >
            <i className="fas fa-wind"></i> Wind
          </button>
          <button
            className={`spc-tab ${activeTab === 'hail' ? 'active' : ''}`}
            onClick={() => setActiveTab('hail')}
          >
            <i className="fas fa-cloud-meatball"></i> Hail
          </button>
        </div>
      )}

      {/* Risk summary bar */}
      {highestRisk && (
        <div className="spc-risk-bar">
          <span className="spc-risk-label">Highest Risk:</span>
          <span
            className="spc-risk-badge"
            style={{
              backgroundColor: highestRisk.color,
              color: getContrastColor(highestRisk.color),
            }}
          >
            {highestRisk.name}
          </span>
          {currentOutlook?.valid_time && (
            <span className="spc-valid-time">
              Valid: {new Date(currentOutlook.valid_time).toLocaleString()}
            </span>
          )}
        </div>
      )}

      {/* Main content */}
      <div className="spc-content">
        {/* Map */}
        <div className="spc-map-container">
          <MapContainer
            center={defaultCenter}
            zoom={defaultZoom}
            style={{ height: '100%', width: '100%', borderRadius: 'var(--radius-md)' }}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            />

            {currentOutlook?.geojson && (
              <>
                <FitBoundsToOutlook geojson={currentOutlook.geojson} />
                <GeoJSON
                  key={`${activeDay}-${activeTab}-${Date.now()}`}
                  data={currentOutlook.geojson}
                  style={getPolygonStyle}
                  onEachFeature={(feature, layer) => {
                    const props = feature.properties || {};
                    const label = (props.LABEL || props.label || '').toUpperCase();
                    const riskName = RISK_NAMES[label] || PROB_NAMES[label] || props.LABEL2 || props.label2 || label;

                    layer.bindPopup(`
                      <div class="spc-popup">
                        <h4>Day ${activeDay} ${activeTab === 'categorical' ? 'Categorical' : activeTab.charAt(0).toUpperCase() + activeTab.slice(1)} Outlook</h4>
                        <p><strong>Risk Level:</strong> ${riskName}</p>
                        ${props.VALID ? `<p><strong>Valid:</strong> ${props.VALID}</p>` : ''}
                        ${props.EXPIRE ? `<p><strong>Expires:</strong> ${props.EXPIRE}</p>` : ''}
                      </div>
                    `);
                  }}
                />
              </>
            )}

            {/* CIG (Conditional Intensity Group) hatched overlay */}
            {currentCIG?.geojson && (
              <GeoJSON
                key={`cig-${activeDay}-${activeTab}-${Date.now()}`}
                data={currentCIG.geojson}
                style={getCIGPolygonStyle}
                onEachFeature={(feature, layer) => {
                  const props = feature.properties || {};
                  const label = (props.LABEL || props.label || '').toUpperCase();
                  const cigName = CIG_NAMES[label] || props.LABEL2 || props.label2 || label;

                  layer.bindPopup(`
                    <div class="spc-popup">
                      <h4>Day ${activeDay} ${activeTab.charAt(0).toUpperCase() + activeTab.slice(1)} - Conditional Intensity</h4>
                      <p><strong>${cigName}</strong></p>
                      <p class="spc-popup-note">Hatched area indicates potential for more intense/violent storms</p>
                    </div>
                  `);
                }}
              />
            )}
          </MapContainer>
        </div>

        {/* Side panel */}
        <div className="spc-side-panel">
          {/* Risk Legend */}
          <div className="spc-panel-section">
            <h3>
              <i className="fas fa-layer-group"></i> {activeTab === 'categorical' ? 'Risk Levels' : 'Probability Levels'}
            </h3>
            <div className="spc-legend">
              {activeTab === 'categorical' ? (
                // Categorical legend
                Object.entries(RISK_COLORS)
                  .sort((a, b) => (RISK_ORDER[b[0]] || 0) - (RISK_ORDER[a[0]] || 0))
                  .map(([level, color]) => (
                    <div key={level} className="spc-legend-item">
                      <span
                        className="spc-legend-color"
                        style={{ backgroundColor: color }}
                      ></span>
                      <span className="spc-legend-label">{RISK_NAMES[level]}</span>
                    </div>
                  ))
              ) : (
                // Probabilistic legend with CIG hatching indicator
                <>
                  {['0.02', '0.05', '0.10', '0.15', '0.30', '0.45', '0.60', 'SIGN']
                    .reverse()
                    .map((level) => (
                      <div key={level} className="spc-legend-item">
                        <span
                          className="spc-legend-color"
                          style={{ backgroundColor: PROB_COLORS[level] || '#888888' }}
                        ></span>
                        <span className="spc-legend-label">{PROB_NAMES[level]}</span>
                      </div>
                    ))}
                  <div className="spc-legend-divider"></div>
                  <div className="spc-legend-item">
                    <span className="spc-legend-color spc-legend-hatched"></span>
                    <span className="spc-legend-label">Conditional Intensity (hatched)</span>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* State Images */}
          {stateImages && Object.keys(stateImages.images).length > 0 && (
            <div className="spc-panel-section">
              <h3>
                <i className="fas fa-image"></i> State Outlooks
                <span className="spc-click-hint">Click to expand</span>
              </h3>
              <div className="spc-state-images-grid">
                {Object.entries(stateImages.images).map(([state, urls]) => {
                  const imageUrl = activeTab === 'categorical' ? urls.categorical : urls[activeTab];
                  return (
                    <div
                      key={state}
                      className="spc-state-image-thumb"
                      onClick={() => setExpandedImage({ state, url: imageUrl })}
                    >
                      <div className="state-label">{state}</div>
                      <img
                        src={imageUrl}
                        alt={`${state} Day ${activeDay} ${activeTab} outlook`}
                        onError={(e) => {
                          (e.target as HTMLImageElement).style.display = 'none';
                        }}
                      />
                      <div className="expand-overlay">
                        <i className="fas fa-expand"></i>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Outlook Discussion */}
          <div className="spc-panel-section">
            <h3
              className="spc-discussion-header"
              onClick={() => setDiscussionExpanded(!discussionExpanded)}
            >
              <i className="fas fa-file-alt"></i> Day {activeDay} Discussion
              <i className={`fas fa-chevron-${discussionExpanded ? 'up' : 'down'} toggle-icon`}></i>
            </h3>
            {discussionExpanded && (
              <div className="spc-discussion-content">
                {discussionText ? (
                  <pre className="spc-discussion-text">{discussionText}</pre>
                ) : (
                  <div className="spc-empty">
                    <i className="fas fa-spinner fa-spin"></i>
                    <p>Loading discussion...</p>
                  </div>
                )}
                <a
                  href={`https://www.spc.noaa.gov/products/outlook/day${activeDay}otlk.html`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="spc-discussion-link"
                >
                  <i className="fas fa-external-link-alt"></i> View on SPC Website
                </a>
              </div>
            )}
          </div>

          {/* Mesoscale Discussions */}
          <div className="spc-panel-section">
            <h3>
              <i className="fas fa-file-alt"></i> Mesoscale Discussions
              {mesoscaleDiscussions.length > 0 && (
                <span className="md-count">{mesoscaleDiscussions.length}</span>
              )}
            </h3>
            {mesoscaleDiscussions.length === 0 ? (
              <div className="spc-empty">
                <i className="fas fa-check-circle"></i>
                <p>No active mesoscale discussions</p>
                <p className="spc-empty-sub">for your selected states</p>
              </div>
            ) : (
              <div className="spc-md-list">
                {mesoscaleDiscussions.map((md) => (
                  <div
                    key={md.md_number}
                    className="spc-md-item"
                    onClick={() => setSelectedMD(md)}
                    style={{ cursor: 'pointer' }}
                    title="Click to open the full discussion"
                  >
                    <div className="md-header">
                      <span className="md-number">MD #{md.md_number}</span>
                      <span className="md-states">{md.affected_states.join(', ')}</span>
                    </div>
                    <div className="md-title">{md.title}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
      </>}

      {/* ── Mesoanalysis Panel ─────────────────────────────────────── */}
      {showMesoanalysis && (
        <MesoanalysisPanel
          sector={mesoSector}
          category={mesoCategory}
          onSectorChange={setMesoSector}
          onCategoryChange={setMesoCategory}
          onLightbox={(url, label) => setMesoLightbox({ url, label })}
        />
      )}

      {/* Mesoscale Discussion modal — full-size image + readable text */}
      {selectedMD && (
        <div
          style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.85)', zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px' }}
          onClick={() => setSelectedMD(null)}
        >
          <div
            style={{ maxWidth: '1000px', width: '100%', maxHeight: '92vh', backgroundColor: 'var(--bg-primary)', borderRadius: '8px', overflow: 'hidden', boxShadow: '0 4px 32px rgba(0,0,0,0.6)', display: 'flex', flexDirection: 'column' }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-color)' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: '12px', flexWrap: 'wrap' }}>
                <span style={{ fontWeight: 700, fontSize: '1rem' }}>Mesoscale Discussion #{selectedMD.md_number}</span>
                {selectedMD.affected_states.length > 0 && (
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                    <i className="fas fa-location-dot"></i> {selectedMD.affected_states.join(', ')}
                  </span>
                )}
              </div>
              <button onClick={() => setSelectedMD(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: '1.2rem' }}>
                <i className="fas fa-times"></i>
              </button>
            </div>
            <div style={{ padding: '16px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <img
                src={selectedMD.image_url}
                alt={`MD ${selectedMD.md_number}`}
                style={{ maxWidth: '100%', maxHeight: '60vh', objectFit: 'contain', alignSelf: 'center', borderRadius: '4px', backgroundColor: '#000' }}
                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
              />
              <p style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6, fontSize: '0.9rem', color: 'var(--text-primary)', margin: 0 }}>
                {selectedMD.description}
              </p>
              <a href={selectedMD.link} target="_blank" rel="noopener noreferrer" style={{ alignSelf: 'flex-start', fontSize: '0.85rem' }}>
                <i className="fas fa-external-link-alt"></i> View full discussion on spc.noaa.gov
              </a>
            </div>
          </div>
        </div>
      )}

      {/* Mesoanalysis lightbox */}
      {mesoLightbox && (
        <div
          style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.85)', zIndex: 9999, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px' }}
          onClick={() => setMesoLightbox(null)}
        >
          <div
            style={{ maxWidth: '95vw', maxHeight: '90vh', backgroundColor: 'var(--bg-primary)', borderRadius: '8px', overflow: 'hidden', boxShadow: '0 4px 32px rgba(0,0,0,0.6)' }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-color)' }}>
              <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>{mesoLightbox.label}</span>
              <button onClick={() => setMesoLightbox(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: '1.1rem' }}>
                <i className="fas fa-times"></i>
              </button>
            </div>
            <img src={mesoLightbox.url} alt={mesoLightbox.label} style={{ maxWidth: '100%', maxHeight: 'calc(90vh - 50px)', display: 'block', objectFit: 'contain', backgroundColor: '#f0f0f0' }} />
          </div>
        </div>
      )}

      {expandedImage && (
        <div
          className="spc-image-modal-overlay"
          onClick={() => setExpandedImage(null)}
        >
          <div className="spc-image-modal" onClick={(e) => e.stopPropagation()}>
            <div className="spc-image-modal-header">
              <h3>
                <i className="fas fa-map"></i> {expandedImage.state} - Day {activeDay} {activeTab.charAt(0).toUpperCase() + activeTab.slice(1)} Outlook
              </h3>
              <button onClick={() => setExpandedImage(null)}>
                <i className="fas fa-times"></i>
              </button>
            </div>
            <div className="spc-image-modal-body">
              <img
                src={expandedImage.url}
                alt={`${expandedImage.state} Day ${activeDay} ${activeTab} outlook`}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

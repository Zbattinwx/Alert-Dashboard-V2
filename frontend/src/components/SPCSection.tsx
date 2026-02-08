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
import { RISK_COLORS, RISK_NAMES, RISK_ORDER, PROB_COLORS, PROB_NAMES, getContrastColor } from '../types/spc';
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

// Response type for Day 2 with probabilities (same structure as Day 1)
interface DayDataResponse {
  categorical: OutlookData | null;
  tornado?: OutlookData | null;
  wind?: OutlookData | null;
  hail?: OutlookData | null;
  risk_colors: Record<string, string>;
  risk_names: Record<string, string>;
}

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
  const [discussionExpanded, setDiscussionExpanded] = useState(false);
  const [expandedImage, setExpandedImage] = useState<{ state: string; url: string } | null>(null);

  // Default center: US
  const defaultCenter: L.LatLngExpression = [39.8283, -98.5795];
  const defaultZoom = 4;

  // Fetch Day 1 data with probabilities
  const fetchDay1Data = useCallback(async (refresh = false) => {
    try {
      const response = await fetch(apiUrl(`/api/spc/day1?include_probabilities=true&refresh=${refresh}`));
      if (!response.ok) throw new Error('Failed to fetch SPC data');
      const data: Day1Response = await response.json();
      setDay1Data(data);

      // Set initial outlook based on active tab
      if (activeTab === 'categorical' && data.categorical) {
        setCurrentOutlook(data.categorical);
      } else if (activeTab === 'tornado' && data.tornado) {
        setCurrentOutlook(data.tornado);
      } else if (activeTab === 'wind' && data.wind) {
        setCurrentOutlook(data.wind);
      } else if (activeTab === 'hail' && data.hail) {
        setCurrentOutlook(data.hail);
      } else if (data.categorical) {
        setCurrentOutlook(data.categorical);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch SPC data');
    }
  }, [activeTab]);

  // Fetch Day 2 data with probabilities
  const fetchDay2Data = useCallback(async (refresh = false) => {
    try {
      // Fetch all Day 2 outlooks in parallel
      const [catRes, tornRes, windRes, hailRes] = await Promise.all([
        fetch(apiUrl(`/api/spc/outlook/day2_categorical?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_tornado?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_wind?refresh=${refresh}`)),
        fetch(apiUrl(`/api/spc/outlook/day2_hail?refresh=${refresh}`)),
      ]);

      const catData = catRes.ok ? await catRes.json() : null;
      const tornData = tornRes.ok ? await tornRes.json() : null;
      const windData = windRes.ok ? await windRes.json() : null;
      const hailData = hailRes.ok ? await hailRes.json() : null;

      const day2: DayDataResponse = {
        categorical: catData?.outlook || null,
        tornado: tornData?.outlook || null,
        wind: windData?.outlook || null,
        hail: hailData?.outlook || null,
        risk_colors: catData?.risk_colors || {},
        risk_names: catData?.risk_names || {},
      };

      setDay2Data(day2);

      // Set initial outlook based on active tab
      if (activeTab === 'categorical' && day2.categorical) {
        setCurrentOutlook(day2.categorical);
      } else if (activeTab === 'tornado' && day2.tornado) {
        setCurrentOutlook(day2.tornado);
      } else if (activeTab === 'wind' && day2.wind) {
        setCurrentOutlook(day2.wind);
      } else if (activeTab === 'hail' && day2.hail) {
        setCurrentOutlook(day2.hail);
      } else if (day2.categorical) {
        setCurrentOutlook(day2.categorical);
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
    if (activeDay === 3) return;

    const dayData = activeDay === 1 ? day1Data : day2Data;
    if (!dayData) return;

    switch (activeTab) {
      case 'categorical':
        setCurrentOutlook(dayData.categorical);
        break;
      case 'tornado':
        setCurrentOutlook(dayData.tornado || null);
        break;
      case 'wind':
        setCurrentOutlook(dayData.wind || null);
        break;
      case 'hail':
        setCurrentOutlook(dayData.hail || null);
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
                    const riskName = RISK_NAMES[label] || props.LABEL2 || props.label2 || label;

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
                // Probabilistic legend - use unique keys to avoid duplicates
                ['0.02', '0.05', '0.10', '0.15', '0.30', '0.45', '0.60', 'SIGN']
                  .reverse()
                  .map((level) => (
                    <div key={level} className="spc-legend-item">
                      <span
                        className="spc-legend-color"
                        style={{ backgroundColor: PROB_COLORS[level] || '#888888' }}
                      ></span>
                      <span className="spc-legend-label">{PROB_NAMES[level]}</span>
                    </div>
                  ))
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
                    className={`spc-md-item ${selectedMD?.md_number === md.md_number ? 'selected' : ''}`}
                    onClick={() => setSelectedMD(selectedMD?.md_number === md.md_number ? null : md)}
                  >
                    <div className="md-header">
                      <span className="md-number">MD #{md.md_number}</span>
                      <span className="md-states">{md.affected_states.join(', ')}</span>
                    </div>
                    <div className="md-title">{md.title}</div>
                    {selectedMD?.md_number === md.md_number && (
                      <div className="md-details">
                        <img
                          src={md.image_url}
                          alt={`MD ${md.md_number}`}
                          className="md-image"
                          onError={(e) => {
                            (e.target as HTMLImageElement).style.display = 'none';
                          }}
                        />
                        <p className="md-description">{md.description}</p>
                        <a
                          href={md.link}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="md-link"
                        >
                          <i className="fas fa-external-link-alt"></i> View Full Discussion
                        </a>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Expanded Image Modal */}
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

import React, { useEffect, useState, useCallback } from 'react';
import { apiUrl } from '../utils/api';

interface WindGust {
  station: string;
  city: string;
  state: string;
  gust_mph: number;
  valid_time: string;
  severity: string;
  lat: number | null;
  lon: number | null;
}

interface WindGustsResponse {
  count: number;
  filter_states: string[];
  thresholds: {
    significant: number;
    severe: number;
    advisory: number;
  };
  by_state: Record<string, WindGust[]>;
}

// Severity colors matching V1 style
const SEVERITY_COLORS: Record<string, string> = {
  significant: '#ff0000', // Red - 70+ mph
  severe: '#ff7f00',      // Orange - 58+ mph
  advisory: '#ffff00',    // Yellow - 46+ mph
  normal: '#9ece6a',      // Green - below thresholds
};

// Format timestamp for display
const formatTime = (isoString: string): string => {
  const date = new Date(isoString);
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
};

// State name mapping
const STATE_NAMES: Record<string, string> = {
  OH: 'Ohio',
  WA: 'Washington',
  OR: 'Oregon',
  IN: 'Indiana',
  KY: 'Kentucky',
  PA: 'Pennsylvania',
  WV: 'West Virginia',
  MI: 'Michigan',
};

export const WindGustsSection: React.FC = () => {
  const [gustsByState, setGustsByState] = useState<Record<string, WindGust[]>>({});
  const [filterStates, setFilterStates] = useState<string[]>([]);
  const [thresholds, setThresholds] = useState({
    significant: 70,
    severe: 58,
    advisory: 46,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState(1);
  const [totalCount, setTotalCount] = useState(0);

  // Fetch gusts from API
  const fetchGusts = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        hours: hours.toString(),
        limit_per_state: '5',
        refresh: refresh.toString(),
      });

      const response = await fetch(apiUrl(`/api/wind-gusts/by-state?${params}`));
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data: WindGustsResponse = await response.json();
      setGustsByState(data.by_state);
      setFilterStates(data.filter_states);
      setThresholds(data.thresholds);
      setTotalCount(data.count);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch wind gusts');
    } finally {
      setLoading(false);
    }
  }, [hours]);

  // Initial fetch and periodic refresh
  useEffect(() => {
    fetchGusts();

    // Refresh every 5 minutes
    const interval = setInterval(() => fetchGusts(), 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [fetchGusts]);

  // Get the highest gust overall
  const highestGust = Object.values(gustsByState)
    .flat()
    .sort((a, b) => b.gust_mph - a.gust_mph)[0];

  return (
    <div className="wind-gusts-section">
      {/* Controls */}
      <div className="wind-gusts-controls">
        <div className="wind-gusts-controls-left">
          <label>
            <span>Time Range:</span>
            <select
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
              className="wind-gusts-select"
            >
              <option value={1}>Last 1 hour</option>
              <option value={3}>Last 3 hours</option>
              <option value={6}>Last 6 hours</option>
              <option value={12}>Last 12 hours</option>
              <option value={24}>Last 24 hours</option>
            </select>
          </label>
          <button
            onClick={() => fetchGusts(true)}
            className="wind-gusts-refresh-btn"
            disabled={loading}
          >
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i>
            Refresh
          </button>
        </div>
        <div className="wind-gusts-stats">
          <span className="wind-gusts-count">{totalCount} Observations</span>
          {filterStates.length > 0 && (
            <span className="wind-gusts-states">
              {filterStates.join(', ')}
            </span>
          )}
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="wind-gusts-error">
          <i className="fas fa-exclamation-triangle"></i>
          Error loading wind gusts: {error}
        </div>
      )}

      {/* Legend */}
      <div className="wind-gusts-legend">
        <div className="wind-gusts-legend-item" style={{ color: SEVERITY_COLORS.significant }}>
          <i className="fas fa-circle"></i>
          <span>Significant ({thresholds.significant}+ mph)</span>
        </div>
        <div className="wind-gusts-legend-item" style={{ color: SEVERITY_COLORS.severe }}>
          <i className="fas fa-circle"></i>
          <span>Severe ({thresholds.severe}+ mph)</span>
        </div>
        <div className="wind-gusts-legend-item" style={{ color: SEVERITY_COLORS.advisory }}>
          <i className="fas fa-circle"></i>
          <span>Advisory ({thresholds.advisory}+ mph)</span>
        </div>
        <div className="wind-gusts-legend-item" style={{ color: SEVERITY_COLORS.normal }}>
          <i className="fas fa-circle"></i>
          <span>Normal</span>
        </div>
      </div>

      {/* Highest gust highlight */}
      {highestGust && (
        <div
          className="wind-gusts-highest"
          style={{ borderColor: SEVERITY_COLORS[highestGust.severity] }}
        >
          <div className="wind-gusts-highest-label">
            <i className="fas fa-trophy"></i> Highest Gust
          </div>
          <div className="wind-gusts-highest-value" style={{ color: SEVERITY_COLORS[highestGust.severity] }}>
            {highestGust.gust_mph} mph
          </div>
          <div className="wind-gusts-highest-location">
            {highestGust.city}, {highestGust.state}
          </div>
          <div className="wind-gusts-highest-time">
            {formatTime(highestGust.valid_time)}
          </div>
        </div>
      )}

      {/* Main content - gusts by state */}
      <div className="wind-gusts-content">
        {loading ? (
          <div className="wind-gusts-loading">
            <i className="fas fa-spinner fa-spin"></i>
            Loading wind gusts...
          </div>
        ) : totalCount === 0 ? (
          <div className="wind-gusts-empty">
            <i className="fas fa-wind"></i>
            <p>No significant wind gusts reported</p>
            <p className="wind-gusts-empty-sub">
              No gusts above 0 mph in the last {hours} hour{hours > 1 ? 's' : ''}
            </p>
          </div>
        ) : (
          <div className="wind-gusts-states-grid">
            {filterStates.map((state) => (
              <div key={state} className="wind-gusts-state-card">
                <div className="wind-gusts-state-header">
                  <h3>
                    <i className="fas fa-map-marker-alt"></i>
                    {STATE_NAMES[state] || state}
                  </h3>
                  <span className="wind-gusts-state-count">
                    {gustsByState[state]?.length || 0} observations
                  </span>
                </div>
                <div className="wind-gusts-state-list">
                  {gustsByState[state]?.length > 0 ? (
                    gustsByState[state].map((gust, index) => (
                      <div
                        key={`${gust.station}-${index}`}
                        className={`wind-gusts-item severity-${gust.severity}`}
                      >
                        <div
                          className="wind-gusts-item-speed"
                          style={{ color: SEVERITY_COLORS[gust.severity] }}
                        >
                          {gust.gust_mph}
                          <span className="wind-gusts-item-unit">mph</span>
                        </div>
                        <div className="wind-gusts-item-info">
                          <div className="wind-gusts-item-city">{gust.city}</div>
                          <div className="wind-gusts-item-time">
                            {formatTime(gust.valid_time)}
                          </div>
                        </div>
                        <div
                          className="wind-gusts-item-indicator"
                          style={{ backgroundColor: SEVERITY_COLORS[gust.severity] }}
                        />
                      </div>
                    ))
                  ) : (
                    <div className="wind-gusts-state-empty">
                      No gusts reported
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

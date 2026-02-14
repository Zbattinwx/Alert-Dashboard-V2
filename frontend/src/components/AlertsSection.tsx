import React, { useState, useMemo, useEffect } from 'react';
import type { Alert } from '../types/alert';
import { PHENOMENON_NAMES, getAlertStyle } from '../types/alert';
import { AlertCard } from './AlertCard';
import { AlertDetailPane } from './AlertDetailPane';
import { clearAlert } from '../utils/api';

interface DynamicFilter {
  id: string;
  phenomenon: string;
  label: string;
  count: number;
  style: {
    backgroundColor: string;
    borderColor: string;
    textColor: string;
  };
}

// Priority order for sorting filters and alerts (highest priority first)
// Warnings come before watches for the same phenomenon type
const PHENOMENON_PRIORITY: Record<string, number> = {
  // Warnings first (sorted by severity)
  TO: 1, TOR: 1,    // Tornado Warning
  SV: 2, SVR: 2,    // Severe T-Storm Warning
  FF: 3, FFW: 3,    // Flash Flood Warning
  FL: 4, FLW: 4,    // Flood Warning
  EW: 5,            // Extreme Wind Warning
  BZ: 6,            // Blizzard Warning
  IS: 7,            // Ice Storm Warning
  WS: 8, WSW: 8,    // Winter Storm Warning
  LE: 9,            // Lake Effect Snow Warning
  HW: 10,           // High Wind Warning
  WC: 11,           // Wind Chill Warning
  EH: 12,           // Excessive Heat Warning
  FW: 13,           // Fire Weather Warning
  // Watches (after warnings)
  TOA: 20,          // Tornado Watch
  SVA: 21,          // Severe T-Storm Watch
  FFA: 22,          // Flash Flood Watch
  FLA: 23,          // Flood Watch
  WSA: 24,          // Winter Storm Watch
  WCA: 25,          // Wind Chill Watch
  HWA: 26,          // High Wind Watch
  EHA: 27,          // Excessive Heat Watch
  FWA: 28,          // Fire Weather Watch
  // Advisories and statements (lower priority)
  WW: 30,           // Winter Weather Advisory
  WI: 31,           // Wind Advisory
  HT: 32,           // Heat Advisory
  FG: 33,           // Dense Fog Advisory
  FR: 34,           // Frost Advisory
  FA: 35,           // Areal Flood Advisory
  SPS: 40,          // Special Weather Statement
};

interface AlertsSectionProps {
  alerts: Alert[];
}

// Helper to get priority for an alert (using composite key phenomenon-significance)
const getAlertPriority = (alert: Alert): number => {
  const phenomenon = alert.phenomenon;
  const significance = alert.significance || 'W';
  // For watches (significance 'A'), try the watch-specific key first (e.g., "TOA")
  if (significance === 'A') {
    const watchKey = `${phenomenon}A`;
    if (PHENOMENON_PRIORITY[watchKey] !== undefined) {
      return PHENOMENON_PRIORITY[watchKey];
    }
  }
  return PHENOMENON_PRIORITY[phenomenon] ?? 99;
};

export const AlertsSection: React.FC<AlertsSectionProps> = ({ alerts }) => {
  const [activeFilters, setActiveFilters] = useState<Set<string>>(new Set());
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  // Generate dynamic filters based on active alerts
  const dynamicFilters = useMemo(() => {
    // Count alerts by phenomenon + significance (to distinguish watches from warnings)
    // Use the first alert's event_name as the label
    const phenomenonGroups: Record<string, { count: number; phenomenon: string; significance: string; eventName: string }> = {};

    alerts.forEach((alert) => {
      const phenomenon = alert.phenomenon;
      const significance = alert.significance || 'W';  // Default to Warning if not specified
      if (phenomenon) {
        // Create a composite key from phenomenon + significance
        const key = `${phenomenon}-${significance}`;
        if (!phenomenonGroups[key]) {
          phenomenonGroups[key] = {
            count: 0,
            phenomenon,
            significance,
            eventName: alert.event_name,  // Use the actual event name from the alert
          };
        }
        phenomenonGroups[key].count += 1;
      }
    });

    // Convert to filter objects with styling
    const filters: DynamicFilter[] = Object.entries(phenomenonGroups).map(([key, group]) => {
      const style = getAlertStyle(group.phenomenon, group.significance);
      // Use the event_name from the alert, which correctly says "High Wind Watch" or "High Wind Warning"
      const label = group.eventName || PHENOMENON_NAMES[group.phenomenon] || group.phenomenon;

      return {
        id: key.toLowerCase(),  // Use composite key for uniqueness
        phenomenon: key,        // Use composite key for filtering
        label,
        count: group.count,
        style: {
          backgroundColor: style.backgroundColor,
          borderColor: style.borderColor,
          textColor: style.textColor,
        },
      };
    });

    // Sort by priority (lower number = higher priority)
    // For watches, try watch-specific key (e.g., "TOA") first, then fall back to base phenomenon
    filters.sort((a, b) => {
      const [basePhenomenonA, sigA] = a.phenomenon.split('-');
      const [basePhenomenonB, sigB] = b.phenomenon.split('-');
      // For watches (significance 'A'), try the watch-specific key first
      let priorityA = PHENOMENON_PRIORITY[basePhenomenonA] ?? 99;
      let priorityB = PHENOMENON_PRIORITY[basePhenomenonB] ?? 99;
      if (sigA === 'A') {
        const watchKeyA = `${basePhenomenonA}A`;
        if (PHENOMENON_PRIORITY[watchKeyA] !== undefined) {
          priorityA = PHENOMENON_PRIORITY[watchKeyA];
        }
      }
      if (sigB === 'A') {
        const watchKeyB = `${basePhenomenonB}A`;
        if (PHENOMENON_PRIORITY[watchKeyB] !== undefined) {
          priorityB = PHENOMENON_PRIORITY[watchKeyB];
        }
      }
      if (priorityA !== priorityB) {
        return priorityA - priorityB;
      }
      // If same priority, sort by count (descending)
      return b.count - a.count;
    });

    return filters;
  }, [alerts]);

  // Remove filters that no longer have any alerts
  useEffect(() => {
    if (activeFilters.size > 0) {
      const validFilterIds = new Set(dynamicFilters.map(f => f.id));
      const updatedFilters = new Set([...activeFilters].filter(f => validFilterIds.has(f)));
      if (updatedFilters.size !== activeFilters.size) {
        setActiveFilters(updatedFilters);
      }
    }
  }, [dynamicFilters, activeFilters]);

  // Toggle a filter on/off
  const toggleFilter = (filterId: string) => {
    setActiveFilters(prev => {
      const newFilters = new Set(prev);
      if (newFilters.has(filterId)) {
        newFilters.delete(filterId);
      } else {
        newFilters.add(filterId);
      }
      return newFilters;
    });
  };

  // Clear all filters (show all)
  const clearFilters = () => {
    setActiveFilters(new Set());
  };

  const filteredAlerts = useMemo(() => {
    // Filter first
    let result = alerts;
    if (activeFilters.size > 0) {
      // Build a set of phenomenon-significance pairs to match
      const selectedPairs = new Set<string>();
      activeFilters.forEach(filterId => {
        const filterConfig = dynamicFilters.find((f) => f.id === filterId);
        if (filterConfig) {
          selectedPairs.add(filterConfig.phenomenon.toUpperCase());
        }
      });
      result = alerts.filter(a => {
        const alertKey = `${a.phenomenon}-${a.significance || 'W'}`;
        return selectedPairs.has(alertKey);
      });
    }

    // Sort by priority (lower = higher priority), then by issued time (newest first)
    return [...result].sort((a, b) => {
      const priorityA = getAlertPriority(a);
      const priorityB = getAlertPriority(b);
      if (priorityA !== priorityB) {
        return priorityA - priorityB;
      }
      // Same priority - sort by time (newest first)
      const timeA = a.issued_time ? new Date(a.issued_time).getTime() : 0;
      const timeB = b.issued_time ? new Date(b.issued_time).getTime() : 0;
      return timeB - timeA;
    });
  }, [alerts, activeFilters, dynamicFilters]);

  const handleAlertClick = (alert: Alert) => {
    setSelectedAlert(alert);
    setDetailOpen(true);
  };

  const handleCloseDetail = () => {
    setDetailOpen(false);
  };

  const handleClearAlert = async (alert: Alert) => {
    const success = await clearAlert(alert.product_id);
    if (success) {
      // The websocket connection will automatically push the updated alert list
      // so no manual refresh is needed here. We can add a toast notification later.
      console.log(`Alert ${alert.product_id} was cleared. The list will update automatically.`);
    }
  };

  return (
    <div className="section active">
      <h2 className="section-title">Active Alerts</h2>

      <div className="filter-controls">
        {/* "All" button clears all filters */}
        <button
          className={`filter-btn ${activeFilters.size === 0 ? 'active' : ''}`}
          onClick={clearFilters}
          style={{
            backgroundColor: activeFilters.size === 0 ? 'var(--accent-blue)' : 'var(--bg-tertiary)',
            borderColor: activeFilters.size === 0 ? 'var(--accent-blue)' : 'var(--border-color)',
            color: activeFilters.size === 0 ? '#ffffff' : 'var(--text-primary)',
          }}
        >
          All ({alerts.length})
        </button>

        {/* Dynamic filters - click to toggle (multi-select) */}
        {dynamicFilters.map((filter) => {
          const isSelected = activeFilters.has(filter.id);
          return (
            <button
              key={filter.id}
              className={`filter-btn ${isSelected ? 'active' : ''}`}
              onClick={() => toggleFilter(filter.id)}
              style={{
                backgroundColor: isSelected ? filter.style.backgroundColor : 'var(--bg-tertiary)',
                borderColor: isSelected ? filter.style.borderColor : 'var(--border-color)',
                color: isSelected ? filter.style.textColor : 'var(--text-primary)',
              }}
            >
              {filter.label} ({filter.count})
            </button>
          );
        })}
      </div>

      {filteredAlerts.length === 0 ? (
        <div className="no-alerts">
          <i className="fas fa-check-circle" style={{ fontSize: '3rem', marginBottom: '1rem', color: 'var(--accent-green)' }}></i>
          <p>No active alerts matching your filter</p>
        </div>
      ) : (
        <div className="alerts-grid">
          {filteredAlerts.map((alert) => (
            <AlertCard
              key={alert.product_id}
              alert={alert}
              onClick={handleAlertClick}
              onClear={handleClearAlert}
            />
          ))}
        </div>
      )}

      <AlertDetailPane
        alert={selectedAlert}
        isOpen={detailOpen}
        onClose={handleCloseDetail}
      />
    </div>
  );
};

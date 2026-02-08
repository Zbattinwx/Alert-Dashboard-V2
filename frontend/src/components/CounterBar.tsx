import React, { useMemo } from 'react';
import type { Alert } from '../types/alert';
import { PHENOMENON_NAMES, getAlertStyle } from '../types/alert';

interface DynamicCounter {
  phenomenon: string;
  label: string;
  count: number;
  style: {
    backgroundColor: string;
    borderColor: string;
    textColor: string;
  };
}

// Priority order for sorting counters (highest priority first)
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

interface CounterBarProps {
  alerts: Alert[];
}

export const CounterBar: React.FC<CounterBarProps> = ({ alerts }) => {
  const dynamicCounters = useMemo(() => {
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

    // Convert to counter objects with styling
    const counters: DynamicCounter[] = Object.entries(phenomenonGroups).map(([key, group]) => {
      const style = getAlertStyle(group.phenomenon, group.significance);
      // Use the event_name from the alert, which correctly says "High Wind Watch" or "High Wind Warning"
      const label = group.eventName || PHENOMENON_NAMES[group.phenomenon] || group.phenomenon;

      return {
        phenomenon: key,  // Use composite key for uniqueness
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
    counters.sort((a, b) => {
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

    return counters;
  }, [alerts]);

  // If no alerts, show a message
  if (dynamicCounters.length === 0) {
    return (
      <div className="counter-bar">
        <div style={{ color: 'var(--text-secondary)', fontSize: '0.875rem' }}>
          No active alerts
        </div>
      </div>
    );
  }

  return (
    <div className="counter-bar">
      {dynamicCounters.map((counter) => (
        <div
          key={counter.phenomenon}
          className="counter-item"
          style={{
            backgroundColor: counter.style.backgroundColor,
            borderColor: counter.style.borderColor,
            color: counter.style.textColor,
          }}
        >
          <span className="counter-title">{counter.label}</span>
          <span className="counter-value">{counter.count}</span>
        </div>
      ))}
    </div>
  );
};

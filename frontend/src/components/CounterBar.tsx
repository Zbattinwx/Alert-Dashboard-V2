import React, { useMemo } from 'react';
import type { Alert } from '../types/alert';

interface CounterConfig {
  id: string;
  label: string;
  phenomenon: string;
  significance?: string;
  className: string;
}

const counters: CounterConfig[] = [
  { id: 'tor', label: 'Tornado Wrn', phenomenon: 'TO', significance: 'W', className: 'tor' },
  { id: 'svr', label: 'Svr T-Storm Wrn', phenomenon: 'SV', significance: 'W', className: 'svr' },
  { id: 'ffw', label: 'Flash Flood Wrn', phenomenon: 'FF', significance: 'W', className: 'ffw' },
  { id: 'toa', label: 'Tornado Watch', phenomenon: 'TO', significance: 'A', className: 'tor-watch' },
  { id: 'sva', label: 'Svr T-Storm Watch', phenomenon: 'SV', significance: 'A', className: 'svr-watch' },
  { id: 'ffa', label: 'Flash Flood Watch', phenomenon: 'FF', significance: 'A', className: 'ffa' },
  { id: 'wsw', label: 'Winter Storm Wrn', phenomenon: 'WS', significance: 'W', className: 'wsw-warn' },
  { id: 'bz', label: 'Blizzard Wrn', phenomenon: 'BZ', significance: 'W', className: 'bz-warn' },
  { id: 'is', label: 'Ice Storm Wrn', phenomenon: 'IS', significance: 'W', className: 'is-warn' },
  { id: 'le', label: 'Lake Effect Wrn', phenomenon: 'LE', significance: 'W', className: 'le-warn' },
  { id: 'ww', label: 'Winter Wx Adv', phenomenon: 'WW', significance: 'Y', className: 'ww-advisory' },
  { id: 'wsa', label: 'Winter Storm Watch', phenomenon: 'WS', significance: 'A', className: 'wsw-warn' },
  { id: 'sps', label: 'Special Wx', phenomenon: 'SPS', className: 'sps' },
];

interface CounterBarProps {
  alerts: Alert[];
}

export const CounterBar: React.FC<CounterBarProps> = ({ alerts }) => {
  const counts = useMemo(() => {
    const result: Record<string, number> = {};

    counters.forEach((counter) => {
      result[counter.id] = alerts.filter((alert) => {
        const matchesPhenomenon = alert.phenomenon === counter.phenomenon;
        if (counter.significance) {
          return matchesPhenomenon && alert.significance === counter.significance;
        }
        return matchesPhenomenon;
      }).length;
    });

    return result;
  }, [alerts]);

  // Only show counters with active alerts (non-zero values)
  const visibleCounters = counters.filter((c) => counts[c.id] > 0);

  // If no alerts, show a message
  if (visibleCounters.length === 0) {
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
      {visibleCounters.map((counter) => (
        <div key={counter.id} className={`counter-item ${counter.className}`}>
          <span className="counter-title">{counter.label}</span>
          <span className="counter-value">{counts[counter.id]}</span>
        </div>
      ))}
    </div>
  );
};

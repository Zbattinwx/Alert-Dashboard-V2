import React, { useState, useMemo } from 'react';
import type { Alert } from '../types/alert';
import { AlertCard } from './AlertCard';
import { AlertDetailPane } from './AlertDetailPane';

interface FilterButton {
  id: string;
  label: string;
  filter: (alert: Alert) => boolean;
}

const filters: FilterButton[] = [
  { id: 'all', label: 'All', filter: () => true },
  { id: 'tor', label: 'Tornado', filter: (a) => a.phenomenon === 'TO' },
  { id: 'svr', label: 'Severe T-Storm', filter: (a) => a.phenomenon === 'SV' },
  { id: 'ff', label: 'Flash Flood', filter: (a) => a.phenomenon === 'FF' },
  { id: 'winter', label: 'Winter', filter: (a) => ['WS', 'BZ', 'IS', 'LE', 'WW', 'WC'].includes(a.phenomenon) },
  { id: 'sps', label: 'Special WX', filter: (a) => a.phenomenon === 'SPS' },
];

interface AlertsSectionProps {
  alerts: Alert[];
}

export const AlertsSection: React.FC<AlertsSectionProps> = ({ alerts }) => {
  const [activeFilter, setActiveFilter] = useState('all');
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const filteredAlerts = useMemo(() => {
    const filterConfig = filters.find((f) => f.id === activeFilter);
    if (!filterConfig) return alerts;
    return alerts.filter(filterConfig.filter);
  }, [alerts, activeFilter]);

  const handleAlertClick = (alert: Alert) => {
    setSelectedAlert(alert);
    setDetailOpen(true);
  };

  const handleCloseDetail = () => {
    setDetailOpen(false);
  };

  return (
    <div className="section active">
      <h2 className="section-title">Active Alerts</h2>

      <div className="filter-controls">
        {filters.map((filter) => (
          <button
            key={filter.id}
            className={`filter-btn ${activeFilter === filter.id ? 'active' : ''}`}
            onClick={() => setActiveFilter(filter.id)}
          >
            {filter.label}
          </button>
        ))}
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

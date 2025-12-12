import React from 'react';
import type { Alert } from '../types/alert';

interface AlertCardProps {
  alert: Alert;
  onClick?: (alert: Alert) => void;
}

export const AlertCard: React.FC<AlertCardProps> = ({ alert, onClick }) => {
  const formatTime = (isoString: string | null) => {
    if (!isoString) return '';
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  };

  const formatAreas = (areas: string[]) => {
    if (areas.length <= 3) {
      return areas.join(', ');
    }
    return `${areas.slice(0, 3).join(', ')} +${areas.length - 3} more`;
  };

  const phenomenonClass = alert.phenomenon?.toLowerCase() || 'default';

  return (
    <div
      className={`alert-card ${phenomenonClass}`}
      onClick={() => onClick?.(alert)}
    >
      <div className="alert-card-header">
        <span className="alert-card-type">{alert.event_name}</span>
        <span className="alert-card-time">
          {formatTime(alert.issued_time)}
        </span>
      </div>
      <div className="alert-card-body">
        {alert.headline && (
          <div className="alert-card-headline">{alert.headline}</div>
        )}
        <div className="alert-card-areas">
          <i className="fas fa-map-marker-alt" style={{ marginRight: '4px' }}></i>
          {formatAreas(alert.affected_areas)}
        </div>
        {alert.threat.has_tornado && (
          <div style={{
            marginTop: '8px',
            padding: '4px 8px',
            backgroundColor: 'var(--tor-color)',
            color: 'white',
            borderRadius: '4px',
            fontSize: '0.75rem',
            fontWeight: 'bold',
            display: 'inline-block'
          }}>
            <i className="fas fa-exclamation-triangle" style={{ marginRight: '4px' }}></i>
            TORNADO {alert.threat.tornado_observed ? 'OBSERVED' : 'POSSIBLE'}
          </div>
        )}
        {alert.threat.max_hail_size_inches && alert.threat.max_hail_size_inches > 0 && (
          <div style={{
            marginTop: '8px',
            fontSize: '0.75rem',
            color: 'var(--text-secondary)'
          }}>
            <i className="fas fa-cloud-meatball" style={{ marginRight: '4px' }}></i>
            Hail: {alert.threat.max_hail_size_inches}" |
            <i className="fas fa-wind" style={{ marginLeft: '8px', marginRight: '4px' }}></i>
            Wind: {alert.threat.max_wind_gust_mph} mph
          </div>
        )}
      </div>
    </div>
  );
};

import React from 'react';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';

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

  const formatExpiration = (isoString: string | null) => {
    if (!isoString) return '';
    const date = new Date(isoString);
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  };

  // Get styling based on phenomenon AND significance (for watch vs warning colors)
  const alertStyle = getAlertStyle(alert.phenomenon, alert.significance);

  // Truncate long location lists (for merged watches with many counties)
  const truncateLocations = (locations: string, maxLength: number = 120): string => {
    if (!locations || locations.length <= maxLength) return locations;

    // Split by semicolon or comma
    const parts = locations.split(/[;,]/).map(s => s.trim()).filter(Boolean);
    if (parts.length <= 3) return locations;

    // Take first 3 locations and add count
    const shown = parts.slice(0, 3).join('; ');
    const remaining = parts.length - 3;
    return `${shown}; and ${remaining} more`;
  };

  const displayLocations = truncateLocations(alert.display_locations || alert.affected_areas.join(', '));

  // Build impacts array
  const impacts: string[] = [];

  // Wind display: show sustained wind and/or gusts
  const hasSustained = alert.threat.sustained_wind_min_mph || alert.threat.sustained_wind_max_mph;
  const hasGusts = alert.threat.max_wind_gust_mph;

  if (hasSustained && hasGusts) {
    // Show both: "Wind: 25-35 mph | Gusts: 50 mph"
    const sustainedMin = alert.threat.sustained_wind_min_mph;
    const sustainedMax = alert.threat.sustained_wind_max_mph;
    const sustainedStr = sustainedMin !== sustainedMax
      ? `${sustainedMin}-${sustainedMax}`
      : `${sustainedMax}`;
    impacts.push(`Wind: ${sustainedStr} mph | Gusts: ${alert.threat.max_wind_gust_mph} mph`);
  } else if (hasSustained) {
    // Only sustained wind (no gusts mentioned)
    const sustainedMin = alert.threat.sustained_wind_min_mph;
    const sustainedMax = alert.threat.sustained_wind_max_mph;
    const sustainedStr = sustainedMin !== sustainedMax
      ? `${sustainedMin}-${sustainedMax}`
      : `${sustainedMax}`;
    impacts.push(`Wind: ${sustainedStr} mph`);
  } else if (hasGusts) {
    // Only gusts (common for severe thunderstorm warnings)
    impacts.push(`Gusts: ${alert.threat.max_wind_gust_mph} mph`);
  }
  if (alert.threat.max_hail_size_inches) {
    impacts.push(`Hail: ${alert.threat.max_hail_size_inches}"`);
  }
  if (alert.threat.snow_amount_max_inches) {
    const snowMin = alert.threat.snow_amount_min_inches || 0;
    const snowMax = alert.threat.snow_amount_max_inches;
    impacts.push(snowMin !== snowMax ? `Snow: ${snowMin}-${snowMax}"` : `Snow: ${snowMax}"`);
  }
  if (alert.threat.ice_accumulation_inches) {
    impacts.push(`Ice: ${alert.threat.ice_accumulation_inches}"`);
  }

  return (
    <div
      className="alert-card"
      onClick={() => onClick?.(alert)}
      style={{
        borderLeftColor: alertStyle.borderColor,
      }}
    >
      <div
        className="alert-card-header"
        style={{
          backgroundColor: alertStyle.backgroundColor,
          color: alertStyle.textColor,
        }}
      >
        <span className="alert-card-type">{alert.event_name}</span>
        <span className="alert-card-time">
          {formatTime(alert.issued_time)}
        </span>
      </div>
      <div className="alert-card-body">
        {/* Location - use display_locations (human readable), truncated for merged watches */}
        <div className="alert-card-areas">
          <i className="fas fa-map-marker-alt" style={{ marginRight: '4px' }}></i>
          {displayLocations}
        </div>

        {/* Expiration time */}
        {alert.expiration_time && (
          <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            <i className="fas fa-clock" style={{ marginRight: '4px' }}></i>
            Until {formatExpiration(alert.expiration_time)}
          </div>
        )}

        {/* Issuing office */}
        {alert.sender_name && (
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '4px' }}>
            {alert.sender_name}
          </div>
        )}

        {/* Tornado tag */}
        {alert.threat.tornado_detection && (
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
            TORNADO {alert.threat.tornado_detection}
          </div>
        )}

        {/* Impacts */}
        {impacts.length > 0 && (
          <div style={{
            marginTop: '8px',
            fontSize: '0.8rem',
            color: 'var(--text-primary)',
            display: 'flex',
            flexWrap: 'wrap',
            gap: '8px'
          }}>
            {impacts.map((impact, idx) => (
              <span key={idx} style={{
                padding: '2px 8px',
                backgroundColor: 'var(--bg-tertiary)',
                borderRadius: '4px'
              }}>
                {impact}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

import React, { useState, useEffect, useCallback } from 'react';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';
import { useWebSocket } from '../hooks/useWebSocket';

// Get WebSocket URL
const getWebSocketUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (import.meta.env.DEV) {
    return `${protocol}//${window.location.hostname}:8000/ws`;
  }
  return `${protocol}//${window.location.host}/ws`;
};

interface OBSAlertProps {
  alert: Alert;
  onDismiss: () => void;
  duration: number;
}

const OBSAlert: React.FC<OBSAlertProps> = ({ alert, onDismiss, duration }) => {
  const [visible, setVisible] = useState(false);
  const [progress, setProgress] = useState(100);

  useEffect(() => {
    // Trigger entrance animation
    requestAnimationFrame(() => setVisible(true));

    // Progress bar countdown
    const startTime = Date.now();
    const progressInterval = setInterval(() => {
      const elapsed = Date.now() - startTime;
      const remaining = Math.max(0, 100 - (elapsed / duration) * 100);
      setProgress(remaining);
    }, 50);

    // Auto-dismiss
    const dismissTimer = setTimeout(() => {
      setVisible(false);
      setTimeout(onDismiss, 500); // Wait for exit animation
    }, duration);

    return () => {
      clearInterval(progressInterval);
      clearTimeout(dismissTimer);
    };
  }, [duration, onDismiss]);

  const alertStyle = getAlertStyle(alert.phenomenon, alert.significance);
  const isEmergency = alert.event_name?.toLowerCase().includes('emergency') ||
    alert.threat?.tornado_damage_threat === 'CATASTROPHIC';
  const isTornado = alert.phenomenon === 'TO' || alert.threat?.tornado_detection;

  // Truncate locations for display
  const truncateLocations = (locations: string, maxLength: number = 100): string => {
    if (!locations || locations.length <= maxLength) return locations;
    const parts = locations.split(/[;,]/).map(s => s.trim()).filter(Boolean);
    if (parts.length <= 2) return locations;
    const shown = parts.slice(0, 2).join('; ');
    const remaining = parts.length - 2;
    return `${shown}; +${remaining} more`;
  };

  const displayLocations = truncateLocations(alert.display_locations || alert.affected_areas?.join(', ') || '');

  return (
    <div className={`obs-alert ${visible ? 'visible' : ''} ${isEmergency ? 'emergency' : ''} ${isTornado ? 'tornado' : ''}`}>
      {/* Progress bar */}
      <div className="obs-alert-progress" style={{ width: `${progress}%`, backgroundColor: alertStyle.borderColor }} />

      {/* Header */}
      <div className="obs-alert-header" style={{ backgroundColor: alertStyle.backgroundColor, color: alertStyle.textColor }}>
        <span className="obs-alert-badge">NEW ALERT</span>
        <span className="obs-alert-title">{alert.event_name}</span>
      </div>

      {/* Body */}
      <div className="obs-alert-body">
        {/* Location */}
        <div className="obs-alert-location">
          <i className="fas fa-map-marker-alt"></i>
          <span>{displayLocations}</span>
        </div>

        {/* Tornado tag */}
        {alert.threat?.tornado_detection && (
          <div className="obs-alert-tornado">
            <i className="fas fa-exclamation-triangle"></i>
            TORNADO {alert.threat.tornado_detection}
            {alert.threat.tornado_damage_threat && ` - ${alert.threat.tornado_damage_threat}`}
          </div>
        )}

        {/* Threat info */}
        <div className="obs-alert-threats">
          {alert.threat?.max_wind_gust_mph && (
            <span className="obs-alert-threat-item">
              <i className="fas fa-wind"></i> {alert.threat.max_wind_gust_mph} mph
            </span>
          )}
          {alert.threat?.max_hail_size_inches && (
            <span className="obs-alert-threat-item">
              <i className="fas fa-cloud-meatball"></i> {alert.threat.max_hail_size_inches}" hail
            </span>
          )}
          {alert.threat?.snow_amount_max_inches && (
            <span className="obs-alert-threat-item">
              <i className="fas fa-snowflake"></i> {alert.threat.snow_amount_min_inches || 0}-{alert.threat.snow_amount_max_inches}" snow
            </span>
          )}
        </div>

        {/* Issuing office */}
        {alert.sender_name && (
          <div className="obs-alert-office">
            {alert.sender_name}
          </div>
        )}
      </div>
    </div>
  );
};

export const OBSOverlay: React.FC = () => {
  const [alertQueue, setAlertQueue] = useState<Alert[]>([]);
  const [currentAlert, setCurrentAlert] = useState<Alert | null>(null);
  const [displayDuration] = useState(12000); // 12 seconds per alert

  const handleNewAlert = useCallback((alert: Alert) => {
    console.log('[OBS] New alert received:', alert.event_name);
    setAlertQueue(prev => [...prev, alert]);
  }, []);

  // Connect to WebSocket
  useWebSocket({
    url: getWebSocketUrl(),
    onAlert: handleNewAlert,
  });

  // Process queue - show one alert at a time
  useEffect(() => {
    if (!currentAlert && alertQueue.length > 0) {
      const [next, ...rest] = alertQueue;
      setCurrentAlert(next);
      setAlertQueue(rest);
    }
  }, [currentAlert, alertQueue]);

  const handleDismiss = useCallback(() => {
    setCurrentAlert(null);
  }, []);

  return (
    <div className="obs-overlay">
      {currentAlert && (
        <OBSAlert
          key={currentAlert.product_id}
          alert={currentAlert}
          onDismiss={handleDismiss}
          duration={displayDuration}
        />
      )}
    </div>
  );
};

import React, { useState, useEffect, useCallback } from 'react';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';

interface NewAlertNotificationProps {
  alert: Alert | null;
  onDismiss?: () => void;
  duration?: number; // Duration in ms before auto-dismiss
}

export const NewAlertNotification: React.FC<NewAlertNotificationProps> = ({
  alert,
  onDismiss,
  duration = 15000, // 15 seconds default like V1
}) => {
  const [visible, setVisible] = useState(false);
  const [currentAlert, setCurrentAlert] = useState<Alert | null>(null);

  const dismiss = useCallback(() => {
    setVisible(false);
    // Wait for animation to complete before clearing alert
    setTimeout(() => {
      setCurrentAlert(null);
      onDismiss?.();
    }, 700);
  }, [onDismiss]);

  useEffect(() => {
    if (alert && alert !== currentAlert) {
      setCurrentAlert(alert);
      // Small delay to trigger CSS animation
      requestAnimationFrame(() => {
        setVisible(true);
      });

      // Auto-dismiss after duration
      const timer = setTimeout(dismiss, duration);
      return () => clearTimeout(timer);
    }
  }, [alert, currentAlert, duration, dismiss]);

  if (!currentAlert) return null;

  const alertStyle = getAlertStyle(currentAlert.phenomenon, currentAlert.significance);
  const isEmergency = currentAlert.event_name?.toLowerCase().includes('emergency') ||
    currentAlert.threat?.tornado_damage_threat === 'CATASTROPHIC';

  return (
    <div
      className={`new-alert-notification ${visible ? 'visible' : ''} ${isEmergency ? 'emergency' : ''}`}
      style={{
        borderLeftColor: alertStyle.borderColor,
        ['--alert-bg' as string]: alertStyle.backgroundColor,
      }}
      onClick={dismiss}
    >
      <div className="new-alert-header">
        <span className="new-alert-badge">JUST ISSUED</span>
        <span className="new-alert-title">{currentAlert.event_name}</span>
      </div>
      <div className="new-alert-body">
        <div className="new-alert-location">
          <i className="fas fa-map-marker-alt"></i>
          <span>{currentAlert.display_locations || currentAlert.affected_areas?.join(', ')}</span>
        </div>
        {currentAlert.threat?.tornado_detection && (
          <div className="new-alert-tornado-tag">
            <i className="fas fa-exclamation-triangle"></i>
            TORNADO {currentAlert.threat.tornado_detection}
            {currentAlert.threat.tornado_damage_threat &&
              ` - ${currentAlert.threat.tornado_damage_threat}`}
          </div>
        )}
        {currentAlert.threat?.max_wind_gust_mph && (
          <div className="new-alert-threat">
            <i className="fas fa-wind"></i>
            Gusts: {currentAlert.threat.max_wind_gust_mph} mph
          </div>
        )}
        {currentAlert.threat?.max_hail_size_inches && (
          <div className="new-alert-threat">
            <i className="fas fa-cloud-meatball"></i>
            Hail: {currentAlert.threat.max_hail_size_inches}"
          </div>
        )}
      </div>
      <div className="new-alert-dismiss">
        Click to dismiss
      </div>
    </div>
  );
};

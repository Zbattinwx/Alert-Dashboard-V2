import React from 'react';
import type { Alert } from '../types/alert';

interface AlertDetailPaneProps {
  alert: Alert | null;
  isOpen: boolean;
  onClose: () => void;
}

export const AlertDetailPane: React.FC<AlertDetailPaneProps> = ({
  alert,
  isOpen,
  onClose,
}) => {
  if (!alert) return null;

  const formatDateTime = (isoString: string | null) => {
    if (!isoString) return 'N/A';
    const date = new Date(isoString);
    return date.toLocaleString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  };

  return (
    <div className={`alert-detail-pane ${isOpen ? 'open' : ''}`}>
      <div className="detail-pane-header">
        <h3>{alert.event_name}</h3>
        <span className="detail-pane-close" onClick={onClose}>
          <i className="fas fa-times"></i>
        </span>
      </div>

      <div className="detail-pane-content">
        {/* Times */}
        <div className="detail-section">
          <div className="detail-section-title">Timing</div>
          <div className="detail-section-content">
            <div><strong>Issued:</strong> {formatDateTime(alert.issued_time)}</div>
            <div><strong>Effective:</strong> {formatDateTime(alert.effective_time)}</div>
            <div><strong>Expires:</strong> {formatDateTime(alert.expiration_time)}</div>
          </div>
        </div>

        {/* Office */}
        <div className="detail-section">
          <div className="detail-section-title">Issuing Office</div>
          <div className="detail-section-content">
            {alert.issuing_office || 'Unknown'}
          </div>
        </div>

        {/* VTEC */}
        {alert.vtec && (
          <div className="detail-section">
            <div className="detail-section-title">VTEC</div>
            <div className="detail-section-content" style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
              {alert.vtec.raw_vtec}
            </div>
          </div>
        )}

        {/* Threat Info */}
        {(alert.threat.has_tornado || alert.threat.max_hail_size_inches || alert.threat.max_wind_gust_mph) && (
          <div className="detail-section">
            <div className="detail-section-title">Threat Information</div>
            <div className="detail-section-content">
              {alert.threat.has_tornado && (
                <div style={{ color: 'var(--tor-color)', fontWeight: 'bold' }}>
                  <i className="fas fa-exclamation-triangle"></i> TORNADO {alert.threat.tornado_observed ? 'OBSERVED' : 'POSSIBLE'}
                  {alert.threat.tornado_damage_threat && ` - ${alert.threat.tornado_damage_threat}`}
                </div>
              )}
              {alert.threat.max_hail_size_inches && (
                <div>
                  <i className="fas fa-cloud-meatball"></i> Max Hail: {alert.threat.max_hail_size_inches}"
                </div>
              )}
              {alert.threat.max_wind_gust_mph && (
                <div>
                  <i className="fas fa-wind"></i> Max Wind: {alert.threat.max_wind_gust_mph} mph
                </div>
              )}
            </div>
          </div>
        )}

        {/* Affected Areas */}
        <div className="detail-section">
          <div className="detail-section-title">Affected Areas ({alert.affected_areas.length})</div>
          <div className="detail-section-content">
            {alert.affected_areas.join(', ')}
          </div>
        </div>

        {/* Headline */}
        {alert.headline && (
          <div className="detail-section">
            <div className="detail-section-title">Headline</div>
            <div className="detail-section-content">
              {alert.headline}
            </div>
          </div>
        )}

        {/* Description */}
        {alert.description && (
          <div className="detail-section">
            <div className="detail-section-title">Description</div>
            <div className="detail-section-content">
              {alert.description}
            </div>
          </div>
        )}

        {/* Instructions */}
        {alert.instruction && (
          <div className="detail-section">
            <div className="detail-section-title">Instructions</div>
            <div className="detail-section-content" style={{
              backgroundColor: 'var(--bg-tertiary)',
              padding: 'var(--spacing-sm)',
              borderRadius: 'var(--radius-sm)'
            }}>
              {alert.instruction}
            </div>
          </div>
        )}

        {/* Product ID */}
        <div className="detail-section">
          <div className="detail-section-title">Product ID</div>
          <div className="detail-section-content" style={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>
            {alert.product_id}
          </div>
        </div>
      </div>
    </div>
  );
};

import { useRef, useImperativeHandle, forwardRef } from 'react';
import type { Alert } from '../../types/alert';
import { getAlertStyle } from '../../types/alert';

export interface AlertGraphicHandle {
  capture: () => Promise<string | null>;
}

interface AlertGraphicProps {
  alert: Alert;
  format?: 'facebook' | 'bluesky';
}

export const AlertGraphic = forwardRef<AlertGraphicHandle, AlertGraphicProps>(
  ({ alert, format = 'facebook' }, ref) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const height = format === 'facebook' ? 630 : 675;

    const alertStyle = getAlertStyle(alert.phenomenon, alert.significance);

    // Build threat tags
    const threats: string[] = [];
    if (alert.threat.tornado_detection) {
      let t = `Tornado: ${alert.threat.tornado_detection}`;
      if (alert.threat.tornado_damage_threat) t += ` (${alert.threat.tornado_damage_threat})`;
      threats.push(t);
    }
    if (alert.threat.max_wind_gust_mph) {
      let t = `Wind: ${alert.threat.max_wind_gust_mph} MPH`;
      if (alert.threat.wind_damage_threat) t += ` (${alert.threat.wind_damage_threat})`;
      threats.push(t);
    }
    if (alert.threat.max_hail_size_inches) {
      threats.push(`Hail: ${alert.threat.max_hail_size_inches}" diameter`);
    }
    if (alert.threat.snow_amount_max_inches) {
      const min = alert.threat.snow_amount_min_inches || 0;
      threats.push(min ? `Snow: ${min}-${alert.threat.snow_amount_max_inches}"` : `Snow: Up to ${alert.threat.snow_amount_max_inches}"`);
    }
    if (alert.threat.ice_accumulation_inches) {
      threats.push(`Ice: ${alert.threat.ice_accumulation_inches}"`);
    }
    if (alert.threat.flash_flood_detection) {
      let t = `Flooding: ${alert.threat.flash_flood_detection}`;
      if (alert.threat.flash_flood_damage_threat) t += ` (${alert.threat.flash_flood_damage_threat})`;
      threats.push(t);
    }

    const formatExpiration = (iso: string | null) => {
      if (!iso) return '';
      const d = new Date(iso);
      return d.toLocaleString('en-US', {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
      });
    };

    useImperativeHandle(ref, () => ({
      async capture() {
        if (!containerRef.current) return null;
        try {
          const html2canvas = (await import('html2canvas')).default;
          const canvas = await html2canvas(containerRef.current, {
            width: 1200,
            height,
            scale: 2,
            backgroundColor: '#0f172a',
            useCORS: true,
          });
          return canvas.toDataURL('image/png');
        } catch (err) {
          console.error('Failed to capture graphic:', err);
          return null;
        }
      },
    }));

    return (
      <div
        ref={containerRef}
        className="social-graphic"
        style={{ height: `${height}px` }}
      >
        {/* Header with TBF branding */}
        <div className="social-graphic-header">
          <span className="brand-text">THE BATTIN FRONT</span>
        </div>

        {/* Alert type bar */}
        <div
          className="social-graphic-alert-bar"
          style={{ backgroundColor: alertStyle.backgroundColor }}
        >
          <span className="event-name" style={{ color: alertStyle.textColor }}>
            {alert.event_name}
          </span>
        </div>

        {/* Body */}
        <div className="social-graphic-body">
          <div className="locations">
            {alert.display_locations || alert.affected_areas.join(', ')}
          </div>

          {threats.length > 0 && (
            <div className="threats">
              {threats.map((t, i) => (
                <span key={i} className="threat-tag">{t}</span>
              ))}
            </div>
          )}

          <div className="meta-row">
            <span>{alert.sender_name || alert.sender_office}</span>
            {alert.expiration_time && (
              <span>Until {formatExpiration(alert.expiration_time)}</span>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="social-graphic-footer">
          <span>thebattinfront.com</span>
          <span>Data: National Weather Service</span>
        </div>
      </div>
    );
  }
);

AlertGraphic.displayName = 'AlertGraphic';

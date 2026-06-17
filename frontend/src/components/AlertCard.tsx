import React, { useState } from 'react';
import type { Alert } from '../types/alert';
import { getAlertStyle } from '../types/alert';

const GRAPHIC_PHENOMENA = new Set(['TO', 'SV', 'FF', 'FA', 'FL', 'BZ', 'WS', 'IS', 'EW', 'HW']);

// OSM impact-scan result shape (mirrors osm_impact_service.py)
interface ImpactItem { name: string; sub?: string; }
interface ImpactCategory {
  key: string;
  label: string;
  at_risk: boolean;
  items: ImpactItem[];
  total: number;
}
interface ImpactResult {
  total: number;
  counts: Record<string, number>;
  categories: ImpactCategory[];
}

interface AlertCardProps {
  alert: Alert;
  onClick?: (alert: Alert) => void;
  onClear?: (alert: Alert) => void;
  onShare?: (alert: Alert) => void;
}

export const AlertCard: React.FC<AlertCardProps> = ({ alert, onClick, onClear, onShare }) => {
  const [generatingGraphic, setGeneratingGraphic] = useState(false);
  const [scanningImpact, setScanningImpact] = useState(false);
  const [impactResult, setImpactResult] = useState<ImpactResult | null>(null);
  const [impactExpanded, setImpactExpanded] = useState(false);

  // Only warnings carry a polygon worth scanning for impacted places.
  const hasPolygon = Array.isArray(alert.polygon) && alert.polygon.length >= 3;

  const handleGenerateGraphic = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setGeneratingGraphic(true);
    try {
      // Generate + save, then open in new tab
      await fetch(`/api/graphics/alert/${alert.product_id}?save=true`);
      window.open(`/api/graphics/alert/${alert.product_id}`, '_blank');
    } finally {
      setGeneratingGraphic(false);
    }
  };

  // Scan the warning polygon via OSM and push the result to the stream widget.
  const handleScanImpact = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setScanningImpact(true);
    try {
      const res = await fetch(`/api/alerts/${alert.product_id}/impact-scan`, { method: 'POST' });
      if (res.ok) {
        const data: ImpactResult = await res.json();
        setImpactResult(data);
        setImpactExpanded(true); // reveal the names right away after a scan
      }
    } finally {
      setScanningImpact(false);
    }
  };

  const handleClearImpact = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await fetch('/api/impact/clear', { method: 'POST' });
    setImpactResult(null);
  };

  // Tell the radar app to zoom to + flash this alert (and show its info card).
  const handleFocusRadar = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await fetch(`/api/alerts/${alert.product_id}/focus`, { method: 'POST' });
  };
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

  const handleClearClick = (e: React.MouseEvent) => {
    e.stopPropagation(); // Prevent the main card click from firing
    onClear?.(alert);
  };

  const handleShareClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onShare?.(alert);
  };

  // Determine if this is a high-threat alert that should visually stand out.
  // A Tornado Emergency is the single most severe warning the NWS issues — it
  // outranks everything else and always gets the strongest treatment.
  const isTornadoEmergency = alert.threat.tornado_emergency === true;
  const isCatastrophic =
    alert.threat.tornado_damage_threat === 'CATASTROPHIC' ||
    alert.threat.thunderstorm_damage_threat === 'CATASTROPHIC';
  const isHighThreat =
    isTornadoEmergency ||
    alert.threat.tornado_detection === 'OBSERVED' ||
    alert.threat.tornado_damage_threat === 'CONSIDERABLE' ||
    isCatastrophic ||
    alert.threat.thunderstorm_damage_threat === 'CONSIDERABLE' ||
    alert.threat.thunderstorm_damage_threat === 'DESTRUCTIVE' ||
    alert.threat.flash_flood_damage_threat === 'CONSIDERABLE' ||
    alert.threat.flash_flood_damage_threat === 'CATASTROPHIC';

  // Build CSS class list
  const cardClasses = ['alert-card'];
  if (isHighThreat) {
    cardClasses.push('high-threat');
    if (isCatastrophic) cardClasses.push('threat-catastrophic');
    if (alert.phenomenon === 'SV') cardClasses.push('threat-svr');
    if (alert.phenomenon === 'FF') cardClasses.push('threat-ffw');
  }
  // Emergency class is independent of phenomenon-specific glows — it overrides
  // them with the most aggressive styling.
  if (isTornadoEmergency) cardClasses.push('threat-tornado-emergency');

  return (
    <div
      className={cardClasses.join(' ')}
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
        {GRAPHIC_PHENOMENA.has(alert.phenomenon) && (
          <span
            className="alert-card-share"
            onClick={handleGenerateGraphic}
            title="Generate broadcast graphic"
            style={{ opacity: generatingGraphic ? 0.5 : 1 }}
          >
            <i className={generatingGraphic ? 'fas fa-spinner fa-spin' : 'fas fa-image'}></i>
          </span>
        )}
        {hasPolygon && (
          <span
            className="alert-card-share"
            onClick={handleScanImpact}
            title="Scan impacted places (OSM) and push to stream"
            style={{ opacity: scanningImpact ? 0.5 : 1 }}
          >
            <i className={scanningImpact ? 'fas fa-spinner fa-spin' : 'fas fa-location-crosshairs'}></i>
          </span>
        )}
        <span
          className="alert-card-share"
          onClick={handleFocusRadar}
          title="Show on radar (zoom + flash)"
        >
          <i className="fas fa-satellite-dish"></i>
        </span>
        <span className="alert-card-share" onClick={handleShareClick} title="Share to social media">
          <i className="fas fa-share-alt"></i>
        </span>
        <span className="alert-card-clear" onClick={handleClearClick}>X</span>
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

        {/* Tornado Emergency — highest tier, always the loudest badge */}
        {isTornadoEmergency && (
          <div className="tornado-emergency-badge" style={{
            marginTop: '8px',
            padding: '6px 10px',
            backgroundColor: 'var(--tor-color)',
            color: 'white',
            borderRadius: '4px',
            fontSize: '0.85rem',
            fontWeight: 800,
            letterSpacing: '0.04em',
            display: 'inline-block'
          }}>
            <i className="fas fa-triangle-exclamation" style={{ marginRight: '6px' }}></i>
            TORNADO EMERGENCY
          </div>
        )}

        {/* Tornado tag (suppressed when an emergency badge is already shown) */}
        {alert.threat.tornado_detection && !isTornadoEmergency && (
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

        {/* Thunderstorm Damage Threat */}
        {alert.threat.thunderstorm_damage_threat && (
          <div style={{
            marginTop: '8px',
            padding: '4px 8px',
            backgroundColor: alert.threat.thunderstorm_damage_threat === 'DESTRUCTIVE' ? '#D50000' : '#FF6D00', // Red for Destructive, Orange for Considerable
            color: 'white',
            borderRadius: '4px',
            fontSize: '0.75rem',
            fontWeight: 'bold',
            display: 'inline-block',
            marginLeft: alert.threat.tornado_detection ? '8px' : '0'
          }}>
            <i className="fas fa-bolt" style={{ marginRight: '4px' }}></i>
            THUNDERSTORM DAMAGE: {alert.threat.thunderstorm_damage_threat}
          </div>
        )}

        {/* OSM impact scan result — live on stream until cleared */}
        {impactResult && (
          <div style={{
            marginTop: '8px',
            padding: '8px 10px',
            backgroundColor: 'rgba(99, 102, 241, 0.12)',
            border: '1px solid rgba(99, 102, 241, 0.4)',
            borderRadius: '4px',
            fontSize: '0.75rem',
          }}>
            {/* Summary line — click to expand/collapse the names */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <span
                style={{ cursor: 'pointer', flex: 1 }}
                onClick={(e) => { e.stopPropagation(); setImpactExpanded(v => !v); }}
                title="Show / hide the impacted place names"
              >
                <i className={`fas fa-chevron-${impactExpanded ? 'down' : 'right'}`} style={{ marginRight: '6px', fontSize: '0.65rem' }}></i>
                <i className="fas fa-people-roof" style={{ marginRight: '4px' }}></i>
                In path: <strong>{impactResult.total}</strong>
                {impactResult.counts.mobile_home ? ` · ${impactResult.counts.mobile_home} mobile-home` : ''}
                {impactResult.counts.schools ? ` · ${impactResult.counts.schools} schools` : ''}
                {impactResult.counts.medical ? ` · ${impactResult.counts.medical} medical` : ''}
              </span>
              <span style={{ color: 'var(--accent-blue, #818cf8)', fontWeight: 600, cursor: 'pointer' }}
                onClick={handleClearImpact} title="Hide the impact panel on stream">
                Clear from stream
              </span>
            </div>

            {/* Expanded named lists */}
            {impactExpanded && (
              <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {impactResult.categories.filter(c => c.items.length > 0).map(cat => (
                  <div key={cat.key}>
                    <div style={{
                      fontSize: '0.68rem',
                      fontWeight: 700,
                      letterSpacing: '0.04em',
                      textTransform: 'uppercase',
                      color: cat.at_risk ? '#ff6b81' : 'var(--accent-blue, #818cf8)',
                      marginBottom: '2px',
                    }}>
                      {cat.at_risk && <i className="fas fa-triangle-exclamation" style={{ marginRight: '4px' }}></i>}
                      {cat.label} ({cat.total})
                    </div>
                    <div style={{ color: 'var(--text-primary)', lineHeight: 1.5 }}>
                      {cat.items.map((it, i) => (
                        <span key={i}>
                          {it.name}{it.sub ? <span style={{ color: 'var(--text-muted)' }}> ({it.sub})</span> : ''}
                          {i < cat.items.length - 1 ? ', ' : ''}
                        </span>
                      ))}
                      {cat.total > cat.items.length ? <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}> +{cat.total - cat.items.length} more</span> : ''}
                    </div>
                  </div>
                ))}
              </div>
            )}
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

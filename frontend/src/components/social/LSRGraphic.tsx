import { useRef, useImperativeHandle, forwardRef } from 'react';
import type { StormReport } from '../../types/lsr';

export interface LSRGraphicHandle {
  capture: () => Promise<string | null>;
}

interface LSRGraphicProps {
  reports: StormReport[];
  format?: 'facebook' | 'bluesky';
}

const TYPE_COLORS: Record<string, string> = {
  TORNADO: '#ef4444',
  HAIL: '#22c55e',
  'TSTM WND GST': '#3b82f6',
  'TSTM WND DMG': '#3b82f6',
  'FLASH FLOOD': '#a855f7',
  FLOOD: '#a855f7',
  SNOW: '#93c5fd',
  ICE: '#67e8f9',
  'HEAVY RAIN': '#6366f1',
  'FUNNEL CLOUD': '#f97316',
};

/**
 * Parse magnitude string to a number for sorting.
 * Handles "5.2", "70 MPH", "1.75 INCH", "E1.75 INCH", "M70 MPH", etc.
 */
function parseMagnitude(mag: string | null): number {
  if (!mag) return 0;
  let s = mag.trim().toUpperCase();
  // Strip E (estimated) or M (measured) prefix
  if (s.length > 0 && (s[0] === 'E' || s[0] === 'M')) s = s.slice(1).trim();
  const match = s.match(/(\d+\.?\d*)/);
  return match ? parseFloat(match[1]) : 0;
}

const TYPE_PRIORITY: Record<string, number> = {
  TORNADO: 100,
  'FUNNEL CLOUD': 90,
  'TSTM WND DMG': 80,
  'TSTM WND GST': 70,
  HAIL: 65,
  'FLASH FLOOD': 60,
  FLOOD: 55,
  'HEAVY RAIN': 40,
  SNOW: 35,
  ICE: 30,
};

function sortBySignificance(reports: StormReport[]): StormReport[] {
  return [...reports].sort((a, b) => {
    // Tornadoes always first
    const aTornado = a.report_type === 'TORNADO' ? 1 : 0;
    const bTornado = b.report_type === 'TORNADO' ? 1 : 0;
    if (aTornado !== bTornado) return bTornado - aTornado;

    // Then by magnitude (normalize wind by /10 for cross-type comparison)
    let aMag = parseMagnitude(a.magnitude);
    let bMag = parseMagnitude(b.magnitude);
    if (a.report_type.includes('WND')) aMag /= 10;
    if (b.report_type.includes('WND')) bMag /= 10;
    if (aMag !== bMag) return bMag - aMag;

    // Tiebreak by report type priority
    return (TYPE_PRIORITY[b.report_type] || 10) - (TYPE_PRIORITY[a.report_type] || 10);
  });
}

export const LSRGraphic = forwardRef<LSRGraphicHandle, LSRGraphicProps>(
  ({ reports, format = 'facebook' }, ref) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const height = format === 'facebook' ? 630 : 675;
    const sorted = sortBySignificance(reports);
    const displayReports = sorted.slice(0, 10);

    const formatTime = (iso?: string | null) => {
      if (!iso) return '';
      const d = new Date(iso);
      return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
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
          console.error('Failed to capture LSR graphic:', err);
          return null;
        }
      },
    }));

    return (
      <div ref={containerRef} className="lsr-graphic" style={{ height: `${height}px` }}>
        <div className="lsr-graphic-header">
          <h2>Storm Reports Summary</h2>
          <div className="subtitle">
            {reports.length > 10
              ? `Top 10 most significant out of ${reports.length} reports`
              : `${reports.length} report${reports.length !== 1 ? 's' : ''} received`
            }
          </div>
        </div>

        <div className="lsr-graphic-body">
          {displayReports.map((r, i) => (
            <div key={i} className="lsr-report-row">
              <span
                className="report-type"
                style={{ color: TYPE_COLORS[r.report_type] || '#fff' }}
              >
                {r.report_type}
              </span>
              <span className="report-mag">{r.magnitude || ''}</span>
              <span className="report-location">
                {r.city ? `Near ${r.city}` : ''}{r.state ? `, ${r.state}` : ''}
              </span>
              <span className="report-time">{formatTime(r.valid_time)}</span>
            </div>
          ))}
          {reports.length > 10 && (
            <div style={{ padding: '10px 0', color: 'rgba(255,255,255,0.5)', fontSize: '16px' }}>
              + {reports.length - 10} more reports
            </div>
          )}
        </div>

        <div className="lsr-graphic-footer">
          <span>thebattinfront.com</span>
          <span>Data: National Weather Service</span>
        </div>
      </div>
    );
  }
);

LSRGraphic.displayName = 'LSRGraphic';

import React, { useEffect, useState, useCallback } from 'react';
import { apiUrl } from '../utils/api';

interface AFDOffice {
  office: string;
  wfo_name: string;
  latest_received: string;
  count: number;
}

interface AFDData {
  office: string;
  wfo_name: string;
  received_at: string;
  wmo_header: string | null;
  raw_text: string;
  sections: Record<string, string>;
}

interface AFDResponse {
  source: string;
  afd: AFDData;
}

// Common WFOs for the configured filter states
const DEFAULT_OFFICES = [
  { code: 'CLE', name: 'Cleveland OH' },
  { code: 'ILN', name: 'Wilmington OH' },
  { code: 'IND', name: 'Indianapolis IN' },
  { code: 'IWX', name: 'Northern Indiana' },
  { code: 'PBZ', name: 'Pittsburgh PA' },
  { code: 'RLX', name: 'Charleston WV' },
  { code: 'LOT', name: 'Chicago IL' },
  { code: 'ILX', name: 'Lincoln IL' },
  { code: 'BMX', name: 'Birmingham AL' },
  { code: 'HUN', name: 'Huntsville AL' },
  { code: 'JAN', name: 'Jackson MS' },
  { code: 'ICT', name: 'Wichita KS' },
  { code: 'OUN', name: 'Norman OK' },
  { code: 'TSA', name: 'Tulsa OK' },
  { code: 'JAX', name: 'Jacksonville FL' },
  { code: 'TBW', name: 'Tampa Bay FL' },
  { code: 'MIA', name: 'Miami FL' },
  { code: 'FWD', name: 'Fort Worth TX' },
  { code: 'HGX', name: 'Houston TX' },
  { code: 'EWX', name: 'Austin/San Antonio TX' },
];

const formatTimestamp = (iso: string): string => {
  const d = new Date(iso);
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
};

export const AFDSection: React.FC = () => {
  const [nwwsOffices, setNwwsOffices] = useState<AFDOffice[]>([]);
  const [selectedOffice, setSelectedOffice] = useState('CLE');
  const [customOffice, setCustomOffice] = useState('');
  const [afd, setAfd] = useState<AFDData | null>(null);
  const [source, setSource] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());

  const fetchOffices = useCallback(async () => {
    try {
      const resp = await fetch(apiUrl('/api/afd'));
      if (resp.ok) {
        const data = await resp.json();
        setNwwsOffices(data.offices);
      }
    } catch {
      // Non-critical
    }
  }, []);

  const fetchAFD = useCallback(async (office: string) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(apiUrl(`/api/afd/${office}?fallback=true`));
      if (!resp.ok) {
        if (resp.status === 404) {
          setAfd(null);
          setError(`No AFD available for ${office.toUpperCase()}`);
          return;
        }
        throw new Error(`HTTP ${resp.status}`);
      }
      const data: AFDResponse = await resp.json();
      setAfd(data.afd);
      setSource(data.source);
      // Expand all sections by default
      setExpandedSections(new Set(Object.keys(data.afd.sections)));
    } catch (err) {
      setAfd(null);
      setError(err instanceof Error ? err.message : 'Failed to fetch AFD');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOffices();
  }, [fetchOffices]);

  useEffect(() => {
    if (selectedOffice) {
      fetchAFD(selectedOffice);
    }
    // Auto-refresh every 5 minutes
    const interval = setInterval(() => {
      if (selectedOffice) fetchAFD(selectedOffice);
    }, 5 * 60_000);
    return () => clearInterval(interval);
  }, [selectedOffice, fetchAFD]);

  const toggleSection = (name: string) => {
    setExpandedSections(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const handleCustomOffice = () => {
    const val = customOffice.trim().toUpperCase();
    if (val.length >= 3) {
      setSelectedOffice(val);
      setCustomOffice('');
    }
  };

  // Merge default offices with NWWS-cached ones
  const allOfficeCodes = new Set(DEFAULT_OFFICES.map(o => o.code));
  const extraOffices = nwwsOffices
    .filter(o => !allOfficeCodes.has(o.office))
    .map(o => ({ code: o.office, name: o.wfo_name.replace('NWS ', '') }));

  return (
    <div className="section active afd-section">
      <h2 className="section-title">Area Forecast Discussions</h2>

      <div className="afd-controls">
        <div className="afd-controls-left">
          <label>
            <span>Office:</span>
            <select
              value={selectedOffice}
              onChange={(e) => setSelectedOffice(e.target.value)}
              className="afd-select"
            >
              {DEFAULT_OFFICES.map(o => (
                <option key={o.code} value={o.code}>{o.code} - {o.name}</option>
              ))}
              {extraOffices.length > 0 && (
                <optgroup label="From NWWS Feed">
                  {extraOffices.map(o => (
                    <option key={o.code} value={o.code}>{o.code} - {o.name}</option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
          <div className="afd-custom-office">
            <input
              className="afd-office-input"
              placeholder="Or enter office..."
              value={customOffice}
              onChange={(e) => setCustomOffice(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === 'Enter' && handleCustomOffice()}
              maxLength={4}
            />
            <button onClick={handleCustomOffice} className="afd-go-btn">Go</button>
          </div>
        </div>
        <div className="afd-controls-right">
          {source && <span className="afd-source-badge">Source: {source.toUpperCase()}</span>}
          <button onClick={() => fetchAFD(selectedOffice)} className="afd-refresh-btn" disabled={loading}>
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i> Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="afd-error">
          <i className="fas fa-exclamation-triangle"></i> {error}
        </div>
      )}

      {loading && !afd ? (
        <div className="afd-loading">
          <i className="fas fa-spinner fa-spin"></i> Loading AFD...
        </div>
      ) : afd ? (
        <div className="afd-content">
          <div className="afd-header-info">
            <span className="afd-office-name">{afd.wfo_name}</span>
            <span className="afd-timestamp">{formatTimestamp(afd.received_at)}</span>
          </div>

          {Object.keys(afd.sections).length > 0 ? (
            <div className="afd-sections">
              {Object.entries(afd.sections).map(([name, text]) => (
                <div key={name} className="afd-section-block">
                  <div className="afd-section-header" onClick={() => toggleSection(name)}>
                    <i className={`fas fa-chevron-${expandedSections.has(name) ? 'down' : 'right'}`}></i>
                    <span>{name}</span>
                  </div>
                  {expandedSections.has(name) && (
                    <pre className="afd-section-text">{text}</pre>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <pre className="afd-raw-text">{afd.raw_text}</pre>
          )}
        </div>
      ) : null}
    </div>
  );
};

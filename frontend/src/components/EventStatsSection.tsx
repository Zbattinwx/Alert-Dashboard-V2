import React, { useEffect, useState, useCallback, useRef } from 'react';
import { apiUrl } from '../utils/api';

interface TimelineEvent {
  time: string;
  event_type: string;
  event_name: string;
  phenomenon: string;
  significance: string;
  location: string;
  is_emergency: boolean;
}

interface PhenomenonStats {
  phenomenon: string;
  label: string;
  total: number;
  by_significance: Record<string, number>;
}

interface LsrStats {
  total: number;
  tornado_reports: number;
  hail_reports: number;
  wind_reports: number;
  max_hail_in: number | null;
  max_wind_mph: number | null;
  by_type: Record<string, number>;
}

interface EventStats {
  session_start: string;
  session_duration: string;
  session_duration_s: number;
  total_issued: number;
  current_active: number;
  peak_concurrent: number;
  tornado_emergency_count: number;
  pds_count: number;
  max_hail_in: number | null;
  max_wind_mph: number | null;
  by_phenomenon: PhenomenonStats[];
  timeline: TimelineEvent[];
  lsr: LsrStats;
}

const PHENOMENON_COLORS: Record<string, string> = {
  TO: '#ff0000',
  SV: '#ffa500',
  FF: '#00cc00',
  EW: '#ff8c00',
  BZ: '#6699ff',
  WS: '#8866ff',
  HW: '#ccaa00',
  SQ: '#8899bb',
  IS: '#9955aa',
  LE: '#5577cc',
};

const SIG_LABELS: Record<string, string> = {
  W: 'Warnings',
  A: 'Watches',
  Y: 'Advisories',
};

function StatCard({ label, value, sub, color, icon }: {
  label: string; value: string | number; sub?: string; color?: string; icon?: string;
}) {
  return (
    <div style={{
      backgroundColor: 'var(--bg-secondary)',
      borderRadius: '8px',
      padding: '14px 16px',
      border: '1px solid var(--border-color)',
      borderLeft: color ? `4px solid ${color}` : '1px solid var(--border-color)',
    }}>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '4px' }}>
        {icon && <i className={`fas ${icon}`} style={{ marginRight: '4px' }}></i>}{label}
      </div>
      <div style={{ fontSize: '1.7rem', fontWeight: 700, color: color || 'var(--text-primary)', lineHeight: 1 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginTop: '4px' }}>{sub}</div>}
    </div>
  );
}

function ageStr(isoTime: string): string {
  const mins = Math.round((Date.now() - new Date(isoTime).getTime()) / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  const m = mins % 60;
  return `${hrs}h ${m}m ago`;
}

function timeStr(isoTime: string): string {
  const d = new Date(isoTime);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export const EventStatsSection: React.FC = () => {
  const [stats, setStats] = useState<EventStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/event-stats'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: EventStats = await res.json();
      setStats(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStats();
    refreshTimer.current = setInterval(fetchStats, 15000);
    return () => { if (refreshTimer.current) clearInterval(refreshTimer.current); };
  }, [fetchStats]);

  const handleReset = async () => {
    if (!window.confirm('Reset event stats? This clears all counters and starts a new session.')) return;
    setResetting(true);
    setResetMsg(null);
    try {
      const res = await fetch(apiUrl('/api/event-stats/reset'), { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setResetMsg('Session reset. Stats cleared.');
      await fetchStats();
    } catch (e) {
      setResetMsg(e instanceof Error ? e.message : 'Reset failed');
    } finally {
      setResetting(false);
    }
  };

  const timelineEventColor = (ev: TimelineEvent) => {
    if (ev.is_emergency) return '#ff0000';
    if (ev.event_type === 'session_reset') return 'var(--text-secondary)';
    if (ev.event_type === 'alert_expired') return '#666';
    return PHENOMENON_COLORS[ev.phenomenon] || 'var(--primary-color)';
  };

  return (
    <div className="section active">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px', flexWrap: 'wrap', gap: '8px' }}>
        <h2 className="section-title" style={{ margin: 0 }}>
          <i className="fas fa-chart-bar" style={{ marginRight: '8px', color: 'var(--primary-color)' }}></i>
          Event Statistics
        </h2>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button
            onClick={fetchStats}
            disabled={loading}
            style={{ padding: '5px 10px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', cursor: 'pointer', fontSize: '0.8rem' }}
          >
            <i className={`fas fa-sync${loading ? ' fa-spin' : ''}`}></i>
          </button>
          <button
            onClick={handleReset}
            disabled={resetting}
            style={{ padding: '5px 12px', borderRadius: '4px', border: '1px solid #cc3333', backgroundColor: 'rgba(200,50,50,0.12)', color: '#ff6060', cursor: 'pointer', fontSize: '0.8rem' }}
          >
            <i className="fas fa-flag"></i> New Event
          </button>
        </div>
      </div>

      {resetMsg && (
        <div style={{ padding: '8px 12px', backgroundColor: 'rgba(50,200,100,0.12)', border: '1px solid #33aa66', borderRadius: '6px', color: '#44cc77', fontSize: '0.85rem', marginBottom: '16px' }}>
          <i className="fas fa-check-circle"></i> {resetMsg}
        </div>
      )}

      {error && (
        <div style={{ padding: '8px 12px', backgroundColor: 'rgba(255,60,60,0.12)', borderRadius: '6px', color: '#ff6060', fontSize: '0.85rem', marginBottom: '16px' }}>
          <i className="fas fa-exclamation-triangle"></i> {error}
        </div>
      )}

      {!stats && loading ? (
        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-secondary)' }}>
          <i className="fas fa-spinner fa-spin" style={{ fontSize: '1.5rem' }}></i>
        </div>
      ) : stats && (
        <>
          {/* Session info bar */}
          <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginBottom: '16px', display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
            <span><i className="fas fa-clock" style={{ marginRight: '4px' }}></i>Session: {stats.session_duration}</span>
            <span><i className="fas fa-calendar-alt" style={{ marginRight: '4px' }}></i>Started: {new Date(stats.session_start).toLocaleString()}</span>
            <span style={{ marginLeft: 'auto', fontSize: '0.72rem', opacity: 0.6 }}>Auto-refreshes every 15s</span>
          </div>

          {/* Top-level stat cards */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '10px', marginBottom: '20px' }}>
            <StatCard label="Total Issued" value={stats.total_issued} icon="fa-bell" color="var(--primary-color)" />
            <StatCard label="Active Now" value={stats.current_active} icon="fa-bolt" color={stats.current_active > 0 ? '#ffaa00' : undefined} />
            <StatCard label="Peak Concurrent" value={stats.peak_concurrent} icon="fa-chart-line" />
            {stats.tornado_emergency_count > 0 && (
              <StatCard label="Tornado Emergencies" value={stats.tornado_emergency_count} icon="fa-exclamation-circle" color="#ff0000" />
            )}
            {stats.pds_count > 0 && (
              <StatCard label="PDS Events" value={stats.pds_count} icon="fa-radiation" color="#cc0088" />
            )}
            {(stats.max_hail_in !== null || stats.lsr?.max_hail_in !== null) && (
              <StatCard
                label="Max Hail"
                value={`${Math.max(stats.max_hail_in ?? 0, stats.lsr?.max_hail_in ?? 0).toFixed(2)}"`}
                icon="fa-cloud-meatball"
                color="#88ccff"
              />
            )}
            {(stats.max_wind_mph !== null || stats.lsr?.max_wind_mph !== null) && (
              <StatCard
                label="Max Wind Gust"
                value={`${Math.max(stats.max_wind_mph ?? 0, stats.lsr?.max_wind_mph ?? 0).toFixed(0)} mph`}
                icon="fa-wind"
                color="#ffaa44"
              />
            )}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '20px' }}>
            {/* Alert breakdown by phenomenon */}
            <div style={{ backgroundColor: 'var(--bg-secondary)', borderRadius: '8px', padding: '14px', border: '1px solid var(--border-color)' }}>
              <h3 style={{ margin: '0 0 12px', fontSize: '0.85rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                <i className="fas fa-exclamation-triangle" style={{ marginRight: '6px' }}></i>Alerts Issued
              </h3>
              {stats.by_phenomenon.length === 0 ? (
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', margin: 0 }}>No alerts this session yet.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {stats.by_phenomenon.map(p => (
                    <div key={p.phenomenon} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <div style={{ width: '3px', height: '28px', borderRadius: '2px', backgroundColor: PHENOMENON_COLORS[p.phenomenon] || '#888', flexShrink: 0 }} />
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                          <span style={{ fontSize: '0.82rem', color: 'var(--text-primary)', fontWeight: 500 }}>{p.label}</span>
                          <span style={{ fontSize: '1rem', fontWeight: 700, color: PHENOMENON_COLORS[p.phenomenon] || 'var(--text-primary)' }}>{p.total}</span>
                        </div>
                        <div style={{ display: 'flex', gap: '6px', marginTop: '1px' }}>
                          {Object.entries(p.by_significance).map(([sig, count]) => (
                            <span key={sig} style={{ fontSize: '0.68rem', color: 'var(--text-secondary)' }}>
                              {SIG_LABELS[sig] || sig}: {count}
                            </span>
                          ))}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* LSR Summary */}
            {stats.lsr && stats.lsr.total > 0 && (
              <div style={{ backgroundColor: 'var(--bg-secondary)', borderRadius: '8px', padding: '14px', border: '1px solid var(--border-color)' }}>
                <h3 style={{ margin: '0 0 12px', fontSize: '0.85rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  <i className="fas fa-bullhorn" style={{ marginRight: '6px' }}></i>Storm Reports (24h)
                </h3>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                  {stats.lsr.tornado_reports > 0 && (
                    <div style={{ textAlign: 'center', padding: '8px', backgroundColor: 'rgba(255,0,0,0.1)', borderRadius: '6px' }}>
                      <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#ff4444' }}>{stats.lsr.tornado_reports}</div>
                      <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>Tornado</div>
                    </div>
                  )}
                  {stats.lsr.hail_reports > 0 && (
                    <div style={{ textAlign: 'center', padding: '8px', backgroundColor: 'rgba(100,180,255,0.1)', borderRadius: '6px' }}>
                      <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#88ccff' }}>{stats.lsr.hail_reports}</div>
                      <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>Hail</div>
                    </div>
                  )}
                  {stats.lsr.wind_reports > 0 && (
                    <div style={{ textAlign: 'center', padding: '8px', backgroundColor: 'rgba(255,170,0,0.1)', borderRadius: '6px' }}>
                      <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#ffaa44' }}>{stats.lsr.wind_reports}</div>
                      <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>Wind</div>
                    </div>
                  )}
                  {stats.lsr.total - stats.lsr.tornado_reports - stats.lsr.hail_reports - stats.lsr.wind_reports > 0 && (
                    <div style={{ textAlign: 'center', padding: '8px', backgroundColor: 'rgba(150,150,150,0.1)', borderRadius: '6px' }}>
                      <div style={{ fontSize: '1.4rem', fontWeight: 700, color: '#aaa' }}>
                        {stats.lsr.total - stats.lsr.tornado_reports - stats.lsr.hail_reports - stats.lsr.wind_reports}
                      </div>
                      <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>Other</div>
                    </div>
                  )}
                </div>
                <div style={{ marginTop: '10px', fontSize: '0.75rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
                  {stats.lsr.total} total reports in last 24 hours
                </div>
              </div>
            )}
          </div>

          {/* Timeline */}
          {stats.timeline.length > 0 && (
            <div style={{ backgroundColor: 'var(--bg-secondary)', borderRadius: '8px', padding: '14px', border: '1px solid var(--border-color)' }}>
              <h3 style={{ margin: '0 0 12px', fontSize: '0.85rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                <i className="fas fa-history" style={{ marginRight: '6px' }}></i>Event Timeline
              </h3>
              <div style={{ maxHeight: '320px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                {stats.timeline.map((ev, i) => (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '6px 8px',
                    borderRadius: '4px', backgroundColor: ev.is_emergency ? 'rgba(255,0,0,0.08)' : 'transparent',
                    borderLeft: `3px solid ${timelineEventColor(ev)}`,
                  }}>
                    <span style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', fontFamily: 'monospace', flexShrink: 0, paddingTop: '1px' }}>
                      {timeStr(ev.time)}
                    </span>
                    <div style={{ flex: 1 }}>
                      <span style={{ fontSize: '0.82rem', color: ev.is_emergency ? '#ff4444' : 'var(--text-primary)', fontWeight: ev.is_emergency ? 700 : 400 }}>
                        {ev.event_name}
                      </span>
                      {ev.location && (
                        <span style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', marginLeft: '6px' }}>
                          {ev.location}
                        </span>
                      )}
                    </div>
                    <span style={{ fontSize: '0.68rem', color: 'var(--text-secondary)', flexShrink: 0 }}>
                      {ageStr(ev.time)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
};

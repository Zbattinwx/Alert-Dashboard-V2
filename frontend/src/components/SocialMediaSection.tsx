import React, { useState, useEffect, useRef, useCallback } from 'react';
import type { Alert } from '../types/alert';
import type { StormReport } from '../types/lsr';
import type { SocialStatus, PostHistoryItem } from '../types/social';
import { ComposeModal } from './social/ComposeModal';
import { HeadlinesGraphic, type HeadlinesGraphicHandle, type Headline } from './social/HeadlinesGraphic';
import { LSR_TYPE_COLORS } from '../types/lsr';
import { apiUrl } from '../utils/api';

interface SocialMediaSectionProps {
  alerts: Alert[];
}

type TabId = 'alerts' | 'reports' | 'headlines' | 'compose' | 'history';

export const SocialMediaSection: React.FC<SocialMediaSectionProps> = ({ alerts }) => {
  const [status, setStatus] = useState<SocialStatus | null>(null);
  const [postHistory, setPostHistory] = useState<PostHistoryItem[]>([]);
  const [composeOpen, setComposeOpen] = useState(false);
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null);
  const [selectedReports, setSelectedReports] = useState<StormReport[]>([]);
  const [composeMode, setComposeMode] = useState<'alert' | 'lsr' | 'quick' | 'headlines'>('quick');
  const [quickMessage, setQuickMessage] = useState('');
  const [activeTab, setActiveTab] = useState<TabId>('headlines');

  // Storm reports state
  const [stormReports, setStormReports] = useState<StormReport[]>([]);
  const [reportsLoading, setReportsLoading] = useState(false);
  const [reportTimeRange, setReportTimeRange] = useState('24');

  // Headlines state
  const [headlines, setHeadlines] = useState<Headline[]>([]);
  const [headlinesWfo, setHeadlinesWfo] = useState('');
  const [headlinesReceivedAt, setHeadlinesReceivedAt] = useState('');
  const [headlinesOffice, setHeadlinesOffice] = useState('ILN');
  const [headlinesLoading, setHeadlinesLoading] = useState(false);
  const [headlinesError, setHeadlinesError] = useState<string | null>(null);
  const [headlinesImageUrl, setHeadlinesImageUrl] = useState<string | null>(null);
  const headlinesGraphicRef = useRef<HeadlinesGraphicHandle>(null);

  // Fetch status, history, and storm reports on mount
  useEffect(() => {
    fetch(apiUrl('/api/social/status'))
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => {});
    fetchHistory();
    fetchStormReports();
  }, []);

  useEffect(() => {
    fetchStormReports();
  }, [reportTimeRange]);

  const fetchHistory = () => {
    fetch(apiUrl('/api/social/history'))
      .then((r) => r.json())
      .then((data) => setPostHistory(data.posts || []))
      .catch(() => {});
  };

  const fetchStormReports = async () => {
    setReportsLoading(true);
    try {
      const resp = await fetch(apiUrl(`/api/lsr/all?hours=${reportTimeRange}`));
      const data = await resp.json();
      setStormReports(data.reports || []);
    } catch {
      setStormReports([]);
    } finally {
      setReportsLoading(false);
    }
  };

  const fetchHeadlines = async (office?: string) => {
    const ofc = office || headlinesOffice;
    setHeadlinesLoading(true);
    setHeadlinesError(null);
    setHeadlinesImageUrl(null);
    try {
      const resp = await fetch(apiUrl(`/api/afd/${ofc}/headlines?count=4`));
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: 'Failed to fetch headlines' }));
        throw new Error(err.detail || 'Failed to fetch headlines');
      }
      const data = await resp.json();
      setHeadlines(data.headlines || []);
      setHeadlinesWfo(data.wfo_name || '');
      setHeadlinesReceivedAt(data.received_at || '');
    } catch (err) {
      setHeadlines([]);
      setHeadlinesError(err instanceof Error ? err.message : 'Failed to fetch headlines');
    } finally {
      setHeadlinesLoading(false);
    }
  };

  // Fetch headlines on mount
  useEffect(() => {
    fetchHeadlines();
  }, []);

  // Capture headlines graphic after it renders
  const captureHeadlinesGraphic = useCallback(async () => {
    if (headlinesGraphicRef.current && headlines.length > 0) {
      // Small delay to let the graphic render
      await new Promise((r) => setTimeout(r, 400));
      const dataUrl = await headlinesGraphicRef.current.capture();
      setHeadlinesImageUrl(dataUrl);
    }
  }, [headlines]);

  useEffect(() => {
    if (headlines.length > 0) {
      captureHeadlinesGraphic();
    }
  }, [headlines, captureHeadlinesGraphic]);

  const handleShareHeadlines = () => {
    if (!headlinesImageUrl || headlines.length === 0) return;
    setSelectedAlert(null);
    setSelectedReports([]);
    setComposeMode('headlines');
    setComposeOpen(true);
  };

  // Group reports by type for summary
  const reportsByType = stormReports.reduce<Record<string, StormReport[]>>((acc, r) => {
    const type = r.report_type;
    if (!acc[type]) acc[type] = [];
    acc[type].push(r);
    return acc;
  }, {});

  const handleShareAlert = (alert: Alert) => {
    setSelectedAlert(alert);
    setSelectedReports([]);
    setComposeMode('alert');
    setComposeOpen(true);
  };

  const handleShareSingleReport = (report: StormReport) => {
    setSelectedAlert(null);
    setSelectedReports([report]);
    setComposeMode('lsr');
    setComposeOpen(true);
  };

  const handleShareReportSummary = () => {
    setSelectedAlert(null);
    setSelectedReports(stormReports);
    setComposeMode('lsr');
    setComposeOpen(true);
  };

  const handleShareReportType = (type: string) => {
    setSelectedAlert(null);
    setSelectedReports(reportsByType[type] || []);
    setComposeMode('lsr');
    setComposeOpen(true);
  };

  const handleQuickCompose = () => {
    setSelectedAlert(null);
    setSelectedReports([]);
    setComposeMode('quick');
    setComposeOpen(true);
  };

  const handleModalClose = () => {
    setComposeOpen(false);
    setSelectedAlert(null);
    setSelectedReports([]);
    fetchHistory();
  };

  const formatTimeAgo = (isoString: string) => {
    const diff = Date.now() - new Date(isoString).getTime();
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return 'Just now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  };

  const formatTime = (isoString: string | null) => {
    if (!isoString) return '';
    const d = new Date(isoString);
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
  };

  // Generate post text for headlines
  const headlinesPostText = headlines.length > 0
    ? `🌤️ Weather Headlines - ${headlinesWfo}\n\n${headlines.map((h, i) => `${i + 1}. ${h.headline}`).join('\n')}\n\n#OHwx #weather #TheBattinFront`
    : '';

  const bskyCharCount = quickMessage.length;
  const tabs: { id: TabId; label: string; icon: string; count?: number }[] = [
    { id: 'headlines', label: 'Headlines', icon: 'fa-newspaper' },
    { id: 'reports', label: 'Storm Reports', icon: 'fa-bullhorn', count: stormReports.length },
    { id: 'alerts', label: 'Alerts', icon: 'fa-exclamation-triangle', count: alerts.length },
    { id: 'compose', label: 'Compose', icon: 'fa-pen' },
    { id: 'history', label: 'History', icon: 'fa-clock', count: postHistory.length },
  ];

  return (
    <div className="section active">
      <h2 className="section-title">
        <i className="fas fa-share-nodes" style={{ marginRight: 8 }}></i>
        Social Media
      </h2>

      {/* Platform Status */}
      <div className="social-status-panel">
        <div className="social-platform-status">
          <span className="platform-icon"><i className="fab fa-facebook"></i></span>
          <div className="platform-info">
            <div className="platform-name">Facebook</div>
            <div className="platform-detail">
              {status?.facebook.configured ? `Page: ${status.facebook.page_id}` : 'Not configured'}
            </div>
          </div>
          <span className={`status-dot ${status?.facebook.configured ? 'connected' : 'disconnected'}`}></span>
        </div>

        <div className="social-platform-status">
          <span className="platform-icon"><i className="fab fa-bluesky"></i></span>
          <div className="platform-info">
            <div className="platform-name">Bluesky</div>
            <div className="platform-detail">
              {status?.bluesky.configured ? `@${status.bluesky.handle}` : 'Not configured'}
            </div>
          </div>
          <span className={`status-dot ${status?.bluesky.configured ? 'connected' : 'disconnected'}`}></span>
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="social-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`social-tab ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            <i className={`fas ${tab.icon}`}></i>
            <span>{tab.label}</span>
            {tab.count !== undefined && tab.count > 0 && (
              <span className="social-tab-count">{tab.count}</span>
            )}
          </button>
        ))}
      </div>

      {/* ---- Headlines Tab ---- */}
      {activeTab === 'headlines' && (
        <div className="social-tab-content">
          {/* Office selector + fetch */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>WFO:</label>
              <select
                value={headlinesOffice}
                onChange={(e) => {
                  setHeadlinesOffice(e.target.value);
                  fetchHeadlines(e.target.value);
                }}
                style={{
                  padding: '3px 8px',
                  background: 'var(--bg-secondary)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  color: 'var(--text-primary)',
                  fontSize: '0.8rem',
                }}
              >
                <option value="ILN">Wilmington OH (ILN)</option>
                <option value="CLE">Cleveland OH (CLE)</option>
                <option value="IND">Indianapolis IN (IND)</option>
                <option value="IWX">Northern Indiana (IWX)</option>
                <option value="PBZ">Pittsburgh PA (PBZ)</option>
                <option value="RLX">Charleston WV (RLX)</option>
              </select>
              <button
                className="btn-share"
                onClick={() => fetchHeadlines()}
                disabled={headlinesLoading}
                style={{ fontSize: '0.72rem', padding: '3px 10px' }}
              >
                <i className={`fas ${headlinesLoading ? 'fa-spinner fa-spin' : 'fa-sync-alt'}`}></i> Refresh
              </button>
            </div>
            <button
              className="btn-share"
              onClick={handleShareHeadlines}
              disabled={headlines.length === 0 || !headlinesImageUrl}
            >
              <i className="fas fa-share-alt"></i> Share Graphic
            </button>
          </div>

          {headlinesLoading ? (
            <div className="social-empty-state">
              <i className="fas fa-spinner fa-spin"></i> Fetching forecast discussion...
            </div>
          ) : headlinesError ? (
            <div className="social-empty-state" style={{ color: 'var(--accent-red)' }}>
              {headlinesError}
            </div>
          ) : headlines.length === 0 ? (
            <div className="social-empty-state">No headlines available</div>
          ) : (
            <>
              {/* Editable headlines */}
              <div className="headlines-panel">
                <h3>
                  <i className="fas fa-newspaper"></i>
                  Weather Headlines &mdash; {headlinesWfo}
                  <span style={{ fontSize: '0.68rem', fontWeight: 400, color: 'var(--text-muted)', marginLeft: 8 }}>
                    Click to edit
                  </span>
                </h3>
                <div className="headlines-edit-list">
                  {headlines.map((h, i) => (
                    <div key={i} className="headline-edit-row">
                      <span className="hl-number">{i + 1}</span>
                      <input
                        type="text"
                        className="headline-edit-input"
                        value={h.headline}
                        onChange={(e) => {
                          const updated = [...headlines];
                          updated[i] = { ...updated[i], headline: e.target.value };
                          setHeadlines(updated);
                          setHeadlinesImageUrl(null); // Mark graphic as stale
                        }}
                      />
                      <button
                        className="headline-remove-btn"
                        title="Remove headline"
                        onClick={() => {
                          const updated = headlines.filter((_, idx) => idx !== i);
                          setHeadlines(updated);
                          setHeadlinesImageUrl(null);
                        }}
                      >
                        <i className="fas fa-times"></i>
                      </button>
                    </div>
                  ))}
                </div>
                {headlines.length < 4 && (
                  <button
                    className="btn-share"
                    style={{ marginTop: 8, fontSize: '0.72rem', padding: '3px 10px' }}
                    onClick={() => {
                      setHeadlines([...headlines, { headline: 'New headline', section: 'Custom', icon: 'fa-cloud-sun' }]);
                      setHeadlinesImageUrl(null);
                    }}
                  >
                    <i className="fas fa-plus"></i> Add Headline
                  </button>
                )}
              </div>

              {/* Graphic preview */}
              <div style={{ display: 'flex', gap: 8, marginTop: 12, marginBottom: 8 }}>
                <button className="btn-share" onClick={captureHeadlinesGraphic}>
                  <i className="fas fa-image"></i> {headlinesImageUrl ? 'Update Graphic' : 'Generate Graphic'}
                </button>
              </div>
              {headlinesImageUrl ? (
                <div className="compose-image-preview">
                  <img src={headlinesImageUrl} alt="Weather Headlines Graphic" />
                </div>
              ) : (
                <div className="compose-image-preview" style={{ padding: 20, color: 'var(--text-muted)' }}>
                  <i className="fas fa-info-circle" style={{ marginRight: 6 }}></i>
                  Edit headlines above, then click "Generate Graphic"
                </div>
              )}
            </>
          )}

          {/* Hidden graphic for capture */}
          {headlines.length > 0 && (
            <div style={{ position: 'absolute', left: '-9999px', top: 0 }}>
              <HeadlinesGraphic
                ref={headlinesGraphicRef}
                headlines={headlines}
                wfoName={headlinesWfo}
                receivedAt={headlinesReceivedAt}
                format="facebook"
              />
            </div>
          )}
        </div>
      )}

      {/* ---- Storm Reports Tab ---- */}
      {activeTab === 'reports' && (
        <div className="social-tab-content">
          {/* Time range + Summary share button */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <label style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>Time range:</label>
              <select
                value={reportTimeRange}
                onChange={(e) => setReportTimeRange(e.target.value)}
                style={{
                  padding: '3px 8px',
                  background: 'var(--bg-secondary)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  color: 'var(--text-primary)',
                  fontSize: '0.8rem',
                }}
              >
                <option value="6">Last 6 hours</option>
                <option value="12">Last 12 hours</option>
                <option value="24">Last 24 hours</option>
                <option value="48">Last 48 hours</option>
              </select>
            </div>
            <button
              className="btn-share"
              onClick={handleShareReportSummary}
              disabled={stormReports.length === 0}
            >
              <i className="fas fa-share-alt"></i> Share Full Summary
            </button>
          </div>

          {reportsLoading ? (
            <div className="social-empty-state">
              <i className="fas fa-spinner fa-spin"></i> Loading storm reports...
            </div>
          ) : stormReports.length === 0 ? (
            <div className="social-empty-state">No storm reports in the selected time range</div>
          ) : (
            <>
              {/* Report Type Summary Cards */}
              <div className="social-lsr-summary">
                {Object.entries(reportsByType)
                  .sort((a, b) => b[1].length - a[1].length)
                  .map(([type, reports]) => (
                    <div key={type} className="social-lsr-type-card">
                      <div className="lsr-type-header">
                        <span
                          className="lsr-type-dot"
                          style={{ backgroundColor: LSR_TYPE_COLORS[type] || '#888' }}
                        ></span>
                        <span className="lsr-type-name">{type}</span>
                        <span className="lsr-type-count">{reports.length}</span>
                      </div>
                      <button
                        className="btn-share"
                        style={{ fontSize: '0.7rem', padding: '2px 8px' }}
                        onClick={() => handleShareReportType(type)}
                      >
                        <i className="fas fa-share-alt"></i> Share
                      </button>
                    </div>
                  ))}
              </div>

              {/* Individual Reports */}
              <h4 style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', margin: '16px 0 8px' }}>
                Individual Reports ({stormReports.length})
              </h4>
              <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                {stormReports.slice(0, 50).map((report, i) => (
                  <div key={report.id || i} className="social-alert-item">
                    <div className="alert-info">
                      <div className="alert-event-name" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span
                          className="lsr-type-dot"
                          style={{ backgroundColor: LSR_TYPE_COLORS[report.report_type] || '#888' }}
                        ></span>
                        {report.report_type}
                        {report.magnitude && (
                          <span style={{ fontWeight: 400, color: 'var(--text-secondary)' }}>
                            {' '}- {report.magnitude}
                          </span>
                        )}
                      </div>
                      <div className="alert-locations">
                        Near {report.city}, {report.state} {report.county ? `(${report.county} Co.)` : ''}
                        {' '}{formatTime(report.valid_time)}
                      </div>
                    </div>
                    <button className="btn-share" onClick={() => handleShareSingleReport(report)}>
                      <i className="fas fa-share-alt"></i>
                    </button>
                  </div>
                ))}
                {stormReports.length > 50 && (
                  <div style={{ textAlign: 'center', padding: 8, color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                    Showing 50 of {stormReports.length} reports
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {/* ---- Alerts Tab ---- */}
      {activeTab === 'alerts' && (
        <div className="social-tab-content">
          <div className="social-alert-browser">
            {alerts.length === 0 ? (
              <div className="social-empty-state">No active alerts to share</div>
            ) : (
              alerts.slice(0, 20).map((alert) => (
                <div key={alert.product_id} className="social-alert-item">
                  <div className="alert-info">
                    <div className="alert-event-name">{alert.event_name}</div>
                    <div className="alert-locations">
                      {alert.display_locations || alert.affected_areas.join(', ')}
                    </div>
                  </div>
                  <button className="btn-share" onClick={() => handleShareAlert(alert)}>
                    <i className="fas fa-share-alt"></i> Share
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* ---- Compose Tab ---- */}
      {activeTab === 'compose' && (
        <div className="social-tab-content">
          <div className="social-quick-compose">
            <h3>Quick Compose</h3>
            <textarea
              value={quickMessage}
              onChange={(e) => setQuickMessage(e.target.value)}
              placeholder="Write a quick post..."
            />
            <div className="social-compose-footer">
              <span className={`social-char-count ${bskyCharCount > 300 ? 'over' : bskyCharCount > 250 ? 'warning' : ''}`}>
                {bskyCharCount} / 300
              </span>
              <button
                className="btn-share"
                onClick={handleQuickCompose}
              >
                <i className="fas fa-paper-plane"></i> Open Composer
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---- History Tab ---- */}
      {activeTab === 'history' && (
        <div className="social-tab-content">
          <div className="social-post-history">
            {postHistory.length === 0 ? (
              <div className="social-empty-state">No posts yet</div>
            ) : (
              postHistory.slice(0, 25).map((post) => (
                <div key={post.id} className="social-history-item">
                  <div className="history-platforms">
                    {post.platforms.includes('facebook') && <i className="fab fa-facebook" style={{ color: '#1877f2' }}></i>}
                    {post.platforms.includes('bluesky') && <i className="fab fa-bluesky" style={{ color: '#0085ff' }}></i>}
                  </div>
                  <span className="history-message">{post.message}</span>
                  {post.has_image && <i className="fas fa-image" style={{ color: 'var(--text-muted)', fontSize: '0.7rem' }}></i>}
                  <span className="history-time">{formatTimeAgo(post.timestamp)}</span>
                  <span className={`history-status ${post.success ? 'success' : 'error'}`}>
                    {post.success ? 'Posted' : 'Failed'}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* Compose Modal */}
      <ComposeModal
        isOpen={composeOpen}
        onClose={handleModalClose}
        alert={selectedAlert}
        stormReports={selectedReports.length > 0 ? selectedReports : undefined}
        initialMessage={
          composeMode === 'quick'
            ? quickMessage
            : composeMode === 'headlines'
            ? headlinesPostText
            : undefined
        }
        preGeneratedImage={composeMode === 'headlines' ? headlinesImageUrl : undefined}
        imageLabel={composeMode === 'headlines' ? 'Weather Headlines Graphic' : undefined}
      />
    </div>
  );
};

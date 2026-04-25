import React, { useEffect, useState, useCallback } from 'react';
import { apiUrl } from '../utils/api';
import { HeadlineGraphicModal } from './HeadlineGraphicModal';

interface GraphicMeta {
  product_id: string;
  event_name: string;
  url: string;
  created_at: string;
}

export const AlertGraphicsSection: React.FC = () => {
  const [graphics, setGraphics] = useState<GraphicMeta[]>([]);
  const [selected, setSelected] = useState<GraphicMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [showHeadlineModal, setShowHeadlineModal] = useState(false);

  const fetchGraphics = useCallback(() => {
    fetch(apiUrl('/api/alert-graphics'))
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.graphics) setGraphics(data.graphics);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchGraphics();
    const id = setInterval(fetchGraphics, 30_000);
    return () => clearInterval(id);
  }, [fetchGraphics]);

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true,
    });
  };

  const handleDelete = async (productId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await fetch(apiUrl(`/api/alert-graphics/${productId}`), { method: 'DELETE' });
      setGraphics(prev => prev.filter(g => g.product_id !== productId));
      if (selected?.product_id === productId) setSelected(null);
    } catch {
      // ignore
    }
  };

  return (
    <div className="section active" style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 className="section-title" style={{ margin: 0 }}>Alert Graphics</h2>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => setShowHeadlineModal(true)}
            style={{
              border: '1px solid var(--primary-color)',
              backgroundColor: 'rgba(59,130,246,0.12)',
              color: 'var(--primary-color)',
              borderRadius: 'var(--radius-sm)',
              padding: '5px 12px',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            <i className="fas fa-tv" style={{ marginRight: 5 }}></i>
            Headline Graphic
          </button>
          <button
            onClick={fetchGraphics}
            style={{
              background: 'none',
              border: '1px solid var(--border-color)',
              color: 'var(--text-secondary)',
              borderRadius: 'var(--radius-sm)',
              padding: '4px 10px',
              cursor: 'pointer',
              fontSize: 12,
            }}
          >
            <i className="fas fa-sync-alt" style={{ marginRight: 5 }}></i>
            Refresh
          </button>
        </div>
      </div>

      {showHeadlineModal && (
        <HeadlineGraphicModal
          onClose={() => { setShowHeadlineModal(false); fetchGraphics(); }}
        />
      )}

      {loading && (
        <p style={{ color: 'var(--text-muted)', fontSize: 14 }}>Loading...</p>
      )}

      {!loading && graphics.length === 0 && (
        <div style={{
          flex: 1, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 12,
          color: 'var(--text-muted)',
        }}>
          <i className="fas fa-images" style={{ fontSize: 40, opacity: 0.3 }}></i>
          <p style={{ fontSize: 14, textAlign: 'center' }}>
            No graphics yet.<br />
            Graphics are generated automatically when new alerts arrive.
          </p>
        </div>
      )}

      {graphics.length > 0 && (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
          gap: 12,
          overflowY: 'auto',
          flex: 1,
          paddingRight: 4,
        }}>
          {graphics.map(g => (
            <div
              key={g.product_id}
              onClick={() => setSelected(g)}
              style={{
                position: 'relative',
                cursor: 'pointer',
                borderRadius: 'var(--radius-md)',
                overflow: 'hidden',
                border: '1px solid var(--border-color)',
                backgroundColor: 'var(--bg-card)',
                transition: 'border-color var(--transition-fast)',
              }}
              onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--accent-blue)')}
              onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--border-color)')}
            >
              <img
                src={apiUrl(g.url)}
                alt={g.event_name || g.product_id}
                style={{ width: '100%', display: 'block' }}
                loading="lazy"
              />
              <div style={{
                padding: '8px 10px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 8,
              }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{
                    fontSize: 12, fontWeight: 600,
                    color: 'var(--text-primary)',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {g.event_name || g.product_id}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    {formatDate(g.created_at)}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  <a
                    href={apiUrl(g.url)}
                    download={`${g.product_id}.png`}
                    onClick={e => e.stopPropagation()}
                    style={{
                      padding: '3px 8px',
                      borderRadius: 'var(--radius-sm)',
                      backgroundColor: 'var(--bg-secondary)',
                      border: '1px solid var(--border-color)',
                      color: 'var(--text-secondary)',
                      fontSize: 11,
                      textDecoration: 'none',
                      cursor: 'pointer',
                    }}
                    title="Download"
                  >
                    <i className="fas fa-download"></i>
                  </a>
                  <button
                    onClick={e => handleDelete(g.product_id, e)}
                    style={{
                      padding: '3px 8px',
                      borderRadius: 'var(--radius-sm)',
                      backgroundColor: 'var(--bg-secondary)',
                      border: '1px solid var(--border-color)',
                      color: 'var(--accent-red)',
                      fontSize: 11,
                      cursor: 'pointer',
                    }}
                    title="Delete"
                  >
                    <i className="fas fa-trash"></i>
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Lightbox */}
      {selected && (
        <div
          onClick={() => setSelected(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            backgroundColor: 'rgba(0,0,0,0.88)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 24,
          }}
        >
          <div onClick={e => e.stopPropagation()} style={{ position: 'relative', maxWidth: '95vw' }}>
            <img
              src={apiUrl(selected.url)}
              alt={selected.event_name || selected.product_id}
              style={{ maxWidth: '100%', borderRadius: 8, display: 'block' }}
            />
            <div style={{
              position: 'absolute', top: -36, right: 0,
              display: 'flex', gap: 8,
            }}>
              <a
                href={apiUrl(selected.url)}
                download={`${selected.product_id}.png`}
                style={{
                  padding: '4px 12px', borderRadius: 4,
                  backgroundColor: 'rgba(255,255,255,0.1)',
                  color: '#fff', fontSize: 12, textDecoration: 'none',
                }}
              >
                <i className="fas fa-download" style={{ marginRight: 5 }}></i>
                Download
              </a>
              <button
                onClick={() => setSelected(null)}
                style={{
                  padding: '4px 12px', borderRadius: 4,
                  backgroundColor: 'rgba(255,255,255,0.1)',
                  border: 'none', color: '#fff', fontSize: 12, cursor: 'pointer',
                }}
              >
                <i className="fas fa-times" style={{ marginRight: 5 }}></i>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

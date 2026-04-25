import React, { useState, useEffect } from 'react';
import { apiUrl } from '../utils/api';

interface EventTypeOption {
  id: string;
  label: string;
  accent: string;
}

interface HeadlineGraphicModalProps {
  onClose: () => void;
  /** Pre-fill from an active alert */
  prefill?: {
    event_type?: string;
    headline?: string;
    subtitle?: string;
    issued_by?: string;
    expires?: string;
    is_emergency?: boolean;
  };
}

export const HeadlineGraphicModal: React.FC<HeadlineGraphicModalProps> = ({ onClose, prefill }) => {
  const [eventTypes, setEventTypes] = useState<EventTypeOption[]>([]);
  const [eventType, setEventType] = useState(prefill?.event_type || 'DEFAULT');
  const [headline, setHeadline] = useState(prefill?.headline || '');
  const [subtitle, setSubtitle] = useState(prefill?.subtitle || '');
  const [body, setBody] = useState('');
  const [issuedBy, setIssuedBy] = useState(prefill?.issued_by || '');
  const [expires, setExpires] = useState(prefill?.expires || '');
  const [isEmergency, setIsEmergency] = useState(prefill?.is_emergency || false);
  const [generating, setGenerating] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(apiUrl('/api/graphics/headline/event-types'))
      .then(r => r.json())
      .then(d => setEventTypes(d.event_types || []))
      .catch(() => {});
  }, []);

  // Auto-set headline from event type label when user picks a type
  const handleEventTypeChange = (id: string) => {
    setEventType(id);
    const et = eventTypes.find(e => e.id === id);
    if (et && !headline) setHeadline(et.label);
    if (id === 'EMERGENCY') setIsEmergency(true);
  };

  const handleGenerate = async (save = false) => {
    if (!headline.trim()) { setError('Headline is required.'); return; }
    setError(null);
    setGenerating(true);

    // Revoke old preview
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      setPreviewUrl(null);
    }

    try {
      const res = await fetch(apiUrl('/api/graphics/headline'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_type: eventType,
          headline,
          subtitle,
          body,
          issued_by: issuedBy,
          expires,
          is_emergency: isEmergency,
          save,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      setPreviewUrl(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generation failed');
    } finally {
      setGenerating(false);
    }
  };

  const selectedType = eventTypes.find(e => e.id === eventType);

  return (
    <div
      style={{ position: 'fixed', inset: 0, backgroundColor: 'rgba(0,0,0,0.85)', zIndex: 10000, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px' }}
      onClick={onClose}
    >
      <div
        style={{ width: '100%', maxWidth: '960px', maxHeight: '92vh', overflowY: 'auto', backgroundColor: 'var(--bg-primary)', borderRadius: '10px', boxShadow: '0 8px 40px rgba(0,0,0,0.7)', display: 'flex', flexDirection: 'column' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
          <h2 style={{ margin: 0, fontSize: '1rem', color: 'var(--text-primary)' }}>
            <i className="fas fa-tv" style={{ marginRight: '8px', color: 'var(--primary-color)' }}></i>
            Headline Graphic
          </h2>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-secondary)', fontSize: '1.1rem' }}>
            <i className="fas fa-times"></i>
          </button>
        </div>

        <div style={{ display: 'flex', gap: '0', flex: 1, minHeight: 0 }}>
          {/* Form panel */}
          <div style={{ flex: '0 0 340px', padding: '16px 20px', borderRight: '1px solid var(--border-color)', overflowY: 'auto' }}>

            {error && (
              <div style={{ padding: '8px 10px', backgroundColor: 'rgba(255,60,60,0.12)', borderRadius: '4px', color: '#ff6060', fontSize: '0.82rem', marginBottom: '12px' }}>
                <i className="fas fa-exclamation-circle"></i> {error}
              </div>
            )}

            {/* Event type */}
            <label style={{ display: 'block', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>Event Type</div>
              <select
                value={eventType}
                onChange={e => handleEventTypeChange(e.target.value)}
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: `2px solid ${selectedType?.accent || 'var(--border-color)'}`, backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.85rem' }}
              >
                {eventTypes.map(et => (
                  <option key={et.id} value={et.id}>{et.label}</option>
                ))}
              </select>
            </label>

            {/* Emergency toggle */}
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px', cursor: 'pointer' }}>
              <input type="checkbox" checked={isEmergency} onChange={e => setIsEmergency(e.target.checked)} />
              <span style={{ fontSize: '0.85rem', color: '#ff4444', fontWeight: isEmergency ? 700 : 400 }}>
                Particularly Dangerous Situation / Emergency
              </span>
            </label>

            {/* Headline */}
            <label style={{ display: 'block', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>
                Headline <span style={{ color: '#ff6060' }}>*</span>
              </div>
              <input
                type="text"
                value={headline}
                onChange={e => setHeadline(e.target.value)}
                placeholder="e.g. Tornado Warning"
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.9rem', boxSizing: 'border-box' }}
              />
            </label>

            {/* Subtitle */}
            <label style={{ display: 'block', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>Subtitle / Location</div>
              <input
                type="text"
                value={subtitle}
                onChange={e => setSubtitle(e.target.value)}
                placeholder="e.g. Warren, Clinton, Montgomery Counties"
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.85rem', boxSizing: 'border-box' }}
              />
            </label>

            {/* Body */}
            <label style={{ display: 'block', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>Detail / Body Text</div>
              <textarea
                value={body}
                onChange={e => setBody(e.target.value)}
                placeholder="Additional detail text shown below the subtitle..."
                rows={3}
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.82rem', resize: 'vertical', boxSizing: 'border-box' }}
              />
            </label>

            {/* Issued by */}
            <label style={{ display: 'block', marginBottom: '12px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>Issued By</div>
              <input
                type="text"
                value={issuedBy}
                onChange={e => setIssuedBy(e.target.value)}
                placeholder="NWS Indianapolis"
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.82rem', boxSizing: 'border-box' }}
              />
            </label>

            {/* Expires */}
            <label style={{ display: 'block', marginBottom: '16px' }}>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: '4px', fontWeight: 600, textTransform: 'uppercase' }}>Expires</div>
              <input
                type="text"
                value={expires}
                onChange={e => setExpires(e.target.value)}
                placeholder="7:45 PM EDT"
                style={{ width: '100%', padding: '7px 8px', borderRadius: '4px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', fontSize: '0.82rem', boxSizing: 'border-box' }}
              />
            </label>

            {/* Action buttons */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <button
                onClick={() => handleGenerate(false)}
                disabled={generating}
                style={{
                  padding: '10px', borderRadius: '6px', border: 'none', cursor: generating ? 'not-allowed' : 'pointer',
                  backgroundColor: selectedType?.accent || 'var(--primary-color)', color: 'white',
                  fontWeight: 700, fontSize: '0.9rem', opacity: generating ? 0.7 : 1,
                }}
              >
                <i className={`fas ${generating ? 'fa-spinner fa-spin' : 'fa-eye'}`} style={{ marginRight: '6px' }}></i>
                {generating ? 'Generating...' : 'Preview Graphic'}
              </button>
              <div style={{ display: 'flex', gap: '6px' }}>
                {previewUrl && (
                  <a
                    href={previewUrl}
                    download="headline.png"
                    style={{ flex: 1, padding: '8px', borderRadius: '6px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', textDecoration: 'none', textAlign: 'center', fontSize: '0.82rem' }}
                  >
                    <i className="fas fa-download" style={{ marginRight: '4px' }}></i>Download
                  </a>
                )}
                {previewUrl && (
                  <button
                    onClick={() => handleGenerate(true)}
                    style={{ flex: 1, padding: '8px', borderRadius: '6px', border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)', cursor: 'pointer', fontSize: '0.82rem' }}
                  >
                    <i className="fas fa-save" style={{ marginRight: '4px' }}></i>Save to Gallery
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Preview panel */}
          <div style={{ flex: 1, padding: '16px', display: 'flex', alignItems: 'center', justifyContent: 'center', backgroundColor: '#0a0a0a', borderRadius: '0 10px 10px 0', minHeight: '300px' }}>
            {previewUrl ? (
              <img
                src={previewUrl}
                alt="Headline graphic preview"
                style={{ maxWidth: '100%', maxHeight: '100%', borderRadius: '4px', boxShadow: '0 4px 24px rgba(0,0,0,0.6)' }}
              />
            ) : (
              <div style={{ textAlign: 'center', color: '#333' }}>
                <i className="fas fa-image" style={{ fontSize: '3rem', display: 'block', marginBottom: '12px' }}></i>
                <span style={{ fontSize: '0.85rem' }}>Preview will appear here</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

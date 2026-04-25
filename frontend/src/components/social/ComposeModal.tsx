import React, { useState, useEffect, useRef, useCallback } from 'react';
import type { Alert } from '../../types/alert';
import type { StormReport } from '../../types/lsr';
import type { PostResult, SocialStatus } from '../../types/social';
import { AlertGraphic, type AlertGraphicHandle } from './AlertGraphic';
import { LSRGraphic, type LSRGraphicHandle } from './LSRGraphic';
import { apiUrl } from '../../utils/api';

interface ComposeModalProps {
  isOpen: boolean;
  onClose: () => void;
  alert?: Alert | null;
  stormReports?: StormReport[];
  initialMessage?: string;
  preGeneratedImage?: string | null;
  imageLabel?: string;
}

export const ComposeModal: React.FC<ComposeModalProps> = ({
  isOpen,
  onClose,
  alert,
  stormReports,
  initialMessage,
  preGeneratedImage,
  imageLabel: imageOverrideLabel,
}) => {
  const [message, setMessage] = useState('');
  const [platforms, setPlatforms] = useState<Set<string>>(new Set(['facebook', 'bluesky']));
  const [template, setTemplate] = useState('default');
  const [lsrTemplate, setLsrTemplate] = useState('summary');
  const [imageDataUrl, setImageDataUrl] = useState<string | null>(null);
  const [includeImage, setIncludeImage] = useState(true);
  const [posting, setPosting] = useState(false);
  const [result, setResult] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [status, setStatus] = useState<SocialStatus | null>(null);

  const alertGraphicRef = useRef<AlertGraphicHandle>(null);
  const lsrGraphicRef = useRef<LSRGraphicHandle>(null);

  const hasAlert = !!alert;
  const hasReports = !!(stormReports && stormReports.length > 0);
  const isSingleReport = hasReports && stormReports!.length === 1;

  // Fetch social status on mount
  useEffect(() => {
    fetch(apiUrl('/api/social/status'))
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => {});
  }, []);

  // Reset state when modal opens with new content
  useEffect(() => {
    if (isOpen) {
      setResult(null);
      setImageDataUrl(preGeneratedImage || null);
      setIncludeImage(true);
    }
  }, [isOpen, alert, stormReports, preGeneratedImage]);

  // Generate text when alert or template changes
  useEffect(() => {
    if (!isOpen) return;

    if (initialMessage !== undefined) {
      setMessage(initialMessage);
      return;
    }

    if (hasAlert) {
      fetch(apiUrl('/api/social/generate-text'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'alert',
          source_id: alert!.product_id,
          template,
        }),
      })
        .then((r) => r.json())
        .then((data) => setMessage(data.text || ''))
        .catch(() => {});
    } else if (hasReports) {
      const sourceData = isSingleReport
        ? stormReports![0]
        : { reports: stormReports };
      fetch(apiUrl('/api/social/generate-text'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_type: 'lsr',
          source_data: sourceData,
          template: isSingleReport ? 'single' : lsrTemplate,
        }),
      })
        .then((r) => r.json())
        .then((data) => setMessage(data.text || ''))
        .catch(() => {});
    }
  }, [isOpen, alert, stormReports, template, lsrTemplate, initialMessage, hasAlert, hasReports, isSingleReport]);

  // Capture graphic
  const captureGraphic = useCallback(async () => {
    if (hasAlert && alertGraphicRef.current) {
      const dataUrl = await alertGraphicRef.current.capture();
      setImageDataUrl(dataUrl);
    } else if (hasReports && lsrGraphicRef.current) {
      const dataUrl = await lsrGraphicRef.current.capture();
      setImageDataUrl(dataUrl);
    }
  }, [hasAlert, hasReports]);

  useEffect(() => {
    if ((hasAlert || hasReports) && isOpen) {
      const timer = setTimeout(captureGraphic, 400);
      return () => clearTimeout(timer);
    }
  }, [hasAlert, hasReports, isOpen, captureGraphic]);

  const togglePlatform = (p: string) => {
    setPlatforms((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  };

  const handlePost = async () => {
    if (!message.trim() || platforms.size === 0) return;

    setPosting(true);
    setResult(null);

    try {
      const altText = hasAlert
        ? `${alert!.event_name} - ${alert!.display_locations}`
        : hasReports
        ? `Storm Reports Summary - ${stormReports!.length} reports`
        : 'Weather graphic from The Battin Front';

      const body: Record<string, unknown> = {
        platforms: Array.from(platforms),
        message: message.trim(),
        images: includeImage && imageDataUrl ? [imageDataUrl] : [],
        alt_text: altText,
      };

      const resp = await fetch(apiUrl('/api/social/post'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      const data: PostResult = await resp.json();

      const successes: string[] = [];
      const errors: string[] = [];

      if (data.facebook) {
        if (data.facebook.success) successes.push('Facebook');
        else errors.push(`Facebook: ${data.facebook.error}`);
      }
      if (data.bluesky) {
        if (data.bluesky.success) successes.push('Bluesky');
        else errors.push(`Bluesky: ${data.bluesky.error}`);
      }

      if (successes.length > 0 && errors.length === 0) {
        setResult({ type: 'success', text: `Posted to ${successes.join(' & ')}` });
      } else if (errors.length > 0 && successes.length > 0) {
        setResult({ type: 'error', text: `Posted to ${successes.join(', ')} but failed: ${errors.join('; ')}` });
      } else {
        setResult({ type: 'error', text: errors.join('; ') });
      }
    } catch (err) {
      setResult({ type: 'error', text: `Network error: ${err}` });
    } finally {
      setPosting(false);
    }
  };

  if (!isOpen) return null;

  const bskyCharCount = message.length;
  const bskyWarning = platforms.has('bluesky') && bskyCharCount > 300;

  const graphicLabel = imageOverrideLabel
    ? imageOverrideLabel
    : hasAlert
    ? 'Alert Graphic'
    : hasReports
    ? isSingleReport
      ? 'Storm Report Graphic'
      : 'Storm Reports Summary Graphic'
    : preGeneratedImage
    ? 'Graphic'
    : null;

  return (
    <div className="compose-modal-overlay" onClick={onClose}>
      <div className="compose-modal" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="compose-modal-header">
          <h2>
            <i className="fas fa-share-alt" style={{ marginRight: 8 }}></i>
            {hasAlert
              ? 'Share Alert'
              : hasReports
              ? isSingleReport
                ? 'Share Storm Report'
                : `Share Storm Reports (${stormReports!.length})`
              : 'Share to Social Media'}
          </h2>
          <button className="compose-modal-close" onClick={onClose}>
            <i className="fas fa-times"></i>
          </button>
        </div>

        <div className="compose-modal-body">
          {/* Platform toggles */}
          <div className="compose-platforms">
            <button
              className={`platform-toggle ${platforms.has('facebook') ? 'active' : ''}`}
              onClick={() => togglePlatform('facebook')}
              disabled={!status?.facebook.configured}
            >
              <i className="fab fa-facebook"></i> Facebook
            </button>
            <button
              className={`platform-toggle ${platforms.has('bluesky') ? 'active' : ''}`}
              onClick={() => togglePlatform('bluesky')}
              disabled={!status?.bluesky.configured}
            >
              <i className="fab fa-bluesky"></i> Bluesky
            </button>
          </div>

          {/* Template selector */}
          {hasAlert && (
            <div className="compose-template-row">
              <label>Template:</label>
              <select value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="default">Default</option>
                <option value="breaking">Breaking</option>
                <option value="minimal">Minimal</option>
                <option value="tornado_emergency">Tornado Emergency</option>
              </select>
            </div>
          )}
          {hasReports && !isSingleReport && (
            <div className="compose-template-row">
              <label>Template:</label>
              <select value={lsrTemplate} onChange={(e) => setLsrTemplate(e.target.value)}>
                <option value="summary">Summary</option>
                <option value="single">First Report Only</option>
              </select>
            </div>
          )}

          {/* Message textarea */}
          <textarea
            className="compose-textarea"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Write your post..."
          />

          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
            <span
              className={`social-char-count ${bskyWarning ? (bskyCharCount > 300 ? 'over' : 'warning') : ''}`}
            >
              {bskyCharCount} characters
              {bskyWarning && ' (Bluesky max: 300)'}
            </span>
          </div>

          {/* Image section - Alert or LSR graphic */}
          {graphicLabel && (
            <div className="compose-image-section">
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <h4 style={{ margin: 0 }}>{graphicLabel}</h4>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={includeImage}
                    onChange={(e) => setIncludeImage(e.target.checked)}
                    style={{ marginRight: 4 }}
                  />
                  Include image
                </label>
              </div>

              {imageDataUrl ? (
                <div className="compose-image-preview">
                  <img src={imageDataUrl} alt={graphicLabel} />
                  <div className="compose-graphic-actions">
                    <button className="btn-share" onClick={captureGraphic}>
                      <i className="fas fa-sync-alt"></i> Regenerate
                    </button>
                  </div>
                </div>
              ) : (
                <div className="compose-image-preview" style={{ padding: 20, color: 'var(--text-muted)' }}>
                  <i className="fas fa-spinner fa-spin" style={{ marginRight: 6 }}></i>
                  Generating graphic...
                </div>
              )}

              {/* Hidden graphics for capture */}
              <div style={{ position: 'absolute', left: '-9999px', top: 0 }}>
                {hasAlert && (
                  <AlertGraphic ref={alertGraphicRef} alert={alert!} format="facebook" />
                )}
                {hasReports && (
                  <LSRGraphic ref={lsrGraphicRef} reports={stormReports!} format="facebook" />
                )}
              </div>
            </div>
          )}

          {/* Result message */}
          {result && (
            <div className={`compose-result ${result.type}`}>
              <i className={`fas ${result.type === 'success' ? 'fa-check-circle' : 'fa-exclamation-circle'}`} style={{ marginRight: 6 }}></i>
              {result.text}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="compose-modal-footer">
          <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            {status?.facebook.configured && status?.bluesky.configured
              ? 'Both platforms configured'
              : status?.facebook.configured
              ? 'Facebook configured'
              : status?.bluesky.configured
              ? 'Bluesky configured'
              : 'No platforms configured'}
          </span>
          <button
            className={`compose-btn-post ${posting ? 'posting' : ''}`}
            onClick={handlePost}
            disabled={posting || !message.trim() || platforms.size === 0}
          >
            {posting ? (
              <>
                <i className="fas fa-spinner fa-spin"></i> Posting...
              </>
            ) : (
              <>
                <i className="fas fa-paper-plane"></i> Post
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};

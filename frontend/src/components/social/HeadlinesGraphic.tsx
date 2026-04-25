import { useRef, useImperativeHandle, forwardRef } from 'react';

export interface Headline {
  headline: string;
  section: string;
  icon: string;
}

export interface HeadlinesGraphicHandle {
  capture: () => Promise<string | null>;
}

interface HeadlinesGraphicProps {
  headlines: Headline[];
  wfoName: string;
  receivedAt: string;
  format?: 'facebook' | 'bluesky';
}

export const HeadlinesGraphic = forwardRef<HeadlinesGraphicHandle, HeadlinesGraphicProps>(
  ({ headlines, wfoName, receivedAt, format = 'facebook' }, ref) => {
    const containerRef = useRef<HTMLDivElement>(null);
    const height = format === 'facebook' ? 630 : 675;

    // Calculate font size based on total content to prevent overflow
    const totalChars = headlines.reduce((sum, h) => sum + h.headline.length, 0);
    const headlineCount = headlines.length;
    // Scale down text if there's a lot of content
    let fontSize = 22;
    if (totalChars > 300 || headlineCount > 3) fontSize = 19;
    if (totalChars > 400) fontSize = 17;
    let rowPadding = 16;
    if (headlineCount > 3) rowPadding = 12;

    const formatDate = (iso: string) => {
      try {
        const d = new Date(iso);
        return d.toLocaleDateString('en-US', {
          weekday: 'long',
          month: 'long',
          day: 'numeric',
          year: 'numeric',
        });
      } catch {
        return '';
      }
    };

    const formatTime = (iso: string) => {
      try {
        const d = new Date(iso);
        return d.toLocaleTimeString('en-US', {
          hour: 'numeric',
          minute: '2-digit',
          hour12: true,
        });
      } catch {
        return '';
      }
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
          console.error('Failed to capture headlines graphic:', err);
          return null;
        }
      },
    }));

    return (
      <div ref={containerRef} className="headlines-graphic" style={{ height: `${height}px` }}>
        {/* Header */}
        <div className="headlines-graphic-header">
          <div className="headlines-brand">THE BATTIN FRONT</div>
          <div className="headlines-title">Weather Headlines</div>
          <div className="headlines-source">
            {wfoName} &mdash; {formatDate(receivedAt)}
          </div>
        </div>

        {/* Headlines */}
        <div className="headlines-graphic-body" style={{ gap: `${rowPadding}px` }}>
          {headlines.map((h, i) => (
            <div key={i} className="headline-row" style={{ padding: `${rowPadding}px 20px` }}>
              <div className="headline-number">{i + 1}</div>
              <div className="headline-content">
                <div className="headline-text" style={{ fontSize: `${fontSize}px` }}>
                  {h.headline}
                </div>
              </div>
              <div className="headline-icon">
                <i className={`fas ${h.icon}`}></i>
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="headlines-graphic-footer">
          <span>thebattinfront.com</span>
          <span>Updated {formatTime(receivedAt)}</span>
          <span>Data: National Weather Service</span>
        </div>
      </div>
    );
  }
);

HeadlinesGraphic.displayName = 'HeadlinesGraphic';

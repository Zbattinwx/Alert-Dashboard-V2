import { useCallback, useEffect, useRef, useState } from 'react';
import { apiUrl } from '../utils/api';

/**
 * Top-of-app banner for the standalone-server self-updater.
 *
 * Polls GET /api/update/status; when a newer packaged build is published it
 * shows "Update available" with an "Update now" button. Clicking it POSTs
 * /api/update/apply (which downloads + verifies + swaps + restarts the server),
 * then waits for the server to come back on the new build and reloads.
 *
 * Renders nothing unless an update is available or one is being applied, so it
 * is invisible in normal operation and on local dev (no version.json).
 */

interface UpdateStatus {
  enabled: boolean;
  frozen: boolean;
  current: string | null;
  current_display: string;
  latest: string | null;
  update_available: boolean;
  notes: string | null;
  pub_date: string | null;
  applying: boolean;
}

type Phase = 'idle' | 'working' | 'restarting' | 'error';

const POLL_MS = 5 * 60 * 1000;

export function UpdateBanner() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState('');
  const restartTimer = useRef<number | null>(null);

  const check = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/update/status'), { cache: 'no-store' });
      if (res.ok) setStatus(await res.json());
    } catch {
      /* transient — keep last known status */
    }
  }, []);

  useEffect(() => {
    check();
    const id = window.setInterval(check, POLL_MS);
    return () => {
      window.clearInterval(id);
      if (restartTimer.current) window.clearTimeout(restartTimer.current);
    };
  }, [check]);

  // Poll until the server returns on a build different from the one we started
  // from, then reload to pick up the new frontend.
  const waitForRestart = useCallback((fromBuild: string | null) => {
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const res = await fetch(apiUrl('/api/update/status'), { cache: 'no-store' });
        if (res.ok) {
          const s: UpdateStatus = await res.json();
          if (s.current && s.current !== fromBuild) {
            window.location.reload();
            return;
          }
        }
      } catch {
        /* still restarting */
      }
      if (tries < 150) {
        restartTimer.current = window.setTimeout(tick, 2000); // up to ~5 min
      } else {
        setPhase('error');
        setMessage('The server is taking longer than expected to come back. Refresh the page to check.');
      }
    };
    restartTimer.current = window.setTimeout(tick, 4000);
  }, []);

  const apply = useCallback(async () => {
    const fromBuild = status?.current ?? null;
    setPhase('working');
    setMessage('Downloading and verifying the update…');
    try {
      const res = await fetch(apiUrl('/api/update/apply'), { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.started) {
        setPhase('error');
        setMessage(data.detail || data.message || 'Could not start the update.');
        return;
      }
      setPhase('restarting');
      setMessage(data.message || 'Updating — the dashboard will restart in a moment.');
      waitForRestart(fromBuild);
    } catch {
      setPhase('error');
      setMessage('Could not reach the server to start the update.');
    }
  }, [status, waitForRestart]);

  const available = !!status && status.update_available;
  if (phase === 'idle' && !available) return null;

  const busy = phase === 'working' || phase === 'restarting';
  const accent = phase === 'error' ? '#e5634a' : '#2e9be6';

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        padding: '10px 18px',
        background: 'linear-gradient(90deg, rgba(46,155,230,0.16), rgba(46,155,230,0.05))',
        borderBottom: `1px solid ${accent}55`,
        borderLeft: `4px solid ${accent}`,
        color: '#e9eef5',
        font: '500 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
      }}
    >
      <style>{`@keyframes tbfUpdSpin{to{transform:rotate(360deg)}}`}</style>

      {busy ? (
        <span
          aria-hidden="true"
          style={{
            width: 16, height: 16, flex: '0 0 auto', borderRadius: '50%',
            border: '2px solid rgba(255,255,255,0.25)', borderTopColor: accent,
            animation: 'tbfUpdSpin 0.8s linear infinite',
          }}
        />
      ) : (
        <span aria-hidden="true" style={{ fontSize: 16, flex: '0 0 auto' }}>{phase === 'error' ? '⚠️' : '⬆️'}</span>
      )}

      <div style={{ flex: '1 1 auto', minWidth: 0 }}>
        {phase === 'idle' && available && (
          <>
            <b style={{ fontWeight: 700 }}>Dashboard update available</b>
            {status?.latest && (
              <span style={{ opacity: 0.75, marginLeft: 8, fontVariantNumeric: 'tabular-nums' }}>
                build {status.latest}
              </span>
            )}
            {status?.notes && (
              <div style={{ opacity: 0.8, fontSize: 13, marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {status.notes}
              </div>
            )}
          </>
        )}
        {phase !== 'idle' && <span>{message}</span>}
      </div>

      {phase === 'idle' && available && (
        <button
          type="button"
          onClick={apply}
          style={{
            flex: '0 0 auto', cursor: 'pointer',
            background: accent, color: '#04121f', border: 'none',
            padding: '7px 16px', borderRadius: 7, fontWeight: 700, fontSize: 13.5,
          }}
        >
          Update now
        </button>
      )}
      {phase === 'error' && (
        <button
          type="button"
          onClick={() => { setPhase('idle'); setMessage(''); check(); }}
          style={{
            flex: '0 0 auto', cursor: 'pointer', background: 'transparent',
            color: '#e9eef5', border: '1px solid rgba(255,255,255,0.35)',
            padding: '6px 14px', borderRadius: 7, fontWeight: 600, fontSize: 13,
          }}
        >
          Dismiss
        </button>
      )}
    </div>
  );
}

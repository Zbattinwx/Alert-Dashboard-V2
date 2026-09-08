/**
 * TBF Escalation Index — system health.
 *
 * This panel exists because of how the classifier failed. It did not crash: it
 * loaded nothing and logged "running pure physics", which reads exactly like
 * "no model has been trained yet". Every packaged build shipped that way for
 * months while the UI carried a probability field that was always empty. A
 * second failure lived alongside it — a feature vector the model could not
 * accept raised on roughly one cell in six, and the error was swallowed without
 * a log line.
 *
 * Neither was visible anywhere a person looks. So the panel's job is not to
 * present metrics attractively; it is to make those two specific states
 * impossible to miss, and to distinguish them from each other and from the
 * legitimate "nothing has happened yet".
 *
 * Three rules follow from that:
 *
 *  1. NOT LOADED is loud, and says packaging rather than "no data". A model
 *     that fails to load is a build problem, not a quiet Tuesday.
 *  2. "Never scored" is shown as its own number, never folded into a
 *     probability of zero. A rising unscored count IS the signature of the
 *     original bug and is the single most diagnostic figure here.
 *  3. Precision is shown against climatology. 14% precision sounds broken and
 *     is roughly 40x better than chance at this base rate; 14% precision at a
 *     14% base rate is worthless. The bare percentage cannot tell them apart.
 */
import React, { useCallback, useEffect, useState } from 'react';

interface ModelSlot {
  found: boolean;
  path: string | null;
  source: 'runtime' | 'bundled' | null;
}
interface Degraded {
  total: number;
  detectors: { key: string; count: number; what: string }[];
  error?: string;
}
interface PathsResponse {
  frozen: boolean;
  runtime_dir: string;
  bundled_dir: string | null;
  models: Record<string, ModelSlot>;
  tracker?: {
    running: boolean;
    rotation_loaded?: boolean;
    severe_loaded?: boolean;
    features?: number;
    note?: string;
    error?: string;
  };
  degraded?: Degraded;
}
interface TargetScore {
  n: number;
  positives?: number;
  base_rate?: number | null;
  threshold?: number;
  tp?: number; fp?: number; fn?: number; tn?: number;
  precision?: number | null;
  recall?: number | null;
  lift?: number | null;
  note?: string;
}
interface Scorecard {
  window_days: number;
  generated_at: string;
  rows_in_window?: number;
  awaiting_labels?: number;
  unscored?: number;
  targets: Record<string, TargetScore>;
  verdict?: string;
  error?: string;
}

const pct = (v?: number | null, d = 1) =>
  v === undefined || v === null || !isFinite(v) ? '—' : `${(v * 100).toFixed(d)}%`;
const num = (v?: number | null) =>
  v === undefined || v === null ? '—' : v.toLocaleString();

/** Human label for a target key. */
const TARGET_LABEL: Record<string, string> = {
  severe: 'Severe',
  rotation: 'Tornado',
};

export const SystemHealthPanel: React.FC = () => {
  const [paths, setPaths] = useState<PathsResponse | null>(null);
  const [card, setCard] = useState<Scorecard | null>(null);
  const [days, setDays] = useState(14);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async (d = days) => {
    setErr(null);
    try {
      const [p, c] = await Promise.all([
        fetch('/api/model/paths').then((r) => r.json()),
        fetch(`/api/model/scorecard?days=${d}`).then((r) => r.json()),
      ]);
      setPaths(p);
      setCard(c);
    } catch (e: any) {
      // A failure to READ the health panel is itself a health signal, so it is
      // surfaced rather than left as an empty panel that looks like "no data".
      setErr(e?.message || 'could not reach the backend');
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    refresh(days);
    const id = setInterval(() => refresh(days), 60000);
    return () => clearInterval(id);
  }, [refresh, days]);

  const tracker = paths?.tracker;
  const rotationUp = !!tracker?.rotation_loaded;
  const severeUp = !!tracker?.severe_loaded;
  const trackerRunning = !!tracker?.running;

  // The headline. Ordered by what a person needs to know first: a model that is
  // not loaded makes every number below meaningless, so it outranks them.
  let head: { tone: 'good' | 'warn' | 'bad'; text: string };
  if (err) {
    head = { tone: 'bad', text: 'Cannot reach the backend' };
  } else if (!trackerRunning) {
    head = { tone: 'warn', text: 'Storm tracking is not running' };
  } else if (!rotationUp && !severeUp) {
    head = { tone: 'bad', text: 'No models loaded — running physics only' };
  } else if (!rotationUp || !severeUp) {
    head = { tone: 'bad', text: `${rotationUp ? 'Severe' : 'Tornado'} model failed to load` };
  } else {
    head = { tone: 'good', text: 'Both models loaded and scoring' };
  }

  const unscored = card?.unscored ?? 0;
  const rows = card?.rows_in_window ?? 0;
  // A high unscored share is the exact signature of the original bug: the model
  // is present but cannot score the feature vector it is being handed.
  const unscoredShare = rows > 0 ? unscored / rows : 0;
  const unscoredAlarming = trackerRunning && rows > 200 && unscoredShare > 0.2;

  return (
    <div className="model-card health-card">
      <div className="model-card-head">
        <h3>Escalation Index — system health</h3>
        <span className={`model-badge ${head.tone}`}>{head.text}</span>
      </div>

      <p className="model-hint">
        The classifier once ran for months loading nothing at all, logging a line
        that read like a model simply hadn&rsquo;t been trained yet. This panel exists
        so that state is visible here instead of only in a log nobody opens.
      </p>

      {err && (
        <div className="model-msg err">
          {err} — the panel cannot confirm whether the models are loaded.
        </div>
      )}

      {/* ── Load state ─────────────────────────────────────────────────── */}
      <div className="health-loadrow">
        <LoadLight
          label="Tornado model"
          up={rotationUp}
          detail={
            !trackerRunning
              ? 'tracker stopped'
              : rotationUp
                ? sourceOf(paths, 'rotation_model.joblib')
                : 'not loaded'
          }
        />
        <LoadLight
          label="Severe model"
          up={severeUp}
          detail={
            !trackerRunning
              ? 'tracker stopped'
              : severeUp
                ? sourceOf(paths, 'severe_model.joblib')
                : 'not loaded'
          }
        />
        <LoadLight
          label="Storm tracking"
          up={trackerRunning}
          detail={
            trackerRunning
              ? `${tracker?.features ?? 0} features`
              : (tracker?.note || 'not running')
          }
        />
        <LoadLight
          label="Build"
          up={true}
          neutral
          detail={paths?.frozen ? 'packaged' : 'from source'}
        />
      </div>

      {!trackerRunning && !err && (
        <p className="model-hint warnline">
          Storm-cell tracking is off, so nothing is being scored and no new
          training rows are collected. It follows <code>nexrad_enabled</code>.
        </p>
      )}

      {trackerRunning && (!rotationUp || !severeUp) && (
        <p className="model-hint badline">
          A model that fails to load is a <strong>packaging fault</strong>, not an
          untrained model — the cells are being tracked but scored with nothing.
          Check the backend log for &ldquo;MISSING FROM THIS BUILD&rdquo;.
        </p>
      )}

      {!!paths?.degraded?.total && (
        <div className="health-degraded">
          <div className="hd-head">
            <span className="hd-title">Detectors reporting failures</span>
            <span className="hd-total">{num(paths.degraded.total)} total</span>
          </div>
          <p className="model-hint">
            These log once per run and then only count, so a detector that failed
            on one bad sweep and one that has failed thousands of times read very
            differently here and identically in the log.
          </p>
          <ul className="hd-list">
            {paths.degraded.detectors.slice(0, 6).map((d) => (
              <li key={d.key}>
                <span className="hd-count">{num(d.count)}</span>
                <span className="hd-what">{d.what}</span>
                <code className="hd-key">{d.key}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Live scorecard ─────────────────────────────────────────────── */}
      <div className="health-sub">
        <h4>Live performance</h4>
        <div className="health-window">
          {[7, 14, 30].map((d) => (
            <button
              key={d}
              className={`chip ${days === d ? 'on' : ''}`}
              onClick={() => setDays(d)}
            >
              {d}d
            </button>
          ))}
          <button className="chip" onClick={() => refresh(days)} disabled={loading}>
            {loading ? 'Checking…' : 'Refresh'}
          </button>
        </div>
      </div>

      <p className="model-hint">
        Scored against the warnings that actually followed, on live storms — not
        the fixed historical holdout the trainer reports. A row counts only once
        labelling has reached it.
      </p>

      {card?.error && <div className="model-msg err">{card.error}</div>}

      <div className="health-targets">
        {['severe', 'rotation'].map((key) => {
          const t = card?.targets?.[key];
          const label = TARGET_LABEL[key] ?? key;
          if (!t || !t.n) {
            return (
              <div className="health-target empty" key={key}>
                <span className="ht-name">{label}</span>
                <span className="ht-empty">
                  {t?.note || 'no completed predictions yet'}
                </span>
              </div>
            );
          }
          const lift = t.lift ?? null;
          const tone = lift === null ? 'warn' : lift >= 2 ? 'good' : 'bad';
          return (
            <div className={`health-target ${tone}`} key={key}>
              <div className="ht-head">
                <span className="ht-name">{label}</span>
                <span className={`model-badge ${tone}`}>
                  {lift === null
                    ? 'not enough data'
                    : lift >= 2
                      ? `${lift.toFixed(1)}× climatology`
                      : 'no better than chance'}
                </span>
              </div>
              <div className="ht-stats">
                <HStat label="Alerts verified" value={pct(t.precision, 0)} />
                <HStat label="Events caught" value={pct(t.recall, 0)} />
                <HStat label="Hits" value={num(t.tp)} sub={`${num(t.fp)} false`} />
                <HStat label="Base rate" value={pct(t.base_rate, 2)} />
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Coverage: the diagnostic that matters most ─────────────────── */}
      <div className="health-coverage">
        <HStat label="Rows in window" value={num(rows)} />
        <HStat label="Awaiting labels" value={num(card?.awaiting_labels)}
               sub="pending, not negative" />
        <HStat
          label="Never scored"
          value={num(unscored)}
          sub={rows ? `${(unscoredShare * 100).toFixed(0)}% of rows` : undefined}
          tone={unscoredAlarming ? 'bad' : undefined}
        />
      </div>

      {unscoredAlarming && (
        <p className="model-hint badline">
          <strong>{(unscoredShare * 100).toFixed(0)}% of cells were never scored.</strong>{' '}
          The models are present but something is failing per cell — this is the
          signature of the feature vector and the estimator having drifted apart.
          The backend logs it once per run.
        </p>
      )}

      {card?.verdict && !card.error && (
        <p className="health-verdict">{card.verdict}</p>
      )}
    </div>
  );
};

/** Where a loaded model came from — a retrained copy, or the shipped seed. */
function sourceOf(paths: PathsResponse | null, name: string): string {
  const m = paths?.models?.[name];
  if (!m?.found) return 'not found';
  return m.source === 'runtime' ? 'retrained copy' : 'shipped with build';
}

const LoadLight: React.FC<{
  label: string; up: boolean; detail?: string; neutral?: boolean;
}> = ({ label, up, detail, neutral }) => (
  <div className={`health-light ${neutral ? 'neutral' : up ? 'up' : 'down'}`}>
    <span className="hl-dot" aria-hidden="true" />
    <span className="hl-text">
      <span className="hl-label">{label}</span>
      <span className="hl-detail">{detail}</span>
    </span>
  </div>
);

const HStat: React.FC<{
  label: string; value: string; sub?: string; tone?: 'bad';
}> = ({ label, value, sub, tone }) => (
  <div className="model-stat">
    <span className="model-stat-label">{label}</span>
    <span className={`model-stat-value ${tone === 'bad' ? 'is-bad' : ''}`}>{value}</span>
    {sub && <span className="model-stat-sub">{sub}</span>}
  </div>
);

export default SystemHealthPanel;

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { SystemHealthPanel } from './SystemHealthPanel';

/**
 * Model dashboard — the rotation classifier, its training archive, and the
 * archive-replay backfill, without a terminal.
 *
 * Three things it has to answer at a glance:
 *   1. Is the live model any good?  Held-out AP and ROC-AUC, not the training
 *      score.  A model can look excellent in training and be noise in the field
 *      (the one that shipped for months scored AUC 0.529 with 14 true positives
 *      against 49,726 false ones), so this shows only never-seen-data numbers
 *      and flags anything below the promotion floor.
 *   2. Is the training data healthy?  Class balance, coverage, and a per-feature
 *      population bar — three features sat at 0.0 in every row for months and
 *      nothing surfaced it.
 *   3. What can we replay, and how long will it take?  Pick severe days, see
 *      the estimate, start it, watch it.
 */

const SITE_PRESETS = ['KILN', 'KIWX', 'KIND', 'KCLE', 'KDTX', 'KGRR', 'KPBZ', 'KLOT'];

// Mirrors MIN_USEFUL_AUC / MIN_AP_LIFT in the promotion gate. Shown so the UI
// tells the same story the backend enforces.
const MIN_USEFUL_AUC = 0.6;
const MIN_AP_LIFT = 2.0;

interface Holdout {
  n?: number; n_pos?: number; auc?: number; ap?: number; brier?: number;
  precision?: number; recall?: number; tp?: number; fp?: number; fn?: number; tn?: number;
  degenerate?: boolean;
}
interface ModelStatus {
  available?: boolean; enabled?: boolean; running?: boolean; phase?: string | null;
  interval_hours?: number; model_exists?: boolean; can_rollback?: boolean;
  last_run?: any; last_promotion?: any;
  live_model?: { trained_at?: string; n_rows?: number; n_pos?: number; holdout?: Holdout } | null;
  history?: any[];
}
interface DataStats {
  exists?: boolean; total?: number; labeled?: number; positives?: number;
  negatives?: number; unlabeled?: number; positive_rate?: number;
  first_ts?: string; last_ts?: string; positive_days?: number;
  top_positive_days?: { day: string; n: number }[];
  by_month?: { month: string; n: number }[];
  sites?: { site: string; n: number }[];
  features?: { name: string; pct_nonzero: number; dead: boolean }[];
}
interface Candidate {
  day: string; tor: number; svr: number; sites: string[];
  est_volumes: number; est_minutes: number;
}
interface BackfillStatus {
  running?: boolean; started_at?: string; days?: string[]; sites?: string[];
  workers?: number; pairs_done?: number; pairs_total?: number;
  rows?: number; volumes?: number; errors?: number;
  current?: { volume: number; of: number; rows: number; errors: number; rate: number } | null;
  eta_minutes?: number | null;
  recent?: { site: string; day: string; rows: number; volumes: number; errors: number }[];
  output_exists?: boolean; output_rows?: number; log_tail?: string[];
}

const fmt = (n?: number, d = 0) =>
  n === undefined || n === null || Number.isNaN(n) ? '—' : n.toLocaleString(undefined, {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });
const pct = (n?: number, d = 1) => (n === undefined || n === null ? '—' : `${(n * 100).toFixed(d)}%`);
const ago = (iso?: string) => {
  if (!iso) return '—';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

export const ModelSection: React.FC = () => {
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [stats, setStats] = useState<DataStats | null>(null);
  const [job, setJob] = useState<BackfillStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const today = new Date().toISOString().slice(0, 10);
  const yearAgo = new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
  const [start, setStart] = useState(yearAgo);
  const [end, setEnd] = useState(today);
  const [sites, setSites] = useState<string[]>(['KILN', 'KIWX', 'KIND', 'KCLE']);
  const [minTor, setMinTor] = useState(1);
  const [workers, setWorkers] = useState(3);
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [searching, setSearching] = useState(false);

  const note = (kind: 'ok' | 'err', text: string) => {
    setMsg({ kind, text });
    setTimeout(() => setMsg(null), 8000);
  };

  const refresh = useCallback(async (withStats = false) => {
    try {
      const [m, j] = await Promise.all([
        fetch('/api/model/rotation/status').then((r) => r.json()),
        fetch('/api/model/backfill/status').then((r) => r.json()),
      ]);
      setModel(m); setJob(j);
      if (withStats) {
        const s = await fetch('/api/model/training/stats').then((r) => r.json());
        setStats(s);
      }
    } catch { /* transient; the poll will retry */ }
  }, []);

  useEffect(() => { refresh(true); }, [refresh]);
  useEffect(() => {
    // Poll fast while a job runs so the progress bar actually moves, slowly
    // otherwise — the stats pass streams a large file.
    const fast = !!job?.running;
    const id = setInterval(() => refresh(false), fast ? 4000 : 20000);
    return () => clearInterval(id);
  }, [job?.running, refresh]);

  const search = async () => {
    if (!sites.length) return note('err', 'Pick at least one radar site.');
    setSearching(true); setCandidates(null);
    try {
      const r = await fetch(
        `/api/model/backfill/candidates?start=${start}&end=${end}` +
        `&sites=${sites.join(',')}&min_tor=${minTor}`);
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const d = await r.json();
      setCandidates(d.days || []);
      setPicked(new Set());
      if (!d.days?.length) note('err', 'No days with tornado warnings in that range.');
    } catch (e: any) {
      note('err', `Search failed: ${e.message}`);
    } finally { setSearching(false); }
  };

  const startJob = async () => {
    if (!picked.size) return note('err', 'Select at least one day.');
    setBusy('start');
    try {
      const r = await fetch('/api/model/backfill/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: [...picked], sites, workers, full_day: false, min_tor: minTor }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      note('ok', `Started: ${d.pairs} (site, day) pairs.`);
      refresh();
    } catch (e: any) { note('err', e.message); } finally { setBusy(null); }
  };

  const post = async (url: string, label: string, key: string) => {
    setBusy(key);
    try {
      const r = await fetch(url, { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      note('ok', `${label}: ${d.why || d.outcome || d.rows_merged !== undefined
        ? (d.rows_merged !== undefined ? `${fmt(d.rows_merged)} rows merged` : (d.why || d.outcome))
        : 'done'}`);
      refresh(true);
    } catch (e: any) { note('err', `${label} failed: ${e.message}`); } finally { setBusy(null); }
  };

  const hold = model?.live_model?.holdout;
  const baseRate = hold?.n && hold?.n_pos ? hold.n_pos / hold.n : undefined;
  const modelVerdict = useMemo(() => {
    if (!model?.model_exists) return { tone: 'warn', text: 'No model in production' };
    if (!hold || hold.degenerate) return { tone: 'warn', text: 'No usable held-out score' };
    if ((hold.auc ?? 0) < MIN_USEFUL_AUC)
      return { tone: 'bad', text: `Below the ${MIN_USEFUL_AUC} AUC floor — not ranking storms` };
    if (baseRate && (hold.ap ?? 0) < baseRate * MIN_AP_LIFT)
      return { tone: 'bad', text: 'AP is at the base rate — no better than guessing' };
    if (!hold.tp) return { tone: 'bad', text: 'Catches nothing at the operating threshold' };
    return { tone: 'good', text: 'Clears the promotion floor' };
  }, [model, hold, baseRate]);

  const pctDone = job?.pairs_total ? (job.pairs_done || 0) / job.pairs_total : 0;

  return (
    <div className="section active model-section">
      <h2 className="section-title">Model &amp; Training Data</h2>

      {msg && (
        <div className={`model-msg ${msg.kind}`}>{msg.text}</div>
      )}

      {/* Health first: if the models are not loaded, every figure below it is
          describing a model that is not actually scoring anything. */}
      <SystemHealthPanel />

      {/* ── Live model ─────────────────────────────────────────────── */}
      <div className="model-card">
        <div className="model-card-head">
          <h3>Rotation classifier</h3>
          <span className={`model-badge ${modelVerdict.tone}`}>{modelVerdict.text}</span>
        </div>
        <p className="model-hint">
          All figures are on <strong>held-out convective days</strong> the model never
          trained on. Watch average precision, not ROC-AUC — at this class balance AUC
          stays flattering while the precision that matters on air moves a lot.
        </p>
        <div className="model-grid">
          <Stat label="Average precision" value={hold?.ap?.toFixed(4)} sub={
            baseRate ? `base rate ${baseRate.toFixed(5)}` : undefined} />
          <Stat label="ROC-AUC" value={hold?.auc?.toFixed(3)} sub={`floor ${MIN_USEFUL_AUC}`} />
          <Stat label="Precision" value={pct(hold?.precision)} />
          <Stat label="Recall" value={pct(hold?.recall)} />
          <Stat label="Caught" value={fmt(hold?.tp)} sub={`missed ${fmt(hold?.fn)}`} />
          <Stat label="False alarms" value={fmt(hold?.fp)} />
          <Stat label="Trained" value={ago(model?.live_model?.trained_at)}
                sub={model?.live_model?.n_rows ? `${fmt(model.live_model.n_rows)} rows` : undefined} />
          <Stat label="Auto-retrain" value={model?.enabled ? 'On' : 'Off'}
                sub={model?.phase ? model.phase : `every ${model?.interval_hours ?? '—'}h`} />
        </div>
        <div className="model-actions">
          <button className="btn" disabled={busy === 'retrain'}
                  onClick={() => post('/api/model/rotation/retrain', 'Retrain', 'retrain')}>
            {busy === 'retrain' ? 'Training…' : 'Retrain now'}
          </button>
          <button className="btn btn-ghost" disabled={!model?.can_rollback || busy === 'rollback'}
                  onClick={() => post('/api/model/rotation/rollback', 'Rollback', 'rollback')}>
            Roll back
          </button>
          <span className="model-hint inline">
            A retrain cannot push a worse model live — the promotion gate compares it to
            the incumbent on the same unseen days.
          </span>
        </div>
        {!!model?.history?.length && (
          <table className="model-table">
            <thead><tr><th>When</th><th>Outcome</th><th>Why</th></tr></thead>
            <tbody>
              {model.history.slice().reverse().slice(0, 6).map((h: any, i: number) => (
                <tr key={i}>
                  <td>{ago(h.at)}</td>
                  <td><span className={`pill ${h.outcome}`}>{h.outcome}</span></td>
                  <td className="muted">{h.why || h.reason || h.error || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Training archive ───────────────────────────────────────── */}
      <div className="model-card">
        <div className="model-card-head">
          <h3>Training archive</h3>
          <button className="btn btn-ghost btn-sm" onClick={() => refresh(true)}>Refresh</button>
        </div>
        <div className="model-grid">
          <Stat label="Rows" value={fmt(stats?.total)} />
          <Stat label="Labelled" value={fmt(stats?.labeled)}
                sub={stats?.unlabeled ? `${fmt(stats.unlabeled)} unlabelled` : undefined} />
          <Stat label="Positives" value={fmt(stats?.positives)}
                sub={stats?.positive_rate !== undefined ? pct(stats.positive_rate, 2) : undefined} />
          <Stat label="Days with positives" value={fmt(stats?.positive_days)} />
        </div>
        {!!stats?.features?.length && (
          <>
            <p className="model-hint">
              Feature population. A column at <strong>0%</strong> is a wiring bug, not a
              quiet feature — three of these sat dead in every row for months.
            </p>
            <div className="feat-bars">
              {stats.features.map((f) => (
                <div className={`feat-row ${f.dead ? 'dead' : ''}`} key={f.name}>
                  <span className="feat-name">{f.name}</span>
                  <span className="feat-track">
                    <span className="feat-fill" style={{ width: `${f.pct_nonzero}%` }} />
                  </span>
                  <span className="feat-pct">{f.dead ? 'DEAD' : `${f.pct_nonzero}%`}</span>
                </div>
              ))}
            </div>
          </>
        )}
        {!!stats?.top_positive_days?.length && (
          <p className="model-hint">
            Busiest labelled days:{' '}
            {stats.top_positive_days.map((d) => `${d.day} (${d.n})`).join(', ')}
          </p>
        )}
      </div>

      {/* ── Backfill ───────────────────────────────────────────────── */}
      <div className="model-card">
        <div className="model-card-head">
          <h3>Backfill from the archive</h3>
          {job?.running && <span className="model-badge good">Running</span>}
        </div>

        {job?.running ? (
          <>
            <div className="bf-progress">
              <div className="bf-bar"><div className="bf-fill" style={{ width: `${pctDone * 100}%` }} /></div>
              <div className="bf-meta">
                <span>{job.pairs_done}/{job.pairs_total} pairs</span>
                <span>{fmt(job.rows)} rows</span>
                <span>{fmt(job.volumes)} volumes</span>
                {!!job.errors && <span className="bad">{fmt(job.errors)} errors</span>}
                {job.eta_minutes != null && <span>~{fmt(job.eta_minutes)}m left</span>}
                {job.workers && <span>{job.workers} workers</span>}
              </div>
            </div>
            {job.current && (
              <p className="model-hint">
                Current pair: volume {job.current.volume}/{job.current.of} ·{' '}
                {job.current.rate.toFixed(2)} vol/s
              </p>
            )}
            <div className="model-actions">
              <button className="btn btn-danger" disabled={busy === 'stop'}
                      onClick={() => post('/api/model/backfill/stop', 'Stop', 'stop')}>
                Stop
              </button>
              <span className="model-hint inline">
                Completed pairs are checkpointed — restarting resumes rather than redoing.
              </span>
            </div>
            {!!job.log_tail?.length && (
              <pre className="bf-log">{job.log_tail.join('\n')}</pre>
            )}
          </>
        ) : (
          <>
            <div className="bf-form">
              <label>From<input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
              <label>To<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
              <label>Min tornado warnings
                <input type="number" min={0} max={50} value={minTor}
                       onChange={(e) => setMinTor(+e.target.value)} />
              </label>
              <label>Workers
                <input type="number" min={1} max={8} value={workers}
                       onChange={(e) => setWorkers(+e.target.value)} />
              </label>
            </div>
            <div className="bf-sites">
              {SITE_PRESETS.map((s) => (
                <button key={s}
                        className={`chip ${sites.includes(s) ? 'on' : ''}`}
                        onClick={() => setSites((cur) =>
                          cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s])}>
                  {s}
                </button>
              ))}
            </div>
            <div className="model-actions">
              <button className="btn" onClick={search} disabled={searching}>
                {searching ? 'Searching IEM…' : 'Find severe days'}
              </button>
              {!!candidates?.length && (
                <button className="btn btn-primary" onClick={startJob}
                        disabled={!picked.size || busy === 'start'}>
                  Replay {picked.size || ''} selected
                </button>
              )}
              {job?.output_exists && !!job.output_rows && (
                <button className="btn btn-ghost" disabled={busy === 'merge'}
                        onClick={() => post('/api/model/backfill/merge', 'Merge', 'merge')}>
                  Label + merge {fmt(job.output_rows)} rows
                </button>
              )}
            </div>
            <p className="model-hint">
              <strong>Order matters:</strong> replay → <em>Label + merge</em> → Retrain.
              Merging labels the rows first; merging raw would leave them
              unlabelled and every training run would silently skip them.
            </p>
            <p className="model-hint">
              Replays padded windows around each cluster of <em>tornado</em> warnings,
              not the whole day, and skips a site-day with fewer than the minimum above —
              a site with no tornado warnings contributes only negatives, which the
              archive already has 450k of. Roughly 20s per
              volume — the estimate below already accounts for it. Each worker needs
              about 3&nbsp;GB, and the job caps itself to what free RAM can carry.
            </p>

            {candidates && (
              <table className="model-table">
                <thead>
                  <tr>
                    <th style={{ width: 32 }}>
                      <input type="checkbox"
                             checked={!!candidates.length && picked.size === candidates.length}
                             onChange={(e) => setPicked(e.target.checked
                               ? new Set(candidates.map((c) => c.day)) : new Set())} />
                    </th>
                    <th>Day</th><th>TOR</th><th>SVR</th><th>Sites</th><th>Est. time</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.day} className={picked.has(c.day) ? 'picked' : ''}>
                      <td>
                        <input type="checkbox" checked={picked.has(c.day)}
                               onChange={() => setPicked((cur) => {
                                 const n = new Set(cur);
                                 n.has(c.day) ? n.delete(c.day) : n.add(c.day);
                                 return n;
                               })} />
                      </td>
                      <td>{c.day}</td>
                      <td><strong>{c.tor}</strong></td>
                      <td className="muted">{c.svr}</td>
                      <td className="muted">{c.sites.join(' ')}</td>
                      <td className="muted">
                        {c.est_minutes >= 60
                          ? `${(c.est_minutes / 60).toFixed(1)}h`
                          : `${c.est_minutes}m`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!!picked.size && (
              <p className="model-hint">
                Selected {picked.size} day(s) ≈{' '}
                {(() => {
                  const mins = candidates!
                    .filter((c) => picked.has(c.day))
                    .reduce((a, c) => a + c.est_minutes, 0) / Math.max(1, workers);
                  return mins >= 60 ? `${(mins / 60).toFixed(1)} hours` : `${Math.round(mins)} minutes`;
                })()}{' '}
                at {workers} worker{workers > 1 ? 's' : ''}.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
};

const Stat: React.FC<{ label: string; value?: string | number; sub?: string }> = ({
  label, value, sub,
}) => (
  <div className="model-stat">
    <span className="model-stat-label">{label}</span>
    <span className="model-stat-value">{value ?? '—'}</span>
    {sub && <span className="model-stat-sub">{sub}</span>}
  </div>
);

export default ModelSection;

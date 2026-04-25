import { useCallback, useEffect, useRef, useState } from 'react';
import type { Alert } from '../types/alert';
import { apiUrl } from '../utils/api';

const STORAGE_KEY = 'alertChimesEnabled';

// Phenomena that trigger chimes (warnings only)
const CHIME_PHENOMENA = new Set(['TO', 'SV', 'FF']);

// Sound event type keys
export type SoundEventType = 'tornado_warning' | 'severe_warning' | 'alert_update';

export interface SoundConfig {
  label: string;
  volume: number;
  custom_file: string | null;
  custom_url: string | null;
}

export interface SoundsConfig {
  [key: string]: SoundConfig;
}

// --- Web Audio API chime synthesis ---

function playTornadoChime(ctx: AudioContext, volume: number) {
  [0, 150, 300].forEach(delay => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 1200;
    osc.type = 'square';
    const t = ctx.currentTime + delay / 1000;
    gain.gain.setValueAtTime(0.3 * volume, t);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.12);
    osc.start(t);
    osc.stop(t + 0.12);
  });
}

function playAlertChime(ctx: AudioContext, volume: number) {
  [880, 660].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = freq;
    osc.type = 'sine';
    const t = ctx.currentTime + i * 0.2;
    gain.gain.setValueAtTime(0.25 * volume, t);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.18);
    osc.start(t);
    osc.stop(t + 0.18);
  });
}

function playUpdateChime(ctx: AudioContext, volume: number) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.frequency.value = 520;
  osc.type = 'sine';
  gain.gain.setValueAtTime(0.15 * volume, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
  osc.start();
  osc.stop(ctx.currentTime + 0.25);
}

function playCustomFile(url: string, volume: number) {
  const audio = new Audio(url);
  audio.volume = Math.max(0, Math.min(1, volume));
  audio.play().catch(() => { /* autoplay policy — ignore */ });
}

// --- Hook ---

export function useAlertChimes() {
  const [enabled, setEnabled] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) !== 'false';
    } catch {
      return true;
    }
  });

  const audioCtxRef = useRef<AudioContext | null>(null);
  const soundConfigRef = useRef<SoundsConfig>({});
  const [soundConfig, setSoundConfig] = useState<SoundsConfig>({});

  // Fetch sound config from backend
  const fetchSoundConfig = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/settings/sounds'));
      if (!res.ok) return;
      const data = await res.json();
      soundConfigRef.current = data.sounds || {};
      setSoundConfig(data.sounds || {});
    } catch {
      // Use defaults if backend is unreachable
    }
  }, []);

  useEffect(() => {
    fetchSoundConfig();
  }, [fetchSoundConfig]);

  const getAudioContext = useCallback(() => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new AudioContext();
    }
    if (audioCtxRef.current.state === 'suspended') {
      audioCtxRef.current.resume();
    }
    return audioCtxRef.current;
  }, []);

  const toggleEnabled = useCallback(() => {
    setEnabled(prev => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_KEY, String(next));
      } catch { /* ignore */ }
      return next;
    });
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, String(enabled));
    } catch { /* ignore */ }
  }, [enabled]);

  // Play a specific sound event type (used by settings test buttons too)
  const playEventType = useCallback((eventType: SoundEventType) => {
    const cfg = soundConfigRef.current[eventType];
    const volume = cfg?.volume ?? (eventType === 'alert_update' ? 0.5 : eventType === 'severe_warning' ? 0.7 : 0.8);

    if (cfg?.custom_url) {
      playCustomFile(cfg.custom_url, volume);
      return;
    }

    const ctx = getAudioContext();
    if (eventType === 'tornado_warning') {
      playTornadoChime(ctx, volume);
    } else if (eventType === 'severe_warning') {
      playAlertChime(ctx, volume);
    } else {
      playUpdateChime(ctx, volume);
    }
  }, [getAudioContext]);

  const playForAlert = useCallback((alert: Alert, type: 'new' | 'update') => {
    if (!enabled) return;
    if (!CHIME_PHENOMENA.has(alert.phenomenon)) return;
    if (alert.significance !== 'W') return;

    if (type === 'new') {
      playEventType(alert.phenomenon === 'TO' ? 'tornado_warning' : 'severe_warning');
    } else {
      playEventType('alert_update');
    }
  }, [enabled, playEventType]);

  useEffect(() => {
    return () => {
      if (audioCtxRef.current) {
        audioCtxRef.current.close();
        audioCtxRef.current = null;
      }
    };
  }, []);

  return {
    chimesEnabled: enabled,
    toggleChimes: toggleEnabled,
    playForAlert,
    playEventType,
    soundConfig,
    refreshSoundConfig: fetchSoundConfig,
  };
}

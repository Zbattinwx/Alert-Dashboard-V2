import React, { useEffect, useState, useCallback, useRef } from 'react';
import { ALERT_COLORS, getAlertStyle } from '../types/alert';
import { apiUrl } from '../utils/api';
import type { SoundEventType, SoundsConfig } from '../hooks/useAlertChimes';

interface PhenomenonItem {
  code: string;
  name: string;
  enabled: boolean;
}

interface PhenomenaResponse {
  categories: Record<string, PhenomenonItem[]>;
  active_phenomena: string[];
  using_overrides: boolean;
}

interface StateItem {
  code: string;
  name: string;
  enabled: boolean;
}

interface StatesResponse {
  states: StateItem[];
  active_states: string[];
  using_overrides: boolean;
}

interface CountyItem {
  code: string;
  name: string;
  enabled: boolean;
}

interface CountyStateInfo {
  state_name: string;
  counties: CountyItem[];
  all_selected: boolean;
}

interface CountiesResponse {
  states: Record<string, CountyStateInfo>;
  using_overrides: boolean;
}

interface GeneralSettings {
  nexrad_enabled: boolean;
  nexrad_default_site: string;
  llm_enabled: boolean;
  agent_enabled: boolean;
  google_chat_enabled: boolean;
  using_overrides: boolean;
}

interface SettingsSectionProps {
  chimesEnabled?: boolean;
  onToggleChimes?: () => void;
  playEventType?: (type: SoundEventType) => void;
  soundConfig?: SoundsConfig;
  refreshSoundConfig?: () => void;
}

export const SettingsSection: React.FC<SettingsSectionProps> = ({
  chimesEnabled = false,
  onToggleChimes,
  playEventType,
  soundConfig: externalSoundConfig,
  refreshSoundConfig,
}) => {
  // ── Phenomena state ──────────────────────────────────────────────────
  const [categories, setCategories] = useState<Record<string, PhenomenonItem[]>>({});
  const [activePhenomena, setActivePhenomena] = useState<Set<string>>(new Set());
  const [savedPhenomena, setSavedPhenomena] = useState<Set<string>>(new Set());
  const [usingPhenomenaOverrides, setUsingPhenomenaOverrides] = useState(false);
  const [phenomenaLoading, setPhenomenaLoading] = useState(true);
  const [phenomenaSaving, setPhenomenaSaving] = useState(false);
  const [phenomenaError, setPhenomenaError] = useState<string | null>(null);
  const [phenomenaMsg, setPhenomenaMsg] = useState<string | null>(null);

  // ── States state ──────────────────────────────────────────────────────
  const [allStates, setAllStates] = useState<StateItem[]>([]);
  const [activeStates, setActiveStates] = useState<Set<string>>(new Set());
  const [savedStates, setSavedStates] = useState<Set<string>>(new Set());
  const [usingStatesOverrides, setUsingStatesOverrides] = useState(false);
  const [statesLoading, setStatesLoading] = useState(true);
  const [statesSaving, setStatesSaving] = useState(false);
  const [statesError, setStatesError] = useState<string | null>(null);
  const [statesMsg, setStatesMsg] = useState<string | null>(null);

  // ── County filter state ───────────────────────────────────────────────
  const [countyStates, setCountyStates] = useState<Record<string, CountyStateInfo>>({});
  const [countySel, setCountySel] = useState<Record<string, Set<string>>>({});
  const [savedCountySel, setSavedCountySel] = useState<Record<string, Set<string>>>({});
  const [usingCountyOverrides, setUsingCountyOverrides] = useState(false);
  const [countiesLoading, setCountiesLoading] = useState(true);
  const [countiesSaving, setCountiesSaving] = useState(false);
  const [countiesError, setCountiesError] = useState<string | null>(null);
  const [countiesMsg, setCountiesMsg] = useState<string | null>(null);

  // ── General settings state ────────────────────────────────────────────
  const [general, setGeneral] = useState<GeneralSettings | null>(null);
  const [savedGeneral, setSavedGeneral] = useState<GeneralSettings | null>(null);
  const [generalLoading, setGeneralLoading] = useState(true);
  const [generalSaving, setGeneralSaving] = useState(false);
  const [generalError, setGeneralError] = useState<string | null>(null);
  const [generalMsg, setGeneralMsg] = useState<string | null>(null);
  const [nexradSiteInput, setNexradSiteInput] = useState('');

  // ── Ticker filter state ───────────────────────────────────────────────
  const [tickerExcluded, setTickerExcluded] = useState<Set<string>>(new Set());
  const [savedTickerExcluded, setSavedTickerExcluded] = useState<Set<string>>(new Set());
  const [tickerSaving, setTickerSaving] = useState(false);
  const [tickerMsg, setTickerMsg] = useState<string | null>(null);

  // ── Google Chat filter state ──────────────────────────────────────────
  const [gchatTypes, setGchatTypes] = useState<{ key: string; label: string }[]>([]);
  const [gchatSend, setGchatSend] = useState<Set<string>>(new Set());
  const [savedGchatSend, setSavedGchatSend] = useState<Set<string>>(new Set());
  const [gchatSaving, setGchatSaving] = useState(false);
  const [gchatMsg, setGchatMsg] = useState<string | null>(null);

  // ── Sound settings state ──────────────────────────────────────────────
  const SOUND_EVENT_TYPES: { key: SoundEventType; label: string; description: string; defaultVolume: number }[] = [
    { key: 'tornado_warning', label: 'Tornado Warning', description: 'New Tornado Warning issued', defaultVolume: 0.8 },
    { key: 'severe_warning', label: 'Severe / Flash Flood Warning', description: 'New SVR or FFW issued', defaultVolume: 0.7 },
    { key: 'alert_update', label: 'Alert Update', description: 'Existing warning updated', defaultVolume: 0.5 },
  ];
  const [soundVolumes, setSoundVolumes] = useState<Record<SoundEventType, number>>({
    tornado_warning: 0.8,
    severe_warning: 0.7,
    alert_update: 0.5,
  });
  const [savedSoundVolumes, setSavedSoundVolumes] = useState<Record<SoundEventType, number>>({
    tornado_warning: 0.8,
    severe_warning: 0.7,
    alert_update: 0.5,
  });
  const [soundSaving, setSoundSaving] = useState(false);
  const [soundMsg, setSoundMsg] = useState<string | null>(null);
  const [uploadingFor, setUploadingFor] = useState<SoundEventType | null>(null);
  const fileInputRefs = useRef<Partial<Record<SoundEventType, HTMLInputElement | null>>>({});

  // Sync volumes from external sound config when it changes
  useEffect(() => {
    if (!externalSoundConfig) return;
    const vols: Record<SoundEventType, number> = {
      tornado_warning: externalSoundConfig.tornado_warning?.volume ?? 0.8,
      severe_warning: externalSoundConfig.severe_warning?.volume ?? 0.7,
      alert_update: externalSoundConfig.alert_update?.volume ?? 0.5,
    };
    setSoundVolumes(vols);
    setSavedSoundVolumes(vols);
  }, [externalSoundConfig]);

  const soundVolumesHaveChanges = SOUND_EVENT_TYPES.some(
    ({ key }) => Math.abs((soundVolumes[key] ?? 0) - (savedSoundVolumes[key] ?? 0)) > 0.001
  );

  const handleSoundVolumesSave = async () => {
    setSoundSaving(true);
    setSoundMsg(null);
    try {
      const payload: Record<string, { volume: number }> = {};
      SOUND_EVENT_TYPES.forEach(({ key }) => {
        payload[key] = { volume: soundVolumes[key] };
      });
      const res = await fetch(apiUrl('/api/settings/sounds'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sounds: payload }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const newVols: Record<SoundEventType, number> = {
        tornado_warning: data.sounds?.tornado_warning?.volume ?? soundVolumes.tornado_warning,
        severe_warning: data.sounds?.severe_warning?.volume ?? soundVolumes.severe_warning,
        alert_update: data.sounds?.alert_update?.volume ?? soundVolumes.alert_update,
      };
      setSoundVolumes(newVols);
      setSavedSoundVolumes(newVols);
      setSoundMsg('Volume settings saved.');
      refreshSoundConfig?.();
    } catch (err) {
      setSoundMsg(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSoundSaving(false);
    }
  };

  const handleSoundUpload = async (eventType: SoundEventType, file: File) => {
    setUploadingFor(eventType);
    setSoundMsg(null);
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch(apiUrl(`/api/settings/sounds/${eventType}/upload`), {
        method: 'POST',
        body: form,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      setSoundMsg(`Custom sound uploaded for ${SOUND_EVENT_TYPES.find(t => t.key === eventType)?.label}.`);
      refreshSoundConfig?.();
    } catch (err) {
      setSoundMsg(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploadingFor(null);
    }
  };

  const handleSoundReset = async (eventType: SoundEventType) => {
    setSoundMsg(null);
    try {
      const res = await fetch(apiUrl(`/api/settings/sounds/${eventType}`), { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSoundMsg(`Reset to built-in chime.`);
      refreshSoundConfig?.();
    } catch (err) {
      setSoundMsg(err instanceof Error ? err.message : 'Reset failed');
    }
  };

  // ── Derived ───────────────────────────────────────────────────────────
  const phenomenaHasChanges = (() => {
    if (activePhenomena.size !== savedPhenomena.size) return true;
    for (const code of activePhenomena) if (!savedPhenomena.has(code)) return true;
    return false;
  })();

  const statesHasChanges = (() => {
    if (activeStates.size !== savedStates.size) return true;
    for (const s of activeStates) if (!savedStates.has(s)) return true;
    return false;
  })();

  const countiesHasChanges = (() => {
    const keys = new Set([...Object.keys(countySel), ...Object.keys(savedCountySel)]);
    for (const st of keys) {
      const cur = countySel[st] ?? new Set<string>();
      const saved = savedCountySel[st] ?? new Set<string>();
      if (cur.size !== saved.size) return true;
      for (const c of cur) if (!saved.has(c)) return true;
    }
    return false;
  })();

  const generalHasChanges = (() => {
    if (!general || !savedGeneral) return false;
    return (
      general.nexrad_enabled !== savedGeneral.nexrad_enabled ||
      general.llm_enabled !== savedGeneral.llm_enabled ||
      general.agent_enabled !== savedGeneral.agent_enabled ||
      general.google_chat_enabled !== savedGeneral.google_chat_enabled ||
      nexradSiteInput !== savedGeneral.nexrad_default_site
    );
  })();

  const tickerHasChanges = (() => {
    if (tickerExcluded.size !== savedTickerExcluded.size) return true;
    for (const k of tickerExcluded) if (!savedTickerExcluded.has(k)) return true;
    return false;
  })();

  const gchatHasChanges = (() => {
    if (gchatSend.size !== savedGchatSend.size) return true;
    for (const k of gchatSend) if (!savedGchatSend.has(k)) return true;
    return false;
  })();

  // ── Fetchers ──────────────────────────────────────────────────────────
  const fetchPhenomena = useCallback(async () => {
    setPhenomenaLoading(true);
    setPhenomenaError(null);
    try {
      const res = await fetch(apiUrl('/api/settings/phenomena'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: PhenomenaResponse = await res.json();
      setCategories(data.categories);
      const s = new Set(data.active_phenomena);
      setActivePhenomena(s);
      setSavedPhenomena(s);
      setUsingPhenomenaOverrides(data.using_overrides);
    } catch (err) {
      setPhenomenaError(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      setPhenomenaLoading(false);
    }
  }, []);

  const fetchStates = useCallback(async () => {
    setStatesLoading(true);
    setStatesError(null);
    try {
      const res = await fetch(apiUrl('/api/settings/states'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: StatesResponse = await res.json();
      setAllStates(data.states);
      const s = new Set(data.active_states);
      setActiveStates(s);
      setSavedStates(s);
      setUsingStatesOverrides(data.using_overrides);
    } catch (err) {
      setStatesError(err instanceof Error ? err.message : 'Failed to load states');
    } finally {
      setStatesLoading(false);
    }
  }, []);

  const fetchCounties = useCallback(async () => {
    setCountiesLoading(true);
    setCountiesError(null);
    try {
      const res = await fetch(apiUrl('/api/settings/counties'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: CountiesResponse = await res.json();
      setCountyStates(data.states);
      const sel: Record<string, Set<string>> = {};
      Object.entries(data.states).forEach(([st, info]) => {
        sel[st] = new Set(info.counties.filter(c => c.enabled).map(c => c.code));
      });
      setCountySel(sel);
      setSavedCountySel(Object.fromEntries(Object.entries(sel).map(([k, v]) => [k, new Set(v)])));
      setUsingCountyOverrides(data.using_overrides);
    } catch (err) {
      setCountiesError(err instanceof Error ? err.message : 'Failed to load counties');
    } finally {
      setCountiesLoading(false);
    }
  }, []);

  const fetchGeneral = useCallback(async () => {
    setGeneralLoading(true);
    setGeneralError(null);
    try {
      const res = await fetch(apiUrl('/api/settings/general'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: GeneralSettings = await res.json();
      setGeneral(data);
      setSavedGeneral(data);
      setNexradSiteInput(data.nexrad_default_site);
    } catch (err) {
      setGeneralError(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      setGeneralLoading(false);
    }
  }, []);

  const fetchTickerSettings = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/settings/ticker'));
      if (res.ok) {
        const data = await res.json();
        const excluded = new Set<string>(data.excluded_types || []);
        setTickerExcluded(excluded);
        setSavedTickerExcluded(excluded);
      }
    } catch { /* ignore */ }
  }, []);

  const fetchGchatSettings = useCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/settings/google-chat'));
      if (res.ok) {
        const data = await res.json();
        const types = (data.types || []) as { key: string; label: string; send: boolean }[];
        setGchatTypes(types.map(t => ({ key: t.key, label: t.label })));
        const sendSet = new Set<string>(types.filter(t => t.send).map(t => t.key));
        setGchatSend(sendSet);
        setSavedGchatSend(sendSet);
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    fetchPhenomena();
    fetchStates();
    fetchCounties();
    fetchGeneral();
    fetchTickerSettings();
    fetchGchatSettings();
  }, [fetchPhenomena, fetchStates, fetchCounties, fetchGeneral, fetchTickerSettings, fetchGchatSettings]);

  // ── Phenomena handlers ────────────────────────────────────────────────
  const togglePhenomenon = (code: string) => {
    setActivePhenomena(prev => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });
    setPhenomenaMsg(null);
  };

  const toggleCategory = (items: PhenomenonItem[]) => {
    const codes = items.map(i => i.code);
    const allEnabled = codes.every(c => activePhenomena.has(c));
    setActivePhenomena(prev => {
      const next = new Set(prev);
      codes.forEach(c => allEnabled ? next.delete(c) : next.add(c));
      return next;
    });
    setPhenomenaMsg(null);
  };

  const handlePhenomenaSave = async () => {
    setPhenomenaSaving(true);
    setPhenomenaError(null);
    setPhenomenaMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/phenomena'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_phenomena: Array.from(activePhenomena) }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const newSet = new Set(data.active_phenomena as string[]);
      setActivePhenomena(newSet);
      setSavedPhenomena(newSet);
      setUsingPhenomenaOverrides(true);
      setPhenomenaMsg(data.message);
    } catch (err) {
      setPhenomenaError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setPhenomenaSaving(false);
    }
  };

  const handlePhenomenaReset = async () => {
    setPhenomenaSaving(true);
    setPhenomenaError(null);
    setPhenomenaMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/phenomena/reset'), { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPhenomenaMsg(data.message);
      await fetchPhenomena();
    } catch (err) {
      setPhenomenaError(err instanceof Error ? err.message : 'Failed to reset');
    } finally {
      setPhenomenaSaving(false);
    }
  };

  // ── States handlers ───────────────────────────────────────────────────
  const toggleState = (code: string) => {
    setActiveStates(prev => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });
    setStatesMsg(null);
  };

  const handleStatesSave = async () => {
    setStatesSaving(true);
    setStatesError(null);
    setStatesMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/states'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filter_states: Array.from(activeStates) }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const newSet = new Set(data.active_states as string[]);
      setActiveStates(newSet);
      setSavedStates(newSet);
      setUsingStatesOverrides(true);
      setStatesMsg(data.message);
      // Monitored states drive the county list — refresh it.
      fetchCounties();
    } catch (err) {
      setStatesError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setStatesSaving(false);
    }
  };

  const handleStatesReset = async () => {
    setStatesSaving(true);
    setStatesError(null);
    setStatesMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/states/reset'), { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStatesMsg(data.message);
      await fetchStates();
      fetchCounties();
    } catch (err) {
      setStatesError(err instanceof Error ? err.message : 'Failed to reset');
    } finally {
      setStatesSaving(false);
    }
  };

  // ── County handlers ───────────────────────────────────────────────────
  const toggleCounty = (state: string, code: string) => {
    setCountySel(prev => {
      const next = { ...prev, [state]: new Set(prev[state] ?? []) };
      next[state].has(code) ? next[state].delete(code) : next[state].add(code);
      return next;
    });
    setCountiesMsg(null);
  };

  const setAllCountiesForState = (state: string, selectAll: boolean) => {
    setCountySel(prev => {
      const next = { ...prev };
      next[state] = selectAll
        ? new Set((countyStates[state]?.counties ?? []).map(c => c.code))
        : new Set<string>();
      return next;
    });
    setCountiesMsg(null);
  };

  const handleCountiesSave = async () => {
    setCountiesSaving(true);
    setCountiesError(null);
    setCountiesMsg(null);
    try {
      // Empty selection for a state means "all counties" — backend drops empties.
      const payload: Record<string, string[]> = {};
      Object.entries(countySel).forEach(([st, set]) => {
        payload[st] = Array.from(set);
      });
      const res = await fetch(apiUrl('/api/settings/counties'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filter_counties: payload }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setSavedCountySel(Object.fromEntries(Object.entries(countySel).map(([k, v]) => [k, new Set(v)])));
      setUsingCountyOverrides(Object.keys(data.filter_counties || {}).length > 0);
      setCountiesMsg(data.message);
    } catch (err) {
      setCountiesError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setCountiesSaving(false);
    }
  };

  const handleCountiesReset = async () => {
    setCountiesSaving(true);
    setCountiesError(null);
    setCountiesMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/counties/reset'), { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCountiesMsg(data.message);
      await fetchCounties();
    } catch (err) {
      setCountiesError(err instanceof Error ? err.message : 'Failed to reset');
    } finally {
      setCountiesSaving(false);
    }
  };

  // ── General handlers ──────────────────────────────────────────────────
  const updateGeneral = (patch: Partial<GeneralSettings>) => {
    setGeneral(prev => prev ? { ...prev, ...patch } : prev);
    setGeneralMsg(null);
  };

  const handleGeneralSave = async () => {
    if (!general || !savedGeneral) return;
    setGeneralSaving(true);
    setGeneralError(null);
    setGeneralMsg(null);
    try {
      const payload: Record<string, unknown> = {};
      if (general.nexrad_enabled !== savedGeneral.nexrad_enabled) payload.nexrad_enabled = general.nexrad_enabled;
      if (general.llm_enabled !== savedGeneral.llm_enabled) payload.llm_enabled = general.llm_enabled;
      if (general.agent_enabled !== savedGeneral.agent_enabled) payload.agent_enabled = general.agent_enabled;
      if (general.google_chat_enabled !== savedGeneral.google_chat_enabled) payload.google_chat_enabled = general.google_chat_enabled;
      if (nexradSiteInput !== savedGeneral.nexrad_default_site) payload.nexrad_default_site = nexradSiteInput;

      const res = await fetch(apiUrl('/api/settings/general'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data: GeneralSettings & { message: string } = await res.json();
      const updated: GeneralSettings = {
        nexrad_enabled: data.nexrad_enabled,
        nexrad_default_site: data.nexrad_default_site,
        llm_enabled: data.llm_enabled,
        agent_enabled: data.agent_enabled,
        google_chat_enabled: data.google_chat_enabled,
        using_overrides: true,
      };
      setGeneral(updated);
      setSavedGeneral(updated);
      setNexradSiteInput(data.nexrad_default_site);
      setGeneralMsg(data.message);
    } catch (err) {
      setGeneralError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setGeneralSaving(false);
    }
  };

  // ── Ticker handlers ───────────────────────────────────────────────────
  const tickerAlertTypes = [
    { key: 'TO_W', label: 'Tornado Warning', phenomenon: 'TO' },
    { key: 'TO_A', label: 'Tornado Watch', phenomenon: 'TO' },
    { key: 'SV_W', label: 'Severe T-Storm Warning', phenomenon: 'SV' },
    { key: 'SV_A', label: 'Severe T-Storm Watch', phenomenon: 'SV' },
    { key: 'FF_W', label: 'Flash Flood Warning', phenomenon: 'FF' },
    { key: 'FF_A', label: 'Flash Flood Watch', phenomenon: 'FF' },
    { key: 'WS_W', label: 'Winter Storm Warning', phenomenon: 'WS' },
    { key: 'WS_A', label: 'Winter Storm Watch', phenomenon: 'WS' },
    { key: 'BZ_W', label: 'Blizzard Warning', phenomenon: 'BZ' },
    { key: 'IS_W', label: 'Ice Storm Warning', phenomenon: 'IS' },
    { key: 'SQ_W', label: 'Snow Squall Warning', phenomenon: 'SQ' },
    { key: 'SPS_S', label: 'Special Weather Statement', phenomenon: 'SPS' },
    { key: 'WW_Y', label: 'Winter Weather Advisory', phenomenon: 'WW' },
    { key: 'HW_W', label: 'High Wind Warning', phenomenon: 'HW' },
    { key: 'WI_Y', label: 'Wind Advisory', phenomenon: 'WI' },
    { key: 'LE_W', label: 'Lake Effect Snow Warning', phenomenon: 'LE' },
    { key: 'WC_W', label: 'Wind Chill Warning', phenomenon: 'WC' },
    { key: 'EC_W', label: 'Extreme Cold Warning', phenomenon: 'EC' },
    { key: 'EW_W', label: 'Extreme Wind Warning', phenomenon: 'EW' },
  ];

  const toggleTickerType = (key: string) => {
    setTickerExcluded(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
    setTickerMsg(null);
  };

  const handleTickerSave = async () => {
    setTickerSaving(true);
    setTickerMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/ticker'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ excluded_types: Array.from(tickerExcluded) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const newSet = new Set<string>(data.excluded_types);
      setTickerExcluded(newSet);
      setSavedTickerExcluded(newSet);
      setTickerMsg(data.message || 'Saved! Refresh ticker to apply.');
    } catch (err) {
      setTickerMsg(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setTickerSaving(false);
    }
  };

  const handleTickerCancel = () => {
    setTickerExcluded(new Set(savedTickerExcluded));
    setTickerMsg(null);
  };

  // ── Google Chat handlers ──────────────────────────────────────────────
  const toggleGchatType = (key: string) => {
    setGchatSend(prev => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
    setGchatMsg(null);
  };

  const handleGchatSave = async () => {
    setGchatSaving(true);
    setGchatMsg(null);
    try {
      const res = await fetch(apiUrl('/api/settings/google-chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ send_types: Array.from(gchatSend) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const types = (data.types || []) as { key: string; label: string; send: boolean }[];
      const sendSet = new Set<string>(types.filter(t => t.send).map(t => t.key));
      setGchatSend(sendSet);
      setSavedGchatSend(sendSet);
      setGchatMsg(data.message || 'Saved!');
    } catch (err) {
      setGchatMsg(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setGchatSaving(false);
    }
  };

  const handleGchatCancel = () => {
    setGchatSend(new Set(savedGchatSend));
    setGchatMsg(null);
  };

  // ── Toggle component ──────────────────────────────────────────────────
  const Toggle = ({ value, onChange, disabled }: { value: boolean; onChange: (v: boolean) => void; disabled?: boolean }) => (
    <button
      onClick={() => !disabled && onChange(!value)}
      disabled={disabled}
      style={{
        width: '44px', height: '24px', borderRadius: '12px', border: 'none',
        cursor: disabled ? 'not-allowed' : 'pointer', position: 'relative',
        backgroundColor: value ? 'var(--primary-color)' : 'var(--bg-tertiary)',
        transition: 'background-color 0.2s', flexShrink: 0, marginLeft: '16px',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <div style={{
        width: '18px', height: '18px', borderRadius: '50%', backgroundColor: 'white',
        position: 'absolute', top: '3px', left: value ? '23px' : '3px',
        transition: 'left 0.2s', boxShadow: '0 1px 3px rgba(0,0,0,0.3)',
      }} />
    </button>
  );

  const SettingRow = ({ label, description, children }: { label: string; description?: string; children: React.ReactNode }) => (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--border-color)' }}>
      <div>
        <div style={{ fontWeight: 500, color: 'var(--text-primary)', fontSize: '0.9rem' }}>{label}</div>
        {description && <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginTop: '2px' }}>{description}</div>}
      </div>
      {children}
    </div>
  );

  const totalPhenomena = Object.values(categories).reduce((sum, items) => sum + items.length, 0);

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="section active">
      <h2 className="section-title">Settings</h2>

      {/* ── General Settings ─────────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">General</h3>
        <p className="settings-description">Feature toggles and core configuration.</p>

        {generalError && (
          <div className="settings-message settings-error" style={{ marginTop: '8px' }}>
            <i className="fas fa-exclamation-circle"></i> {generalError}
          </div>
        )}
        {generalMsg && (
          <div className="settings-message settings-success" style={{ marginTop: '8px' }}>
            <i className="fas fa-check-circle"></i> {generalMsg}
          </div>
        )}

        {generalLoading ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Loading...</p>
        ) : general && (
          <div className="settings-category-card" style={{ marginTop: '12px' }}>
            <SettingRow label="NEXRAD Radar" description="Level 2 radar processing and display">
              <Toggle value={general.nexrad_enabled} onChange={v => updateGeneral({ nexrad_enabled: v })} />
            </SettingRow>
            <SettingRow label="Default NEXRAD Site" description="4-character ICAO code (e.g. KTWX, KILN)">
              <input
                type="text"
                value={nexradSiteInput}
                onChange={e => { setNexradSiteInput(e.target.value.toUpperCase()); setGeneralMsg(null); }}
                maxLength={4}
                placeholder="KTWX"
                style={{
                  width: '70px', padding: '4px 8px', borderRadius: '4px',
                  border: '1px solid var(--border-color)', backgroundColor: 'var(--bg-secondary)',
                  color: 'var(--text-primary)', fontSize: '0.85rem', textAlign: 'center',
                  fontFamily: 'monospace', textTransform: 'uppercase',
                }}
              />
            </SettingRow>
            <SettingRow label="LLM Assistant" description="AI-powered weather assistant (requires Ollama)">
              <Toggle value={general.llm_enabled} onChange={v => updateGeneral({ llm_enabled: v })} />
            </SettingRow>
            <SettingRow label="AI Agent" description="Tool-calling agent for data queries">
              <Toggle value={general.agent_enabled} onChange={v => updateGeneral({ agent_enabled: v })} />
            </SettingRow>
            <SettingRow label="Google Chat Notifications" description="Send alerts to Google Chat webhook">
              <Toggle value={general.google_chat_enabled} onChange={v => updateGeneral({ google_chat_enabled: v })} />
            </SettingRow>
          </div>
        )}

        {generalHasChanges && (
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button
              className="settings-cancel-btn"
              onClick={() => { setGeneral(savedGeneral); setNexradSiteInput(savedGeneral?.nexrad_default_site ?? ''); setGeneralMsg(null); setGeneralError(null); }}
              disabled={generalSaving}
            >
              Cancel
            </button>
            <button className="settings-save-btn" onClick={handleGeneralSave} disabled={generalSaving}>
              {generalSaving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        )}
      </div>

      {/* ── Audio Notifications ───────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">Audio Notifications</h3>
        <p className="settings-description">
          Play audio chimes when tornado, severe thunderstorm, or flash flood warnings are issued or updated.
          Upload custom MP3/WAV files and adjust volume per event type.
        </p>

        {soundMsg && (
          <div className="settings-message settings-success" style={{ marginTop: '8px' }}>
            <i className="fas fa-check-circle"></i> {soundMsg}
          </div>
        )}

        <div className="settings-category-card" style={{ marginTop: '12px' }}>
          {/* Master enable/disable toggle */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--border-color)', marginBottom: '12px' }}>
            <div>
              <div style={{ fontWeight: 500, color: 'var(--text-primary)', fontSize: '0.9rem' }}>Alert chimes enabled</div>
              <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', marginTop: '2px' }}>
                Master on/off for all alert sounds
              </div>
            </div>
            <Toggle value={chimesEnabled} onChange={() => onToggleChimes?.()} />
          </div>

          {/* Per-event-type rows */}
          {SOUND_EVENT_TYPES.map(({ key, label, description }) => {
            const cfg = externalSoundConfig?.[key];
            const hasCustom = !!cfg?.custom_file;
            const isUploading = uploadingFor === key;
            return (
              <div key={key} style={{ padding: '10px 0', borderBottom: '1px solid var(--border-color)' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '12px' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500, color: 'var(--text-primary)', fontSize: '0.88rem' }}>{label}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: '2px' }}>{description}</div>
                    {hasCustom && (
                      <div style={{ fontSize: '0.72rem', color: 'var(--primary-color)', marginTop: '3px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                        <i className="fas fa-music"></i> {cfg!.custom_file}
                      </div>
                    )}
                    {!hasCustom && (
                      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted, var(--text-secondary))', marginTop: '3px' }}>
                        Built-in synthesized chime
                      </div>
                    )}
                  </div>

                  {/* Volume slider */}
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px', minWidth: '90px' }}>
                    <label style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>
                      Volume {Math.round((soundVolumes[key] ?? 0) * 100)}%
                    </label>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={soundVolumes[key] ?? 0}
                      onChange={e => {
                        setSoundVolumes(prev => ({ ...prev, [key]: parseFloat(e.target.value) }));
                        setSoundMsg(null);
                      }}
                      disabled={!chimesEnabled}
                      style={{ width: '80px', accentColor: 'var(--primary-color)' }}
                    />
                  </div>

                  {/* Action buttons */}
                  <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexShrink: 0 }}>
                    {/* Test */}
                    <button
                      onClick={() => playEventType?.(key)}
                      disabled={!chimesEnabled}
                      title="Test this sound"
                      style={{
                        padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border-color)',
                        backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)',
                        cursor: chimesEnabled ? 'pointer' : 'not-allowed', fontSize: '0.75rem',
                        opacity: chimesEnabled ? 1 : 0.5,
                      }}
                    >
                      <i className="fas fa-play"></i>
                    </button>

                    {/* Upload */}
                    <button
                      onClick={() => fileInputRefs.current[key]?.click()}
                      disabled={isUploading}
                      title="Upload custom sound (MP3, WAV, OGG)"
                      style={{
                        padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border-color)',
                        backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)',
                        cursor: 'pointer', fontSize: '0.75rem',
                      }}
                    >
                      {isUploading ? <i className="fas fa-spinner fa-spin"></i> : <i className="fas fa-upload"></i>}
                    </button>
                    <input
                      type="file"
                      accept=".mp3,.wav,.ogg,.m4a"
                      style={{ display: 'none' }}
                      ref={el => { fileInputRefs.current[key] = el; }}
                      onChange={e => {
                        const file = e.target.files?.[0];
                        if (file) handleSoundUpload(key, file);
                        e.target.value = '';
                      }}
                    />

                    {/* Reset to default */}
                    {hasCustom && (
                      <button
                        onClick={() => handleSoundReset(key)}
                        title="Revert to built-in chime"
                        style={{
                          padding: '4px 8px', borderRadius: '4px', border: '1px solid var(--border-color)',
                          backgroundColor: 'var(--bg-secondary)', color: 'var(--text-secondary)',
                          cursor: 'pointer', fontSize: '0.75rem',
                        }}
                      >
                        <i className="fas fa-undo"></i>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {soundVolumesHaveChanges && (
          <div style={{ marginTop: '10px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button
              className="settings-cancel-btn"
              onClick={() => { setSoundVolumes({ ...savedSoundVolumes }); setSoundMsg(null); }}
              disabled={soundSaving}
            >
              Cancel
            </button>
            <button className="settings-save-btn" onClick={handleSoundVolumesSave} disabled={soundSaving}>
              {soundSaving ? 'Saving...' : 'Save Volumes'}
            </button>
          </div>
        )}
      </div>

      {/* ── Monitored States ──────────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">Monitored States</h3>
        <p className="settings-description">
          Select which US states to monitor for alerts. All alert fetching, SPC outlooks, and data displays respect this filter.
        </p>

        <div className="settings-status-bar">
          <div className="settings-status-left">
            <span className={`settings-status-dot ${usingStatesOverrides ? 'custom' : 'default'}`}></span>
            <span className="settings-status-text">
              {usingStatesOverrides ? 'Using custom states' : 'Using defaults (.env)'}
            </span>
          </div>
          <span className="settings-status-count">{activeStates.size} state{activeStates.size !== 1 ? 's' : ''} selected</span>
        </div>

        {statesError && (
          <div className="settings-message settings-error">
            <i className="fas fa-exclamation-circle"></i> {statesError}
          </div>
        )}
        {statesMsg && (
          <div className="settings-message settings-success">
            <i className="fas fa-check-circle"></i> {statesMsg}
          </div>
        )}

        {statesLoading ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginTop: '12px' }}>Loading...</p>
        ) : (
          <div className="settings-category-card" style={{ marginTop: '12px' }}>
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))',
              gap: '6px',
            }}>
              {allStates.map(state => {
                const isActive = activeStates.has(state.code);
                return (
                  <button
                    key={state.code}
                    onClick={() => toggleState(state.code)}
                    className={`settings-toggle-btn ${isActive ? 'enabled' : ''}`}
                    style={isActive ? {
                      borderLeftColor: 'var(--primary-color)',
                      backgroundColor: 'var(--primary-color-20, rgba(59,130,246,0.12))',
                      color: 'var(--text-primary)',
                    } : { opacity: 0.45 }}
                    title={state.name}
                  >
                    <span className="settings-toggle-code">{state.code}</span>
                    <span className="settings-toggle-name">{state.name}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <div className="settings-action-bar">
          {usingStatesOverrides && (
            <button className="settings-reset-btn" onClick={handleStatesReset} disabled={statesSaving}>
              <i className="fas fa-undo"></i> Reset to Defaults
            </button>
          )}
          {statesHasChanges && (
            <button className="settings-cancel-btn" onClick={() => { setActiveStates(new Set(savedStates)); setStatesMsg(null); setStatesError(null); }} disabled={statesSaving}>
              Cancel
            </button>
          )}
          <button className="settings-save-btn" onClick={handleStatesSave} disabled={!statesHasChanges || statesSaving}>
            {statesSaving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>

      {/* ── County Filter ─────────────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">County Filter</h3>
        <p className="settings-description">
          Narrow alerts to specific counties within your monitored states. Leave a state with
          no counties selected to keep all of its counties (no narrowing).
        </p>

        <div className="settings-status-bar">
          <div className="settings-status-left">
            <span className={`settings-status-dot ${usingCountyOverrides ? 'custom' : 'default'}`}></span>
            <span className="settings-status-text">
              {usingCountyOverrides ? 'Filtering to selected counties' : 'All counties in monitored states'}
            </span>
          </div>
        </div>

        {countiesError && (
          <div className="settings-message settings-error">
            <i className="fas fa-exclamation-circle"></i> {countiesError}
          </div>
        )}
        {countiesMsg && (
          <div className="settings-message settings-success">
            <i className="fas fa-check-circle"></i> {countiesMsg}
          </div>
        )}

        {countiesLoading ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginTop: '12px' }}>Loading...</p>
        ) : Object.keys(countyStates).length === 0 ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginTop: '12px' }}>
            Select monitored states first to choose counties.
          </p>
        ) : (
          Object.entries(countyStates).map(([st, info]) => {
            const sel = countySel[st] ?? new Set<string>();
            const allOn = sel.size === info.counties.length && info.counties.length > 0;
            return (
              <div key={st} className="settings-category-card" style={{ marginTop: '12px' }}>
                <div className="settings-category-header">
                  <div className="settings-category-title">
                    {info.state_name}
                    <span className="settings-category-count">
                      {sel.size === 0 ? 'all' : `${sel.size}/${info.counties.length}`}
                    </span>
                  </div>
                  <button
                    className="settings-select-all-btn"
                    onClick={() => setAllCountiesForState(st, !allOn)}
                  >
                    {allOn ? 'Clear' : 'Select All'}
                  </button>
                </div>
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
                  gap: '6px',
                }}>
                  {info.counties.map(county => {
                    const isOn = sel.has(county.code);
                    return (
                      <button
                        key={county.code}
                        onClick={() => toggleCounty(st, county.code)}
                        className={`settings-toggle-btn ${isOn ? 'enabled' : ''}`}
                        style={isOn ? {
                          borderLeftColor: 'var(--primary-color)',
                          backgroundColor: 'var(--primary-color-20, rgba(59,130,246,0.12))',
                          color: 'var(--text-primary)',
                        } : { opacity: 0.45 }}
                        title={`${county.name} (${county.code})`}
                      >
                        <span className="settings-toggle-name">{county.name}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })
        )}

        <div className="settings-action-bar">
          {usingCountyOverrides && (
            <button className="settings-reset-btn" onClick={handleCountiesReset} disabled={countiesSaving}>
              <i className="fas fa-undo"></i> Clear Filter
            </button>
          )}
          {countiesHasChanges && (
            <button
              className="settings-cancel-btn"
              onClick={() => {
                setCountySel(Object.fromEntries(Object.entries(savedCountySel).map(([k, v]) => [k, new Set(v)])));
                setCountiesMsg(null);
                setCountiesError(null);
              }}
              disabled={countiesSaving}
            >
              Cancel
            </button>
          )}
          <button className="settings-save-btn" onClick={handleCountiesSave} disabled={!countiesHasChanges || countiesSaving}>
            {countiesSaving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>

      {/* ── Ticker Widget Filter ──────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">Ticker Widget Filter</h3>
        <p className="settings-description">
          Choose which alert types appear on the OBS ticker. Disabled types will be hidden. Refresh the ticker after saving.
        </p>

        {tickerMsg && (
          <div className="settings-message settings-success" style={{ marginTop: '8px' }}>
            <i className="fas fa-check-circle"></i> {tickerMsg}
          </div>
        )}

        <div className="settings-category-card" style={{ marginTop: '12px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '6px' }}>
            {tickerAlertTypes.map(type => {
              const isShown = !tickerExcluded.has(type.key);
              const style = getAlertStyle(type.phenomenon);
              const alertColor = ALERT_COLORS[type.phenomenon];
              return (
                <button
                  key={type.key}
                  onClick={() => toggleTickerType(type.key)}
                  className={`settings-toggle-btn ${isShown ? 'enabled' : ''}`}
                  style={isShown && alertColor ? {
                    borderLeftColor: alertColor.backgroundColor,
                    backgroundColor: `${alertColor.backgroundColor}20`,
                    color: 'var(--text-primary)',
                  } : { opacity: 0.4 }}
                  title={isShown ? 'Showing on ticker (click to hide)' : 'Hidden from ticker (click to show)'}
                >
                  <span className="settings-toggle-swatch" style={{ backgroundColor: style.backgroundColor }}></span>
                  <span className="settings-toggle-name" style={{ fontSize: '0.78rem' }}>{type.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {tickerHasChanges && (
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button className="settings-cancel-btn" onClick={handleTickerCancel} disabled={tickerSaving}>Cancel</button>
            <button className="settings-save-btn" onClick={handleTickerSave} disabled={tickerSaving}>
              {tickerSaving ? 'Saving...' : 'Save Ticker Filter'}
            </button>
          </div>
        )}
      </div>

      {/* ── Google Chat Alerts ──────────────────────────────────────── */}
      <div className="settings-section" style={{ marginBottom: '24px' }}>
        <h3 className="settings-subtitle">Google Chat Alerts</h3>
        <p className="settings-description">
          Choose which alert types are sent to Google Chat. Enabled types are pushed
          to the Chat space when a new warning is issued. Applies immediately on save.
        </p>

        {gchatMsg && (
          <div className="settings-message settings-success" style={{ marginTop: '8px' }}>
            <i className="fas fa-check-circle"></i> {gchatMsg}
          </div>
        )}

        <div className="settings-category-card" style={{ marginTop: '12px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '6px' }}>
            {gchatTypes.map(type => {
              const phenomenon = type.key.split('_')[0];
              const isSending = gchatSend.has(type.key);
              const style = getAlertStyle(phenomenon);
              const alertColor = ALERT_COLORS[phenomenon];
              return (
                <button
                  key={type.key}
                  onClick={() => toggleGchatType(type.key)}
                  className={`settings-toggle-btn ${isSending ? 'enabled' : ''}`}
                  style={isSending && alertColor ? {
                    borderLeftColor: alertColor.backgroundColor,
                    backgroundColor: `${alertColor.backgroundColor}20`,
                    color: 'var(--text-primary)',
                  } : { opacity: 0.4 }}
                  title={isSending ? 'Sent to Google Chat (click to stop)' : 'Not sent (click to send)'}
                >
                  <span className="settings-toggle-swatch" style={{ backgroundColor: style.backgroundColor }}></span>
                  <span className="settings-toggle-name" style={{ fontSize: '0.78rem' }}>{type.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {gchatHasChanges && (
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
            <button className="settings-cancel-btn" onClick={handleGchatCancel} disabled={gchatSaving}>Cancel</button>
            <button className="settings-save-btn" onClick={handleGchatSave} disabled={gchatSaving}>
              {gchatSaving ? 'Saving...' : 'Save Google Chat Filter'}
            </button>
          </div>
        )}
      </div>

      {/* ── Monitored Alert Types ─────────────────────────────────── */}
      <div className="settings-section">
        <h3 className="settings-subtitle">Monitored Alert Types</h3>
        <p className="settings-description">
          Select which alert types to monitor. Changes apply to the dashboard and all widgets.
        </p>

        <div className="settings-status-bar">
          <div className="settings-status-left">
            <span className={`settings-status-dot ${usingPhenomenaOverrides ? 'custom' : 'default'}`}></span>
            <span className="settings-status-text">
              {usingPhenomenaOverrides ? 'Using custom settings' : 'Using defaults (.env)'}
            </span>
          </div>
          <span className="settings-status-count">{activePhenomena.size} of {totalPhenomena} enabled</span>
        </div>

        {phenomenaError && (
          <div className="settings-message settings-error">
            <i className="fas fa-exclamation-circle"></i> {phenomenaError}
          </div>
        )}
        {phenomenaMsg && (
          <div className="settings-message settings-success">
            <i className="fas fa-check-circle"></i> {phenomenaMsg}
          </div>
        )}

        {phenomenaLoading ? (
          <p style={{ color: 'var(--text-secondary)' }}>Loading settings...</p>
        ) : (
          <div className="settings-categories">
            {Object.entries(categories).map(([catName, items]) => {
              const enabledCount = items.filter(i => activePhenomena.has(i.code)).length;
              const allEnabled = enabledCount === items.length;
              return (
                <div key={catName} className="settings-category-card">
                  <div className="settings-category-header">
                    <div className="settings-category-title">
                      {catName}
                      <span className="settings-category-count">{enabledCount}/{items.length}</span>
                    </div>
                    <button className="settings-select-all-btn" onClick={() => toggleCategory(items)}>
                      {allEnabled ? 'Deselect All' : 'Select All'}
                    </button>
                  </div>
                  <div className="settings-phenomena-grid">
                    {items.map(item => {
                      const isEnabled = activePhenomena.has(item.code);
                      const style = getAlertStyle(item.code);
                      const alertColor = ALERT_COLORS[item.code];
                      return (
                        <button
                          key={item.code}
                          className={`settings-toggle-btn ${isEnabled ? 'enabled' : ''}`}
                          onClick={() => togglePhenomenon(item.code)}
                          style={isEnabled && alertColor ? {
                            borderLeftColor: alertColor.backgroundColor,
                            backgroundColor: `${alertColor.backgroundColor}20`,
                            color: 'var(--text-primary)',
                          } : undefined}
                          title={`${item.name} (${item.code})`}
                        >
                          <span className="settings-toggle-swatch" style={{ backgroundColor: style.backgroundColor }}></span>
                          <span className="settings-toggle-code">{item.code}</span>
                          <span className="settings-toggle-name">{item.name}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="settings-action-bar">
          {usingPhenomenaOverrides && (
            <button className="settings-reset-btn" onClick={handlePhenomenaReset} disabled={phenomenaSaving}>
              <i className="fas fa-undo"></i> Reset to Defaults
            </button>
          )}
          {phenomenaHasChanges && (
            <button className="settings-cancel-btn" onClick={() => { setActivePhenomena(new Set(savedPhenomena)); setPhenomenaMsg(null); setPhenomenaError(null); }} disabled={phenomenaSaving}>
              Cancel
            </button>
          )}
          <button className="settings-save-btn" onClick={handlePhenomenaSave} disabled={!phenomenaHasChanges || phenomenaSaving}>
            {phenomenaSaving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>
    </div>
  );
};

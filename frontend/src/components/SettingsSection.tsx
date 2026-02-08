import React, { useEffect, useState, useCallback } from 'react';
import { ALERT_COLORS, getAlertStyle } from '../types/alert';

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

export const SettingsSection: React.FC = () => {
  const [categories, setCategories] = useState<Record<string, PhenomenonItem[]>>({});
  const [activePhenomena, setActivePhenomena] = useState<Set<string>>(new Set());
  const [savedPhenomena, setSavedPhenomena] = useState<Set<string>>(new Set());
  const [usingOverrides, setUsingOverrides] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const hasChanges = (() => {
    if (activePhenomena.size !== savedPhenomena.size) return true;
    for (const code of activePhenomena) {
      if (!savedPhenomena.has(code)) return true;
    }
    return false;
  })();

  const fetchSettings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('/api/settings/phenomena');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data: PhenomenaResponse = await response.json();
      setCategories(data.categories);
      const activeSet = new Set(data.active_phenomena);
      setActivePhenomena(activeSet);
      setSavedPhenomena(activeSet);
      setUsingOverrides(data.using_overrides);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  const togglePhenomenon = (code: string) => {
    setActivePhenomena(prev => {
      const next = new Set(prev);
      if (next.has(code)) {
        next.delete(code);
      } else {
        next.add(code);
      }
      return next;
    });
    setSuccessMsg(null);
  };

  const toggleCategory = (categoryItems: PhenomenonItem[]) => {
    const codes = categoryItems.map(i => i.code);
    const allEnabled = codes.every(c => activePhenomena.has(c));
    setActivePhenomena(prev => {
      const next = new Set(prev);
      codes.forEach(c => {
        if (allEnabled) {
          next.delete(c);
        } else {
          next.add(c);
        }
      });
      return next;
    });
    setSuccessMsg(null);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const response = await fetch('/api/settings/phenomena', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_phenomena: Array.from(activePhenomena) }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      const data = await response.json();
      const newSet = new Set(data.active_phenomena as string[]);
      setActivePhenomena(newSet);
      setSavedPhenomena(newSet);
      setUsingOverrides(true);
      setSuccessMsg(data.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setSaving(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const response = await fetch('/api/settings/phenomena/reset', { method: 'POST' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      setSuccessMsg(data.message);
      await fetchSettings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setActivePhenomena(new Set(savedPhenomena));
    setSuccessMsg(null);
    setError(null);
  };

  // Count total available
  const totalPhenomena = Object.values(categories).reduce((sum, items) => sum + items.length, 0);

  if (loading) {
    return (
      <div className="section active">
        <h2 className="section-title">Settings</h2>
        <p style={{ color: 'var(--text-secondary)' }}>Loading settings...</p>
      </div>
    );
  }

  return (
    <div className="section active">
      <h2 className="section-title">Settings</h2>

      <div className="settings-section">
        <h3 className="settings-subtitle">Monitored Alert Types</h3>
        <p className="settings-description">
          Select which alert types to monitor. Changes apply to the dashboard and all widgets.
        </p>

        {/* Status bar */}
        <div className="settings-status-bar">
          <div className="settings-status-left">
            <span className={`settings-status-dot ${usingOverrides ? 'custom' : 'default'}`}></span>
            <span className="settings-status-text">
              {usingOverrides ? 'Using custom settings' : 'Using defaults (.env)'}
            </span>
          </div>
          <span className="settings-status-count">
            {activePhenomena.size} of {totalPhenomena} enabled
          </span>
        </div>

        {/* Messages */}
        {error && (
          <div className="settings-message settings-error">
            <i className="fas fa-exclamation-circle"></i> {error}
          </div>
        )}
        {successMsg && (
          <div className="settings-message settings-success">
            <i className="fas fa-check-circle"></i> {successMsg}
          </div>
        )}

        {/* Category cards */}
        <div className="settings-categories">
          {Object.entries(categories).map(([catName, items]) => {
            const enabledCount = items.filter(i => activePhenomena.has(i.code)).length;
            const allEnabled = enabledCount === items.length;

            return (
              <div key={catName} className="settings-category-card">
                <div className="settings-category-header">
                  <div className="settings-category-title">
                    {catName}
                    <span className="settings-category-count">
                      {enabledCount}/{items.length}
                    </span>
                  </div>
                  <button
                    className="settings-select-all-btn"
                    onClick={() => toggleCategory(items)}
                  >
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
                        <span
                          className="settings-toggle-swatch"
                          style={{ backgroundColor: style.backgroundColor }}
                        ></span>
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

        {/* Action bar */}
        <div className="settings-action-bar">
          {usingOverrides && (
            <button
              className="settings-reset-btn"
              onClick={handleReset}
              disabled={saving}
            >
              <i className="fas fa-undo"></i> Reset to Defaults
            </button>
          )}
          {hasChanges && (
            <button
              className="settings-cancel-btn"
              onClick={handleCancel}
              disabled={saving}
            >
              Cancel
            </button>
          )}
          <button
            className="settings-save-btn"
            onClick={handleSave}
            disabled={!hasChanges || saving}
          >
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>
    </div>
  );
};

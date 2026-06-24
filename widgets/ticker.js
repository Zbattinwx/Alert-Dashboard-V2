/**
 * Alert Dashboard V2 - Non-Sponsored Ticker Widget
 */

class AlertTicker {
    constructor() {
        // Configuration
        this.config = {
            rotationSpeed: 10000,  // ms between alert rotations
            reconnectDelay: 5000,  // ms before reconnecting
            filterStates: null,    // null = use server-side filtering
            theme: 'classic'
        };

        // State
        this.alerts = [];
        this.currentIndex = 0;
        this.ws = null;
        this.rotationTimer = null;
        this.connected = false;
        this.currentScrollDuration = 0;  // Track current scroll animation duration
        this.excludedTypes = new Set();  // e.g. Set(['TO_A', 'SV_A']) to exclude watches

        // DOM elements
        this.container = null;
        this.logoImg = null;
        this.content = null;
        this.titleEl = null;
        this.subtitleEl = null;
        this.locationEl = null;
        this.expiresEl = null;
        this.noAlertsEl = null;
        this.statusIndicator = null;

        // Parse URL parameters
        this.parseUrlParams();

        // Initialize
        this.init();
    }

    parseUrlParams() {
        const params = new URLSearchParams(window.location.search);

        if (params.get('theme')) {
            this.config.theme = params.get('theme');
        }

        if (params.get('states')) {
            this.config.filterStates = params.get('states').split(',').map(s => s.trim().toUpperCase());
        }

        if (params.get('speed')) {
            this.config.rotationSpeed = parseInt(params.get('speed')) || 10000;
        }

        // URL-based exclusions (e.g. ?exclude=TO_A,SV_A to hide watches)
        if (params.get('exclude')) {
            params.get('exclude').split(',').forEach(t => {
                this.excludedTypes.add(t.trim().toUpperCase());
            });
        }

        // Preview/test hook: ?test=emergency injects a synthetic tornado
        // emergency so the escalation styling can be verified on demand.
        this.testEmergency = params.get('test') === 'emergency';
    }

    init() {
        // Wait for DOM
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.setup());
        } else {
            this.setup();
        }
    }

    setup() {
        // Get DOM elements
        this.container = document.getElementById('ticker-container');
        this.logoImg = document.getElementById('ticker-logo-img');
        this.content = document.getElementById('ticker-content');

        // Logo from the active white-label brand (falls back to the default
        // brand logo server-side). Was hardcoded to /tbf_logo.png.
        if (this.logoImg) {
            this.logoImg.src = getBasePath() + '/api/brand/logo';
        }
        this.titleEl = document.getElementById('ticker-title');
        this.subtitleEl = document.getElementById('ticker-subtitle');
        this.locationEl = document.getElementById('ticker-location');
        this.expiresEl = document.getElementById('ticker-expires-time');
        this.noAlertsEl = document.getElementById('ticker-no-alerts');
        this.statusIndicator = document.getElementById('connection-status');

        // Apply theme
        this.applyTheme(this.config.theme);

        // Show the synthetic emergency right away in test mode (before the WS
        // delivers real alerts, which will re-inject it at the front).
        if (this.testEmergency) {
            this.handleBulkAlerts([]);
        }

        // Fetch ticker filter settings from dashboard, then connect
        this.fetchTickerSettings().then(() => {
            this.connect();
            this.startRotation();
        });

        // Update expiration times every minute
        setInterval(() => this.updateExpirationTime(), 60000);

        // Safety net: drop alerts whose expiration time has passed, in case an
        // alert_remove broadcast is ever missed (the ticker has no other way to
        // age out a stale alert since alert_bulk only arrives on connect).
        setInterval(() => this.sweepExpiredAlerts(), 30000);
    }

    sweepExpiredAlerts() {
        if (!this.alerts.length) return;
        const now = Date.now();
        const before = this.alerts.length;
        this.alerts = this.alerts.filter(a => {
            const exp = a.expiration_time || a.expires;
            if (!exp) return true;                 // no expiry → keep
            const t = new Date(exp).getTime();
            return isNaN(t) || t > now;            // keep if unparseable or still valid
        });
        if (this.alerts.length === before) return; // nothing aged out

        if (this.currentIndex >= this.alerts.length) this.currentIndex = 0;
        if (this.alerts.length > 0) this.displayAlert(this.alerts[this.currentIndex]);
        else this.displayNoAlerts();
    }

    async fetchTickerSettings() {
        try {
            const response = await fetch(getApiUrl('/api/settings/ticker'));
            if (response.ok) {
                const data = await response.json();
                if (data.excluded_types && Array.isArray(data.excluded_types)) {
                    data.excluded_types.forEach(t => this.excludedTypes.add(t));
                    console.log('Ticker excluded types:', Array.from(this.excludedTypes));
                }
            }
        } catch (err) {
            console.warn('Could not fetch ticker settings:', err);
        }
    }

    isAlertExcluded(alert) {
        if (this.excludedTypes.size === 0) return false;
        const phenomenon = alert.phenomenon || '';
        const significance = alert.significance || '';
        const key = `${phenomenon}_${significance}`;
        const excluded = this.excludedTypes.has(key);
        console.log(`Ticker filter: ${alert.event_name} key=${key} excluded=${excluded}`);
        return excluded;
    }

    applyTheme(theme) {
        // Remove existing theme classes
        document.body.classList.remove(
            'theme-classic',
            'theme-atmospheric',
            'theme-storm-chaser',
            'theme-meteorologist',
            'theme-winter'
        );

        // Add new theme class
        if (theme && theme !== 'classic') {
            document.body.classList.add(`theme-${theme}`);
        }
    }

    connect() {
        // Tear down any prior socket first so its stale handlers can't fire or
        // schedule their own reconnect — otherwise a flapping connection stacks
        // orphaned sockets, each scheduling another reconnect (a slow storm).
        if (this.ws) {
            this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
            try { this.ws.close(); } catch (e) {}
            this.ws = null;
        }
        if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }

        const wsUrl = getWebSocketUrl();
        console.log('Connecting to WebSocket:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.connected = true;
                this.updateConnectionStatus(true);
            };

            this.ws.onmessage = (event) => {
                this.handleMessage(event.data);
            };

            this.ws.onclose = () => {
                console.log('WebSocket disconnected');
                this.connected = false;
                this.updateConnectionStatus(false);
                this.scheduleReconnect();
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
        } catch (error) {
            console.error('Failed to create WebSocket:', error);
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
        // Guard against multiple in-flight reconnect timers.
        if (this._reconnectTimer) return;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, this.config.reconnectDelay);
    }

    handleMessage(data) {
        try {
            const message = JSON.parse(data);

            switch (message.type) {
                case 'alert_bulk':
                    // Backend sends { type, data: { count, alerts } }
                    const alerts = message.data?.alerts || [];
                    this.handleBulkAlerts(alerts);
                    break;

                case 'alert_new':
                    this.handleNewAlert(message.data);
                    break;

                case 'alert_remove':
                    this.handleAlertExpired(message.data);
                    break;

                case 'alert_update':
                    this.handleAlertUpdate(message.data);
                    break;

                case 'connection_ack':
                    console.log('Connection acknowledged:', message.data?.client_id);
                    break;

                case 'pong':
                case 'system_status':
                    // Ignore these
                    break;

                default:
                    console.log('Unknown message type:', message.type);
            }
        } catch (error) {
            console.error('Error parsing message:', error);
        }
    }

    handleBulkAlerts(alerts) {
        console.log('Received bulk alerts:', alerts.length);

        // Filter alerts if state filter is configured
        if (this.config.filterStates && this.config.filterStates.length > 0) {
            alerts = this.filterAlertsByState(alerts);
        }

        // Filter out excluded alert types
        if (this.excludedTypes.size > 0) {
            alerts = alerts.filter(a => !this.isAlertExcluded(a));
        }

        this.alerts = alerts;
        // Test hook: keep a synthetic emergency pinned to the front so the
        // escalation can be previewed even with no real alerts active.
        if (this.testEmergency) {
            this.alerts.unshift(this._testEmergencyAlert());
        }
        this.currentIndex = 0;

        // Display first alert or no-alerts state
        if (this.alerts.length > 0) {
            this.displayAlert(this.alerts[0]);
        } else {
            this.displayNoAlerts();
        }
    }

    handleNewAlert(alert) {
        console.log('New alert:', alert.event_name);

        // Check state filter
        if (this.config.filterStates && this.config.filterStates.length > 0) {
            if (!this.alertMatchesStateFilter(alert)) {
                return;
            }
        }

        // Check excluded types
        if (this.isAlertExcluded(alert)) {
            return;
        }

        // Add to beginning of alerts list
        this.alerts.unshift(alert);

        // Reset to show new alert
        this.currentIndex = 0;
        this.displayAlert(alert);

        // Restart rotation
        this.startRotation();
    }

    handleAlertExpired(alertData) {
        // The backend's alert_remove payload identifies the alert by product_id
        // (not id/alert_id), so match on product_id first — mirroring
        // handleAlertUpdate. The old id/alert_id-only lookup never matched, so
        // expired/cancelled alerts never dropped off the ticker.
        const alertId = alertData.product_id || alertData.id || alertData.alert_id;
        console.log('Alert expired/removed:', alertId, alertData.reason || '');

        // Remove from alerts list
        const index = this.alerts.findIndex(a =>
            (a.product_id && a.product_id === alertId) ||
            (a.id && a.id === alertId) ||
            (a.alert_id && a.alert_id === alertId)
        );
        if (index !== -1) {
            this.alerts.splice(index, 1);

            // Adjust current index if needed
            if (this.currentIndex >= this.alerts.length) {
                this.currentIndex = 0;
            }

            // Update display
            if (this.alerts.length > 0) {
                this.displayAlert(this.alerts[this.currentIndex]);
            } else {
                this.displayNoAlerts();
            }
        }
    }

    handleAlertUpdate(alert) {
        console.log('Alert updated:', alert.event_name);

        // Skip excluded types
        if (this.isAlertExcluded(alert)) {
            return;
        }

        const alertId = alert.product_id || alert.id || alert.alert_id;
        const index = this.alerts.findIndex(a =>
            (a.product_id && a.product_id === alertId) ||
            (a.id && a.id === alertId) ||
            (a.alert_id && a.alert_id === alertId)
        );

        if (index !== -1) {
            this.alerts[index] = alert;

            // Jump straight to a freshly-upgraded tornado emergency; otherwise
            // only refresh if this is the alert currently on screen.
            if (this.isTornadoEmergency(alert)) {
                this.currentIndex = index;
                this.displayAlert(alert);
            } else if (index === this.currentIndex) {
                this.displayAlert(alert);
            }
        }
    }

    filterAlertsByState(alerts) {
        return alerts.filter(alert => this.alertMatchesStateFilter(alert));
    }

    alertMatchesStateFilter(alert) {
        if (!this.config.filterStates || this.config.filterStates.length === 0) {
            return true;
        }

        // Check ugc_codes for state matches
        const ugcCodes = alert.ugc_codes || alert.affected_areas || [];

        for (const ugc of ugcCodes) {
            if (typeof ugc === 'string' && ugc.length >= 2) {
                const state = ugc.substring(0, 2).toUpperCase();
                if (this.config.filterStates.includes(state)) {
                    return true;
                }
            }
        }

        return false;
    }

    displayAlert(alert) {
        if (!alert) return;

        // Get alert info
        const info = this.getAlertDisplayInfo(alert);
        const emergency = this.isTornadoEmergency(alert);

        // Update container class for styling
        this.container.className = 'ticker-container';
        this.container.classList.add(info.phenomena);
        if (emergency) this.container.classList.add('emergency');

        // Hide no-alerts message
        if (this.noAlertsEl) {
            this.noAlertsEl.style.display = 'none';
        }
        if (this.content) {
            this.content.style.display = 'flex';
        }

        // Fade out
        this.content.classList.add('fade-out');

        setTimeout(() => {
            // Update title — a tornado emergency overrides the product name.
            this.titleEl.textContent = emergency ? 'TORNADO EMERGENCY' : info.name;

            // Update subtitle with key details (wind gusts, hail size, etc.)
            const keyDetails = this.extractKeyDetails(alert);
            if (this.subtitleEl) {
                this.subtitleEl.textContent = keyDetails || '';
            }

            // Update location with scroll if needed
            const location = this.formatLocation(alert);
            this.locationEl.textContent = location;
            this.setupLocationScroll();

            // Update expiration (V2 API uses expiration_time)
            const expiresValue = alert.expiration_time || alert.expires;
            this.expiresEl.textContent = this.formatExpirationTime(expiresValue);
            this.expiresEl.dataset.expires = expiresValue;

            // Fade in
            this.content.classList.remove('fade-out');
            this.content.classList.add('fade-in');

            setTimeout(() => {
                this.content.classList.remove('fade-in');
            }, 300);
        }, 300);
    }

    displayNoAlerts() {
        this.container.className = 'ticker-container no-alerts';

        if (this.content) {
            this.content.style.display = 'none';
        }

        if (this.noAlertsEl) {
            this.noAlertsEl.style.display = 'flex';
        }
    }

    setupLocationScroll() {
        // Remove existing scroll animation
        this.locationEl.classList.remove('scrolling');
        this.locationEl.style.animation = '';
        this.currentScrollDuration = 0;

        // Check if text overflows container
        const containerWidth = this.locationEl.parentElement.offsetWidth - 40; // padding
        const textWidth = this.locationEl.scrollWidth;

        if (textWidth > containerWidth) {
            // Calculate animation duration based on text length
            const duration = textWidth / 60; // ~60px per second
            this.currentScrollDuration = duration;

            this.locationEl.style.animation = `scrollText ${duration}s linear infinite`;
            this.locationEl.classList.add('scrolling');
        }

        // Schedule next rotation based on scroll duration or default time
        this.scheduleNextRotation();
    }

    scheduleNextRotation() {
        // Clear any existing timer
        if (this.rotationTimer) {
            clearTimeout(this.rotationTimer);
        }

        if (this.alerts.length <= 1) return;

        // If scrolling, wait for at least one full scroll cycle + 2 seconds buffer
        // Otherwise use default rotation speed
        let delay;
        if (this.currentScrollDuration > 0) {
            // Wait for scroll to complete + small buffer, minimum of rotation speed
            delay = Math.max((this.currentScrollDuration + 2) * 1000, this.config.rotationSpeed);
        } else {
            delay = this.config.rotationSpeed;
        }

        this.rotationTimer = setTimeout(() => {
            this.rotateToNext();
        }, delay);
    }

    startRotation() {
        // Initial rotation scheduling is handled by setupLocationScroll
        // This is called when alerts are first loaded or a new alert arrives
        // The actual scheduling happens after displayAlert -> setupLocationScroll
    }

    // A tornado emergency is the most severe tornado warning — flagged by the
    // backend's structured `tornado_emergency` field. Falls back to a
    // CATASTROPHIC tornado damage tag or the literal "TORNADO EMERGENCY" text
    // for legacy/preview alerts. Mirrors the React app's detection.
    isTornadoEmergency(alert) {
        if (!alert) return false;
        const threat = alert.threat || {};
        if (threat.tornado_emergency === true) return true;
        if (threat.tornado_damage_threat === 'CATASTROPHIC') return true;
        const desc = (alert.description || alert.raw_text || '').toUpperCase();
        return desc.includes('TORNADO EMERGENCY');
    }

    findEmergencyIndex() {
        return this.alerts.findIndex(a => this.isTornadoEmergency(a));
    }

    _testEmergencyAlert() {
        return {
            product_id: 'TEST-TOR-E',
            phenomenon: 'TO', significance: 'W',
            event_name: 'Tornado Warning',
            description: 'TORNADO EMERGENCY for the test area. Take cover now!',
            threat: { tornado_damage_threat: 'CATASTROPHIC' },
            display_locations: 'TEST — Clark County, OH',
            area_description: 'TEST — Clark County, OH',
            expiration_time: new Date(Date.now() + 30 * 60000).toISOString(),
        };
    }

    rotateToNext() {
        // During a tornado emergency, pin the ticker to it (take-over behavior)
        // instead of rotating past it to lesser alerts.
        const emIdx = this.findEmergencyIndex();
        if (emIdx !== -1) {
            if (this.currentIndex !== emIdx) {
                this.currentIndex = emIdx;
                this.displayAlert(this.alerts[emIdx]);
            } else {
                this.scheduleNextRotation(); // already pinned — keep the heartbeat alive
            }
            return;
        }

        if (this.alerts.length <= 1) return;

        this.currentIndex = (this.currentIndex + 1) % this.alerts.length;
        this.displayAlert(this.alerts[this.currentIndex]);
        // scheduleNextRotation is called by setupLocationScroll after display
    }

    updateExpirationTime() {
        if (!this.expiresEl || !this.expiresEl.dataset.expires) return;

        const newTime = this.formatExpirationTime(this.expiresEl.dataset.expires);
        this.expiresEl.textContent = newTime;
    }

    updateConnectionStatus(connected) {
        if (this.statusIndicator) {
            this.statusIndicator.className = 'connection-status ' + (connected ? 'connected' : 'disconnected');
        }
    }

    getAlertDisplayInfo(alert) {
        // V2 API uses 'phenomenon' (singular), fallback to 'phenomena' for compatibility
        const phenomena = alert.phenomenon || alert.phenomena || alert.event_code || '';

        // Use the shared ALERT_TYPE_INFO map (widget-common.js) — same names /
        // shortNames, but a module const instead of a 60-entry object rebuilt on
        // every displayAlert call.
        const map = (typeof ALERT_TYPE_INFO !== 'undefined') ? ALERT_TYPE_INFO : {};
        const info = map[phenomena] || map['default'] || { shortName: 'WX', name: 'Weather Alert' };

        return {
            shortName: info.shortName,
            name: alert.event_name || info.name,
            phenomena: phenomena || 'default'
        };
    }

    formatLocation(alert) {
        // Use display_locations if available (from V2 API)
        // Can be either a string or an array
        if (alert.display_locations) {
            if (typeof alert.display_locations === 'string') {
                return alert.display_locations;
            }
            if (Array.isArray(alert.display_locations) && alert.display_locations.length > 0) {
                return alert.display_locations.join(', ');
            }
        }

        // Fall back to affected_areas
        if (alert.affected_areas && Array.isArray(alert.affected_areas) && alert.affected_areas.length > 0) {
            return alert.affected_areas.join(', ');
        }

        // Fall back to area_description
        if (alert.area_description) {
            return alert.area_description;
        }

        return 'Unknown Location';
    }

    extractKeyDetails(alert) {
        const threat = alert.threat || {};
        const parts = [];

        // High-priority threat tags — a Tornado Emergency leads everything.
        if (this.isTornadoEmergency(alert)) {
            parts.push('TORNADO EMERGENCY');
        } else if (threat.tornado_detection === 'OBSERVED') {
            parts.push('OBSERVED TORNADO');
        }
        if (threat.tornado_damage_threat === 'CATASTROPHIC') {
            parts.push('CATASTROPHIC');
        } else if (threat.tornado_damage_threat === 'CONSIDERABLE') {
            parts.push('CONSIDERABLE');
        }
        if (threat.thunderstorm_damage_threat === 'DESTRUCTIVE') {
            parts.push('DESTRUCTIVE');
        } else if (threat.thunderstorm_damage_threat === 'CONSIDERABLE') {
            parts.push('CONSIDERABLE');
        }
        if (threat.flash_flood_damage_threat === 'CATASTROPHIC') {
            parts.push('CATASTROPHIC FLOODING');
        } else if (threat.flash_flood_damage_threat === 'CONSIDERABLE') {
            parts.push('CONSIDERABLE FLOODING');
        }

        // Wind
        const hasSustained = threat.sustained_wind_min_mph || threat.sustained_wind_max_mph;
        const hasGusts = threat.max_wind_gust_mph;
        if (hasSustained && hasGusts) {
            const min = threat.sustained_wind_min_mph;
            const max = threat.sustained_wind_max_mph;
            const sustainedStr = min !== max ? `${min}-${max}` : `${max}`;
            parts.push(`Wind: ${sustainedStr} mph | Gusts: ${hasGusts} mph`);
        } else if (hasSustained) {
            const min = threat.sustained_wind_min_mph;
            const max = threat.sustained_wind_max_mph;
            const sustainedStr = min !== max ? `${min}-${max}` : `${max}`;
            parts.push(`Wind: ${sustainedStr} mph`);
        } else if (hasGusts) {
            parts.push(`Gusts: ${hasGusts} mph`);
        }

        // Hail
        if (threat.max_hail_size_inches) {
            parts.push(`Hail: ${threat.max_hail_size_inches}"`);
        }

        // Snow
        if (threat.snow_amount_max_inches) {
            const snowMin = threat.snow_amount_min_inches || 0;
            const snowMax = threat.snow_amount_max_inches;
            parts.push(snowMin !== snowMax ? `Snow: ${snowMin}-${snowMax}"` : `Snow: ${snowMax}"`);
        }

        // Ice
        if (threat.ice_accumulation_inches) {
            parts.push(`Ice: ${threat.ice_accumulation_inches}"`);
        }

        if (parts.length > 0) {
            return parts.join('  ·  ');
        }

        // Fallback: parse description text
        const desc = alert.description || '';
        const whatMatch = desc.match(/\*\s*WHAT\.\.\.([^*]+)/i);
        if (whatMatch) {
            let what = whatMatch[1].trim().replace(/\s+/g, ' ').replace(/occurring\.?$/i, '').trim();
            if (what.length > 100) what = what.substring(0, 100) + '...';
            return what;
        }

        return null;
    }

    formatExpirationTime(expiresStr) {
        if (!expiresStr) return '--';

        const expires = new Date(expiresStr);
        const now = new Date();
        const diffMs = expires - now;

        if (diffMs < 0) return 'Expired';

        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMins / 60);
        const remainingMins = diffMins % 60;

        if (diffHours > 0) {
            return `${diffHours}h ${remainingMins}m`;
        }
        return `${diffMins}m`;
    }
}

// Initialize ticker
const ticker = new AlertTicker();

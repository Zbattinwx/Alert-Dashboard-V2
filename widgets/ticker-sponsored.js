/**
 * Alert Dashboard V2 - Sponsored Ticker Widget
 */

class SponsoredAlertTicker {
    constructor() {
        // Configuration
        this.config = {
            rotationSpeed: 10000,       // ms between alert rotations
            sponsorRotationSpeed: 15000, // ms between sponsor rotations
            reconnectDelay: 5000,        // ms before reconnecting
            filterStates: null,          // null = use server-side filtering
            theme: 'classic',
            sponsors: []                 // Array of sponsor objects
        };

        // State
        this.alerts = [];
        this.currentAlertIndex = 0;
        this.currentSponsorIndex = 0;
        this.ws = null;
        this.alertRotationTimer = null;
        this.sponsorRotationTimer = null;
        this.connected = false;
        this.currentScrollDuration = 0;  // Track current scroll animation duration
        this.excludedTypes = new Set();  // e.g. Set(['TO_A', 'SV_A']) to exclude watches

        // DOM elements
        this.container = null;
        this.sponsorContainer = null;
        this.badge = null;
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

        if (params.get('sponsor_speed')) {
            this.config.sponsorRotationSpeed = parseInt(params.get('sponsor_speed')) || 15000;
        }

        // URL-based exclusions (e.g. ?exclude=TO_A,SV_A to hide watches)
        if (params.get('exclude')) {
            params.get('exclude').split(',').forEach(t => {
                this.excludedTypes.add(t.trim().toUpperCase());
            });
        }

        // Parse sponsors from URL or use defaults
        if (params.get('sponsors')) {
            try {
                this.config.sponsors = JSON.parse(decodeURIComponent(params.get('sponsors')));
            } catch (e) {
                console.error('Failed to parse sponsors from URL');
            }
        }
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
        this.sponsorContainer = document.getElementById('sponsor-container');
        this.badge = document.getElementById('ticker-badge');
        this.content = document.getElementById('ticker-content');
        this.titleEl = document.getElementById('ticker-title');
        this.subtitleEl = document.getElementById('ticker-subtitle');
        this.locationEl = document.getElementById('ticker-location');
        this.expiresEl = document.getElementById('ticker-expires-time');
        this.noAlertsEl = document.getElementById('ticker-no-alerts');
        this.statusIndicator = document.getElementById('connection-status');

        // Apply theme
        this.applyTheme(this.config.theme);

        // Load sponsors
        this.loadSponsors();

        // Fetch ticker filter settings from dashboard, then connect
        this.fetchTickerSettings().then(() => {
            this.connect();
            this.startAlertRotation();
            this.startSponsorRotation();
        });

        // Update expiration times every minute
        setInterval(() => this.updateExpirationTime(), 60000);
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
        return this.excludedTypes.has(key);
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

    async loadSponsors() {
        // If sponsors were provided via URL, use those
        if (this.config.sponsors.length > 0) {
            this.displaySponsor(this.config.sponsors[0]);
            return;
        }

        // Try to load from API
        try {
            const response = await fetch(getApiUrl('/api/widgets/sponsors'));
            if (response.ok) {
                const data = await response.json();
                this.config.sponsors = data.sponsors || [];
            }
        } catch (error) {
            console.log('No sponsors API available, using defaults');
        }

        // Use default sponsor if none configured
        if (this.config.sponsors.length === 0) {
            this.config.sponsors = [
                {
                    type: 'text',
                    content: 'Weather Dashboard',
                    subtext: 'Powered by NWS'
                }
            ];
        }

        // Display first sponsor
        this.displaySponsor(this.config.sponsors[0]);
    }

    displaySponsor(sponsor) {
        if (!sponsor || !this.sponsorContainer) return;

        this.sponsorContainer.classList.add('fade-out');

        setTimeout(() => {
            this.sponsorContainer.innerHTML = '';

            if (sponsor.type === 'image' && sponsor.logo) {
                const img = document.createElement('img');
                img.src = sponsor.logo;
                img.alt = sponsor.name || 'Sponsor';
                img.className = 'sponsor-logo';
                this.sponsorContainer.appendChild(img);
            } else {
                const textEl = document.createElement('div');
                textEl.className = 'sponsor-text';
                textEl.innerHTML = sponsor.content || sponsor.name || 'Sponsor';
                if (sponsor.subtext) {
                    textEl.innerHTML += `<br><small>${sponsor.subtext}</small>`;
                }
                this.sponsorContainer.appendChild(textEl);
            }

            this.sponsorContainer.classList.remove('fade-out');
            this.sponsorContainer.classList.add('fade-in');

            setTimeout(() => {
                this.sponsorContainer.classList.remove('fade-in');
            }, 300);
        }, 300);
    }

    startSponsorRotation() {
        if (this.config.sponsors.length <= 1) return;

        if (this.sponsorRotationTimer) {
            clearInterval(this.sponsorRotationTimer);
        }

        this.sponsorRotationTimer = setInterval(() => {
            this.currentSponsorIndex = (this.currentSponsorIndex + 1) % this.config.sponsors.length;
            this.displaySponsor(this.config.sponsors[this.currentSponsorIndex]);
        }, this.config.sponsorRotationSpeed);
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
        this.currentAlertIndex = 0;

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
        this.currentAlertIndex = 0;
        this.displayAlert(alert);

        // Restart rotation
        this.startAlertRotation();
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
            if (this.currentAlertIndex >= this.alerts.length) {
                this.currentAlertIndex = 0;
            }

            // Update display
            if (this.alerts.length > 0) {
                this.displayAlert(this.alerts[this.currentAlertIndex]);
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

            // Update display if this is the current alert
            if (index === this.currentAlertIndex) {
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

        // Update container class for styling
        this.container.className = 'ticker-container';
        this.container.classList.add(info.phenomena);

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
            // Update badge
            this.badge.textContent = info.shortName;

            // Update title
            this.titleEl.textContent = info.name;

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

        this.badge.textContent = 'WX';
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
        this.scheduleNextAlertRotation();
    }

    scheduleNextAlertRotation() {
        // Clear any existing timer
        if (this.alertRotationTimer) {
            clearTimeout(this.alertRotationTimer);
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

        this.alertRotationTimer = setTimeout(() => {
            this.rotateToNextAlert();
        }, delay);
    }

    startAlertRotation() {
        // Initial rotation scheduling is handled by setupLocationScroll
        // This is called when alerts are first loaded or a new alert arrives
        // The actual scheduling happens after displayAlert -> setupLocationScroll
    }

    rotateToNextAlert() {
        if (this.alerts.length <= 1) return;

        this.currentAlertIndex = (this.currentAlertIndex + 1) % this.alerts.length;
        this.displayAlert(this.alerts[this.currentAlertIndex]);
        // scheduleNextAlertRotation is called by setupLocationScroll after display
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
        // Extract key details from description for display
        const desc = alert.description || '';

        // Try to find the WHAT section which contains key info
        const whatMatch = desc.match(/\*\s*WHAT\.\.\.([^*]+)/i);
        if (whatMatch) {
            let what = whatMatch[1].trim();
            // Clean up and shorten
            what = what.replace(/\s+/g, ' ').replace(/occurring\.?$/i, '').trim();
            // Limit length
            if (what.length > 100) {
                what = what.substring(0, 100) + '...';
            }
            return what;
        }

        // Try to extract wind info patterns
        const windMatch = desc.match(/winds?\s+(\d+\s*to\s*\d+\s*mph)[^.]*gusts?\s+(?:from\s+)?(\d+\s*to\s*\d+\s*mph|\d+\s*mph)/i);
        if (windMatch) {
            return `Winds ${windMatch[1]}, gusts ${windMatch[2]}`;
        }

        // Try to extract hail size
        const hailMatch = desc.match(/hail\s+(?:up\s+to\s+)?([^,.]+(?:inch|diameter)[^,.]*)/i);
        if (hailMatch) {
            return `Hail: ${hailMatch[1].trim()}`;
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

    // Add sponsor via API
    addSponsor(sponsor) {
        this.config.sponsors.push(sponsor);
        if (this.config.sponsors.length === 1) {
            this.displaySponsor(sponsor);
            this.startSponsorRotation();
        }
    }

    // Remove sponsor
    removeSponsor(index) {
        if (index >= 0 && index < this.config.sponsors.length) {
            this.config.sponsors.splice(index, 1);
            if (this.currentSponsorIndex >= this.config.sponsors.length) {
                this.currentSponsorIndex = 0;
            }
            if (this.config.sponsors.length > 0) {
                this.displaySponsor(this.config.sponsors[this.currentSponsorIndex]);
            }
        }
    }
}

// Initialize ticker
const ticker = new SponsoredAlertTicker();

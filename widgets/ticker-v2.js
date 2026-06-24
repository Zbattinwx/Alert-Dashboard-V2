/**
 * Alert Dashboard V2 - Ticker V2 (two-bar broadcast ticker)
 *
 * Ported from the ONW dashboard's favored ticker-v2 layout:
 *   - TOP BAR    : alert name + impact tags, background colored by alert type
 *   - BOTTOM BAR : brand logo | scrolling affected locations | optional sponsor
 *
 * Uses the shared V2 widget helpers (widget-common.js) for base-path-aware
 * WebSocket/API URLs and the ALERT_TYPE_INFO map, and consumes the V2
 * alert_bulk / alert_new / alert_update / alert_remove WebSocket protocol
 * (the same one ticker.js / ticker-sponsored.js use).
 */

// FontAwesome icon per phenomenon for the top bar. Degrades silently to the
// default triangle if the FontAwesome CDN is unavailable on the stream box.
const TICKER_V2_ICONS = {
    TO: 'fa-tornado',
    SV: 'fa-cloud-bolt',
    FF: 'fa-water',
    FL: 'fa-water',
    WS: 'fa-snowflake',
    BZ: 'fa-wind',
    IS: 'fa-icicles',
    LE: 'fa-snowflake',
    WW: 'fa-snowflake',
    WC: 'fa-temperature-low',
    CW: 'fa-temperature-low',
    EC: 'fa-temperature-low',
    HW: 'fa-wind',
    WI: 'fa-wind',
    SQ: 'fa-snowflake',
    SS: 'fa-water',
    SPS: 'fa-triangle-exclamation',
    default: 'fa-triangle-exclamation',
};

class TickerV2 {
    constructor() {
        this.config = {
            rotationSpeed: 10000,         // ms hold when locations fit (no scroll)
            reconnectDelay: 5000,         // ms before reconnecting
            sponsorRotationSpeed: 15000,  // ms between sponsor rotations
            filterStates: null,           // null = no client-side state filter
            sponsors: [],
        };

        this.alerts = [];
        this.currentIndex = 0;
        this.currentSponsorIndex = 0;
        this.ws = null;
        this.alertTimer = null;
        this.sponsorTimer = null;
        this._reconnectTimer = null;
        this.excludedTypes = new Set();  // e.g. Set(['TO_A','SV_A']) to hide watches
        this.noAlertsMessage = 'No Active Alerts';
        this.testEmergency = false;

        this.parseUrlParams();
        this.init();
    }

    parseUrlParams() {
        const p = new URLSearchParams(window.location.search);
        if (p.get('states')) {
            this.config.filterStates = p.get('states').split(',').map(s => s.trim().toUpperCase());
        }
        if (p.get('speed')) {
            this.config.rotationSpeed = parseInt(p.get('speed')) || 10000;
        }
        if (p.get('exclude')) {
            p.get('exclude').split(',').forEach(t => this.excludedTypes.add(t.trim().toUpperCase()));
        }
        if (p.get('message')) {
            this.noAlertsMessage = p.get('message');
        }
        if (p.get('sponsor_speed')) {
            this.config.sponsorRotationSpeed = parseInt(p.get('sponsor_speed')) || 15000;
        }
        if (p.get('sponsors')) {
            try {
                this.config.sponsors = JSON.parse(decodeURIComponent(p.get('sponsors')));
            } catch (e) {
                console.error('Ticker V2: failed to parse sponsors from URL');
            }
        }
        this.testEmergency = p.get('test') === 'emergency';
    }

    init() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.setup());
        } else {
            this.setup();
        }
    }

    setup() {
        this.alertBar = document.getElementById('alert-bar');
        this.infoBar = document.getElementById('info-bar');
        this.iconEl = document.querySelector('.alert-bar-icon i');
        this.titleEl = document.getElementById('alert-bar-title');
        this.impactsEl = document.getElementById('alert-bar-impacts');
        this.expiresEl = document.getElementById('alert-bar-expires-time');
        this.logoImg = document.getElementById('info-bar-logo-img');
        this.locationsContainer = document.getElementById('info-bar-locations');
        this.locationsSpan = this.locationsContainer.querySelector('span');
        this.sponsorContainer = document.querySelector('.info-bar-sponsor');

        // Brand logo from the active white-label brand (same source as ticker.js).
        if (this.logoImg) {
            this.logoImg.src = getBasePath() + '/api/brand/logo';
        }

        this.loadSponsors();

        // Preview the emergency takeover immediately in test mode.
        if (this.testEmergency) {
            this.handleBulkAlerts([]);
        }

        // Fetch server-side ticker exclusions, then connect.
        this.fetchTickerSettings().then(() => this.connect());

        // Keep the relative "Expires" countdown fresh.
        setInterval(() => this.updateExpirationTime(), 60000);
        // Safety net: age out alerts whose expiry passed if an alert_remove was missed.
        setInterval(() => this.sweepExpiredAlerts(), 30000);
    }

    async fetchTickerSettings() {
        try {
            const response = await fetch(getApiUrl('/api/settings/ticker'));
            if (response.ok) {
                const data = await response.json();
                if (Array.isArray(data.excluded_types)) {
                    data.excluded_types.forEach(t => this.excludedTypes.add(t));
                    console.log('Ticker V2 excluded types:', Array.from(this.excludedTypes));
                }
            }
        } catch (err) {
            console.warn('Ticker V2: could not fetch ticker settings:', err);
        }
    }

    // ----------------------------------------------------------------- sponsors
    async loadSponsors() {
        // Priority: ?sponsors= URL param (inline JSON) -> sponsors.json dropped
        // in the widgets folder next to this file (hot-swappable, no rebuild)
        // -> nothing (the sponsor slot stays hidden so locations get full width).
        if (this.config.sponsors.length === 0) {
            try {
                // Relative URL -> resolves to /<base>/widgets/sponsors.json.
                const response = await fetch('sponsors.json', { cache: 'no-store' });
                if (response.ok) {
                    const data = await response.json();
                    this.config.sponsors = Array.isArray(data) ? data : (data.sponsors || []);
                }
            } catch (e) {
                // No sponsors.json -> sponsor slot stays hidden.
            }
        }

        if (!Array.isArray(this.config.sponsors) || this.config.sponsors.length === 0) {
            this.sponsorContainer.classList.add('empty');
            return;
        }

        this.sponsorContainer.classList.remove('empty');
        this.displaySponsor(this.config.sponsors[0]);
        this.startSponsorRotation();
    }

    displaySponsor(sponsor) {
        if (!sponsor || !this.sponsorContainer) return;
        const textEl = this.sponsorContainer.querySelector('.sponsor-text');
        const logoEl = this.sponsorContainer.querySelector('.sponsor-logo');

        logoEl.classList.add('fading');
        textEl.classList.add('fading');

        setTimeout(() => {
            // Accept both ONW ({logo_url,text}) and V2 ({type,logo,content,name}) shapes.
            const logo = sponsor.logo_url || sponsor.logo || (sponsor.type === 'image' ? sponsor.url : null);
            const text = sponsor.text || sponsor.content || sponsor.name;

            if (logo) {
                logoEl.src = logo;
                logoEl.style.display = 'block';
                textEl.style.display = 'none';
            } else if (text) {
                textEl.textContent = text;
                textEl.style.display = 'block';
                logoEl.style.display = 'none';
            }

            logoEl.classList.remove('fading');
            textEl.classList.remove('fading');
        }, 200);
    }

    startSponsorRotation() {
        if (this.config.sponsors.length <= 1) return;
        if (this.sponsorTimer) clearInterval(this.sponsorTimer);
        this.sponsorTimer = setInterval(() => {
            this.currentSponsorIndex = (this.currentSponsorIndex + 1) % this.config.sponsors.length;
            this.displaySponsor(this.config.sponsors[this.currentSponsorIndex]);
        }, this.config.sponsorRotationSpeed);
    }

    // --------------------------------------------------------------- websocket
    connect() {
        // Tear down any prior socket so stale handlers can't stack reconnects.
        if (this.ws) {
            this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
            try { this.ws.close(); } catch (e) { /* ignore */ }
            this.ws = null;
        }
        if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }

        const wsUrl = getWebSocketUrl();
        console.log('Ticker V2 connecting to WebSocket:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);
            this.ws.onopen = () => console.log('Ticker V2 WebSocket connected');
            this.ws.onmessage = (event) => this.handleMessage(event.data);
            this.ws.onclose = () => {
                console.log('Ticker V2 WebSocket disconnected');
                this.scheduleReconnect();
            };
            this.ws.onerror = (error) => console.error('Ticker V2 WebSocket error:', error);
        } catch (error) {
            console.error('Ticker V2 failed to create WebSocket:', error);
            this.scheduleReconnect();
        }
    }

    scheduleReconnect() {
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
                    this.handleBulkAlerts(message.data?.alerts || []);
                    break;
                case 'alert_new':
                    this.handleNewAlert(message.data);
                    break;
                case 'alert_update':
                    this.handleAlertUpdate(message.data);
                    break;
                case 'alert_remove':
                    this.handleAlertExpired(message.data);
                    break;
                case 'connection_ack':
                case 'pong':
                case 'system_status':
                    break;
                default:
                    break;
            }
        } catch (error) {
            console.error('Ticker V2 error parsing message:', error);
        }
    }

    // ---------------------------------------------------------- alert handlers
    handleBulkAlerts(alerts) {
        this.alerts = this.filterAlerts(alerts);
        if (this.testEmergency) {
            this.alerts.unshift(this._testEmergencyAlert());
        }
        this.currentIndex = 0;
        this.restart();
    }

    handleNewAlert(alert) {
        if (this.isAlertFiltered(alert)) return;
        this.alerts.unshift(alert);
        this.currentIndex = 0;
        this.restart();
    }

    handleAlertUpdate(alert) {
        if (this.isAlertFiltered(alert)) return;
        const id = this.alertId(alert);
        const index = this.alerts.findIndex(a => this.alertId(a) === id);
        if (index !== -1) {
            this.alerts[index] = alert;
            // Jump straight to a freshly-upgraded tornado emergency.
            if (this.isTornadoEmergency(alert)) {
                this.currentIndex = index;
                this.restart();
            } else if (index === this.currentIndex) {
                this.renderTopBar(alert);
            }
        } else {
            // An update for an alert we don't have yet — treat it as new.
            this.alerts.push(alert);
        }
    }

    handleAlertExpired(alertData) {
        const id = alertData.product_id || alertData.id || alertData.alert_id;
        const index = this.alerts.findIndex(a => this.alertId(a) === id);
        if (index === -1) return;
        this.alerts.splice(index, 1);
        if (this.currentIndex >= this.alerts.length) this.currentIndex = 0;
        this.restart();
    }

    sweepExpiredAlerts() {
        if (!this.alerts.length) return;
        const now = Date.now();
        const before = this.alerts.length;
        this.alerts = this.alerts.filter(a => {
            const exp = a.expiration_time || a.expires;
            if (!exp) return true;
            const t = new Date(exp).getTime();
            return isNaN(t) || t > now;
        });
        if (this.alerts.length === before) return;
        if (this.currentIndex >= this.alerts.length) this.currentIndex = 0;
        this.restart();
    }

    // ------------------------------------------------------------- filtering
    filterAlerts(alerts) {
        return alerts.filter(a => !this.isAlertFiltered(a));
    }

    isAlertFiltered(alert) {
        // State filter (optional, via ?states=)
        if (this.config.filterStates && this.config.filterStates.length > 0) {
            const ugc = alert.ugc_codes || alert.affected_areas || [];
            const match = ugc.some(z => typeof z === 'string' && z.length >= 2 &&
                this.config.filterStates.includes(z.substring(0, 2).toUpperCase()));
            if (!match) return true;
        }
        // Excluded type filter (server settings + ?exclude=)
        if (this.excludedTypes.size > 0) {
            const key = `${alert.phenomenon || ''}_${alert.significance || ''}`;
            if (this.excludedTypes.has(key)) return true;
        }
        return false;
    }

    alertId(alert) {
        return alert.product_id || alert.id || alert.alert_id;
    }

    // --------------------------------------------------------------- rotation
    restart() {
        clearTimeout(this.alertTimer);
        this.rotate();
    }

    rotate() {
        clearTimeout(this.alertTimer);

        if (this.alerts.length === 0) {
            this.displayNoAlerts();
            return;
        }

        this.infoBar.classList.remove('no-alerts-active');

        // Tornado-emergency takeover: pin the ticker to it instead of rotating past.
        const emIdx = this.alerts.findIndex(a => this.isTornadoEmergency(a));
        const pinned = emIdx !== -1;
        if (pinned) this.currentIndex = emIdx;
        if (this.currentIndex >= this.alerts.length) this.currentIndex = 0;

        const alert = this.alerts[this.currentIndex];
        this.renderTopBar(alert);

        // Bottom bar: affected locations, scrolling if they overflow.
        this.locationsContainer.classList.remove('scrolling');
        this.locationsSpan.style.animationDuration = '';
        this.locationsSpan.textContent = this.formatLocation(alert);

        setTimeout(() => {
            const containerWidth = this.locationsContainer.clientWidth;
            const textWidth = this.locationsSpan.scrollWidth;

            if (textWidth > containerWidth) {
                // Scroll the locations, advance only after one full pass completes.
                const duration = textWidth / 60; // ~60px/s
                this.locationsSpan.style.animationDuration = `${duration}s`;
                this.locationsContainer.classList.add('scrolling');
                this.locationsSpan.addEventListener('animationend', () => {
                    if (!pinned) this.currentIndex++;
                    this.alertTimer = setTimeout(() => this.rotate(), 800);
                }, { once: true });
            } else {
                // Fits — hold for the configured rotation speed, then advance.
                if (!pinned) this.currentIndex++;
                this.alertTimer = setTimeout(() => this.rotate(), this.config.rotationSpeed);
            }
        }, 100);
    }

    renderTopBar(alert) {
        const emergency = this.isTornadoEmergency(alert);
        const phenomenon = alert.phenomenon || alert.phenomena || '';
        const significance = alert.significance || '';

        // Color class: "TO-W" with a bare-phenomenon fallback ("TO").
        const colorClass = significance ? `${phenomenon}-${significance}` : phenomenon;
        this.alertBar.className = `alert-bar ${phenomenon} ${colorClass}`.trim();
        if (emergency) this.alertBar.classList.add('emergency');

        // Icon
        if (this.iconEl) {
            const icon = TICKER_V2_ICONS[phenomenon] || TICKER_V2_ICONS.default;
            this.iconEl.className = `fas ${icon}`;
        }

        // Title — a tornado emergency overrides the product name.
        this.titleEl.textContent = emergency ? 'TORNADO EMERGENCY' : this.alertName(alert);

        // Impact tags
        this.impactsEl.innerHTML = this.buildImpactsHTML(alert);

        // Expiration
        const exp = alert.expiration_time || alert.expires;
        this.expiresEl.textContent = formatExpirationTime(exp);
        this.expiresEl.dataset.expires = exp || '';
    }

    displayNoAlerts() {
        this.alertBar.className = 'alert-bar hidden';
        this.infoBar.classList.add('no-alerts-active');
        this.locationsContainer.classList.remove('scrolling');
        this.locationsSpan.style.animationDuration = '';
        this.locationsSpan.textContent = this.noAlertsMessage;

        // Scroll the "no alerts" message only if it overflows.
        setTimeout(() => {
            const containerWidth = this.locationsContainer.clientWidth;
            const textWidth = this.locationsSpan.scrollWidth;
            if (textWidth > containerWidth) {
                this.locationsContainer.classList.add('scrolling');
                this.locationsSpan.style.animationDuration = `${textWidth / 75}s`;
            }
        }, 100);
    }

    // --------------------------------------------------------------- helpers
    alertName(alert) {
        const phenomenon = alert.phenomenon || alert.phenomena || '';
        const map = (typeof ALERT_TYPE_INFO !== 'undefined') ? ALERT_TYPE_INFO : {};
        const info = map[phenomenon] || map.default || { name: 'Weather Alert' };
        return alert.event_name || info.name;
    }

    // A tornado emergency is flagged by the backend's structured threat fields,
    // with text fallbacks for legacy/preview alerts. Mirrors ticker.js.
    isTornadoEmergency(alert) {
        if (!alert) return false;
        const threat = alert.threat || {};
        if (threat.tornado_emergency === true) return true;
        if (threat.tornado_damage_threat === 'CATASTROPHIC') return true;
        const desc = (alert.description || alert.raw_text || '').toUpperCase();
        return desc.includes('TORNADO EMERGENCY');
    }

    // Map V2's nested threat model to ONW-style impact tags. Only shows info that
    // isn't already in the alert title.
    buildImpactsHTML(alert) {
        const t = alert.threat || {};
        const emergency = this.isTornadoEmergency(alert);
        let html = '';

        // Tornado detection
        const det = (t.tornado_detection || '').toUpperCase();
        if (!emergency && det === 'OBSERVED') {
            html += `<span class="impact-tag impact-observed">OBSERVED</span>`;
        } else if (!emergency && det && det !== 'POSSIBLE' && det !== 'RADAR INDICATED') {
            html += `<span class="impact-tag">${det}</span>`;
        } else if (!emergency && det === 'POSSIBLE') {
            html += `<span class="impact-tag impact-possible">TORNADO POSSIBLE</span>`;
        }

        // Tornado damage threat (PDS / considerable)
        const td = (t.tornado_damage_threat || '').toUpperCase();
        if (!emergency && td === 'CATASTROPHIC') {
            html += `<span class="impact-tag impact-catastrophic">PDS</span>`;
        } else if (td === 'CONSIDERABLE') {
            html += `<span class="impact-tag impact-considerable">CONSIDERABLE</span>`;
        }

        // Thunderstorm damage threat
        const sd = (t.thunderstorm_damage_threat || '').toUpperCase();
        if (sd === 'DESTRUCTIVE') {
            html += `<span class="impact-tag impact-destructive">DESTRUCTIVE</span>`;
        } else if (sd === 'CONSIDERABLE') {
            html += `<span class="impact-tag impact-considerable">CONSIDERABLE</span>`;
        }

        // Flash flood damage threat
        const fd = (t.flash_flood_damage_threat || '').toUpperCase();
        if (fd === 'CATASTROPHIC') {
            html += `<span class="impact-tag impact-flood impact-catastrophic">FLOOD: CATASTROPHIC</span>`;
        } else if (fd === 'CONSIDERABLE') {
            html += `<span class="impact-tag impact-flood impact-considerable">FLOOD: CONSIDERABLE</span>`;
        }

        // Measured values
        const gust = t.max_wind_gust_mph;
        const sustained = t.sustained_wind_max_mph;
        if (gust) {
            html += `<span class="impact-tag">${gust} MPH WIND</span>`;
        } else if (sustained) {
            html += `<span class="impact-tag">${sustained} MPH SUSTAINED</span>`;
        }
        if (t.max_hail_size_inches) {
            html += `<span class="impact-tag">${t.max_hail_size_inches}" HAIL</span>`;
        }
        if (t.snow_amount_max_inches) {
            const mn = t.snow_amount_min_inches || 0;
            const mx = t.snow_amount_max_inches;
            const range = mn !== mx ? `${mn}-${mx}` : `${mx}`;
            html += `<span class="impact-tag"><i class="fas fa-snowflake"></i> ${range}"</span>`;
        }
        if (t.ice_accumulation_inches) {
            html += `<span class="impact-tag">${t.ice_accumulation_inches}" ICE</span>`;
        }

        return html;
    }

    formatLocation(alert) {
        if (alert.display_locations) {
            if (typeof alert.display_locations === 'string') return alert.display_locations;
            if (Array.isArray(alert.display_locations) && alert.display_locations.length > 0) {
                return alert.display_locations.join(', ');
            }
        }
        if (Array.isArray(alert.affected_areas) && alert.affected_areas.length > 0) {
            return alert.affected_areas.join(', ');
        }
        return alert.area_description || 'Unknown Location';
    }

    updateExpirationTime() {
        if (!this.expiresEl || !this.expiresEl.dataset.expires) return;
        this.expiresEl.textContent = formatExpirationTime(this.expiresEl.dataset.expires);
    }

    _testEmergencyAlert() {
        return {
            product_id: 'TEST-TOR-E',
            phenomenon: 'TO', significance: 'W',
            event_name: 'Tornado Warning',
            description: 'TORNADO EMERGENCY for the test area. Take cover now!',
            threat: { tornado_damage_threat: 'CATASTROPHIC', tornado_detection: 'OBSERVED' },
            display_locations: 'TEST - Clark County, OH',
            expiration_time: new Date(Date.now() + 30 * 60000).toISOString(),
        };
    }
}

// Initialize ticker
const tickerV2 = new TickerV2();

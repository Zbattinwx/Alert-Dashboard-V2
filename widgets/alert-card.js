/**
 * Alert Dashboard V2 - OBS Alert Card Widget
 * Pops up a card when a new alert is issued, auto-dismisses after a configurable duration.
 */

// Extended color map with text color and border color (matches frontend ALERT_COLORS)
const CARD_COLORS = {
    'TO':  { bg: '#ff0000', border: '#cc0000', text: '#ffffff' },
    'TOR': { bg: '#ff0000', border: '#cc0000', text: '#ffffff' },
    'TOA': { bg: '#ffff00', border: '#cccc00', text: '#000000' },
    'SV':  { bg: '#ffa500', border: '#cc8400', text: '#000000' },
    'SVR': { bg: '#ffa500', border: '#cc8400', text: '#000000' },
    'SVS': { bg: '#00ffff', border: '#00cccc', text: '#000000' },
    'SVA': { bg: '#db7093', border: '#b05a76', text: '#000000' },
    'FF':  { bg: '#8b0000', border: '#6f0000', text: '#ffffff' },
    'FFW': { bg: '#8b0000', border: '#6f0000', text: '#ffffff' },
    'FFS': { bg: '#8b0000', border: '#6f0000', text: '#ffffff' },
    'FFA': { bg: '#2e8b57', border: '#246f46', text: '#ffffff' },
    'FL':  { bg: '#00ff00', border: '#00cc00', text: '#000000' },
    'FLW': { bg: '#00ff00', border: '#00cc00', text: '#000000' },
    'FLA': { bg: '#2e8b57', border: '#246f46', text: '#ffffff' },
    'WS':  { bg: '#ff69b4', border: '#cc5490', text: '#000000' },
    'WSW': { bg: '#ff69b4', border: '#cc5490', text: '#000000' },
    'WSA': { bg: '#4682b4', border: '#3a6a90', text: '#ffffff' },
    'BZ':  { bg: '#ff4500', border: '#cc3700', text: '#ffffff' },
    'IS':  { bg: '#8b008b', border: '#6f006f', text: '#ffffff' },
    'LE':  { bg: '#008b8b', border: '#006f6f', text: '#ffffff' },
    'WW':  { bg: '#7b68ee', border: '#6253be', text: '#ffffff' },
    'WC':  { bg: '#b0c4de', border: '#8d9db2', text: '#000000' },
    'WCA': { bg: '#5f9ea0', border: '#4c7e80', text: '#ffffff' },
    'CW':  { bg: '#afeeee', border: '#8ccece', text: '#000000' },
    'HW':  { bg: '#daa520', border: '#ae8419', text: '#000000' },
    'WI':  { bg: '#d2b48c', border: '#a8906f', text: '#000000' },
    'EH':  { bg: '#c71585', border: '#9f1169', text: '#ffffff' },
    'EHA': { bg: '#800000', border: '#660000', text: '#ffffff' },
    'HT':  { bg: '#ff7f50', border: '#cc6540', text: '#000000' },
    'FW':  { bg: '#ff1493', border: '#cc1076', text: '#ffffff' },
    'FWA': { bg: '#ffdead', border: '#ccb28a', text: '#000000' },
    'SPS': { bg: '#ffe4b5', border: '#ccb691', text: '#000000' },
    'EW':  { bg: '#ff8c00', border: '#cc7000', text: '#000000' },
    'DS':  { bg: '#ffe4c4', border: '#ccb69c', text: '#000000' },
    'SQ':  { bg: '#C71585', border: '#9f1169', text: '#ffffff' },
    'FG':  { bg: '#708090', border: '#596673', text: '#ffffff' },
    'FZ':  { bg: '#483d8b', border: '#3a3170', text: '#ffffff' },
    'FZA': { bg: '#00ced1', border: '#00a5a7', text: '#000000' },
    'FR':  { bg: '#64ffda', border: '#50ccae', text: '#000000' },
    'EC':  { bg: '#0000ff', border: '#0000cc', text: '#ffffff' },
    'ZR':  { bg: '#da70d6', border: '#ae5aab', text: '#000000' },
    'SM':  { bg: '#f0e68c', border: '#c0b870', text: '#000000' },
    'ZF':  { bg: '#008080', border: '#006666', text: '#ffffff' },
    'AS':  { bg: '#808080', border: '#666666', text: '#ffffff' },
    'HZ':  { bg: '#9400d3', border: '#7600a9', text: '#ffffff' },
    'SS':  { bg: '#b524f7', border: '#911dc5', text: '#ffffff' },
    'TS':  { bg: '#fd6347', border: '#ca4f39', text: '#ffffff' },
    'SU':  { bg: '#228b22', border: '#1b6f1b', text: '#ffffff' },
    'CF':  { bg: '#228b22', border: '#1b6f1b', text: '#ffffff' },
    'DEFAULT': { bg: '#444444', border: '#333333', text: '#ffffff' }
};

function getCardColors(phenomenon, significance) {
    if (significance === 'A') {
        const watchKey = phenomenon + 'A';
        if (CARD_COLORS[watchKey]) return CARD_COLORS[watchKey];
    }
    return CARD_COLORS[phenomenon] || CARD_COLORS['DEFAULT'];
}

class AlertCardWidget {
    constructor() {
        this.config = {
            duration: 30000,
            reconnectDelay: 5000,
            filterStates: null
        };

        this.alerts = [];       // all known alerts (for priority comparison)
        this.ws = null;
        this.connected = false;
        this.dismissTimer = null;
        this.currentAlert = null;
        this.isShowing = false;
        this.initialBulkReceived = false;

        // DOM
        this.container = null;
        this.cardEl = null;
        this.eventNameEl = null;
        this.issuedTimeEl = null;
        this.headerEl = null;
        this.locationsEl = null;
        this.expirationEl = null;
        this.senderEl = null;
        this.tagsEl = null;
        this.impactsEl = null;

        this.parseUrlParams();
        this.init();
    }

    parseUrlParams() {
        const params = new URLSearchParams(window.location.search);

        if (params.get('states')) {
            this.config.filterStates = params.get('states').split(',').map(s => s.trim().toUpperCase());
        }
        if (params.get('duration')) {
            this.config.duration = parseInt(params.get('duration')) || 30000;
        }
    }

    init() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.setup());
        } else {
            this.setup();
        }
    }

    setup() {
        this.container = document.getElementById('alert-card-container');
        this.cardEl = document.getElementById('alert-card');
        this.headerEl = document.getElementById('card-header');
        this.eventNameEl = document.getElementById('card-event-name');
        this.issuedTimeEl = document.getElementById('card-issued-time');
        this.locationsEl = document.getElementById('card-locations');
        this.expirationEl = document.getElementById('card-expiration');
        this.senderEl = document.getElementById('card-sender');
        this.tagsEl = document.getElementById('card-tags');
        this.impactsEl = document.getElementById('card-impacts');

        this.connect();
    }

    // ── WebSocket ──

    connect() {
        const wsUrl = getWebSocketUrl();
        console.log('Alert Card: connecting to', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('Alert Card: WebSocket connected');
                this.connected = true;
            };

            this.ws.onmessage = (event) => this.handleMessage(event.data);

            this.ws.onclose = () => {
                console.log('Alert Card: WebSocket disconnected');
                this.connected = false;
                setTimeout(() => this.connect(), this.config.reconnectDelay);
            };

            this.ws.onerror = (err) => {
                console.error('Alert Card: WebSocket error', err);
            };
        } catch (err) {
            console.error('Alert Card: failed to connect', err);
            setTimeout(() => this.connect(), this.config.reconnectDelay);
        }
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
                    this.handleAlertRemove(message.data);
                    break;
                case 'connection_ack':
                    console.log('Alert Card: connected as', message.data?.client_id);
                    break;
                default:
                    break;
            }
        } catch (err) {
            console.error('Alert Card: message parse error', err);
        }
    }

    // ── Alert Handlers ──

    handleBulkAlerts(alerts) {
        if (this.config.filterStates) {
            alerts = alerts.filter(a => alertMatchesStateFilter(a, this.config.filterStates));
        }
        this.alerts = alerts;
        this.initialBulkReceived = true;
        // Don't popup on initial bulk — these are existing alerts, not new ones
        console.log('Alert Card: bulk received,', alerts.length, 'alerts');
    }

    handleNewAlert(alert) {
        if (this.config.filterStates && !alertMatchesStateFilter(alert, this.config.filterStates)) {
            return;
        }

        // Add to tracked alerts
        this.alerts.push(alert);

        // Show it if nothing is showing, or if this is higher priority
        if (!this.isShowing || (this.currentAlert && (alert.priority || 99) < (this.currentAlert.priority || 99))) {
            this.showAlert(alert);
        }
    }

    handleAlertUpdate(alert) {
        // Update in tracked list
        const idx = this.alerts.findIndex(a => a.product_id === alert.product_id);
        if (idx >= 0) {
            this.alerts[idx] = alert;
        }

        // If currently displayed, refresh the card content
        if (this.currentAlert && this.currentAlert.product_id === alert.product_id) {
            this.populateCard(alert);
            this.currentAlert = alert;
        }
    }

    handleAlertRemove(data) {
        // Remove from tracked list
        this.alerts = this.alerts.filter(a => a.product_id !== data.product_id);

        // If the removed alert is currently displayed, dismiss it
        if (this.currentAlert && this.currentAlert.product_id === data.product_id) {
            this.dismissAlert();
        }
    }

    // ── Display ──

    showAlert(alert) {
        // Clear any pending dismiss
        if (this.dismissTimer) {
            clearTimeout(this.dismissTimer);
            this.dismissTimer = null;
        }

        this.currentAlert = alert;
        this.populateCard(alert);

        // Slide in
        this.container.classList.remove('hidden', 'slide-out');
        this.isShowing = true;

        // Schedule auto-dismiss
        this.dismissTimer = setTimeout(() => this.dismissAlert(), this.config.duration);
    }

    dismissAlert() {
        if (this.dismissTimer) {
            clearTimeout(this.dismissTimer);
            this.dismissTimer = null;
        }

        // Slide out
        this.container.classList.add('slide-out');
        this.isShowing = false;
        this.currentAlert = null;

        // After animation, fully hide
        setTimeout(() => {
            if (!this.isShowing) {
                this.container.classList.add('hidden');
                this.container.classList.remove('slide-out');
            }
        }, 450);
    }

    populateCard(alert) {
        const colors = getCardColors(alert.phenomenon || '', alert.significance || '');

        // Header styling
        this.headerEl.style.backgroundColor = colors.bg;
        this.headerEl.style.color = colors.text;
        this.cardEl.style.borderLeftColor = colors.border;

        // Event name & time
        this.eventNameEl.textContent = alert.event_name || 'Weather Alert';
        this.issuedTimeEl.textContent = this.formatTime(alert.issued_time);

        // Locations
        this.locationsEl.textContent = this.truncateLocations(
            alert.display_locations || (alert.affected_areas || []).join(', ')
        );

        // Expiration
        if (alert.expiration_time) {
            this.expirationEl.textContent = 'Until ' + this.formatExpiration(alert.expiration_time);
        } else {
            this.expirationEl.textContent = '';
        }

        // Sender
        this.senderEl.textContent = alert.sender_name || '';

        // Tags (tornado detection, thunderstorm damage, flash flood detection)
        this.tagsEl.innerHTML = '';
        const threat = alert.threat || {};

        if (threat.tornado_detection) {
            const tag = document.createElement('span');
            tag.className = 'tag tag-tornado';
            tag.textContent = 'TORNADO ' + threat.tornado_detection;
            this.tagsEl.appendChild(tag);
        }

        if (threat.thunderstorm_damage_threat) {
            const tag = document.createElement('span');
            tag.className = 'tag ' + (threat.thunderstorm_damage_threat === 'DESTRUCTIVE' ? 'tag-tstorm-destructive' : 'tag-tstorm-considerable');
            tag.textContent = 'THUNDERSTORM DAMAGE: ' + threat.thunderstorm_damage_threat;
            this.tagsEl.appendChild(tag);
        }

        if (threat.flash_flood_detection) {
            const tag = document.createElement('span');
            tag.className = 'tag tag-flood';
            tag.textContent = 'FLASH FLOOD ' + threat.flash_flood_detection;
            this.tagsEl.appendChild(tag);
        }

        if (threat.flash_flood_damage_threat) {
            const tag = document.createElement('span');
            tag.className = 'tag tag-flood';
            tag.textContent = 'FLASH FLOOD DAMAGE: ' + threat.flash_flood_damage_threat;
            this.tagsEl.appendChild(tag);
        }

        // Impact badges (wind, hail, snow, ice)
        this.impactsEl.innerHTML = '';
        const impacts = this.buildImpacts(threat);
        for (const text of impacts) {
            const badge = document.createElement('span');
            badge.className = 'impact-badge';
            badge.textContent = text;
            this.impactsEl.appendChild(badge);
        }
    }

    buildImpacts(threat) {
        const impacts = [];

        const hasSustained = threat.sustained_wind_min_mph || threat.sustained_wind_max_mph;
        const hasGusts = threat.max_wind_gust_mph;

        if (hasSustained && hasGusts) {
            const min = threat.sustained_wind_min_mph;
            const max = threat.sustained_wind_max_mph;
            const sustained = min !== max ? `${min}-${max}` : `${max}`;
            impacts.push(`Wind: ${sustained} mph | Gusts: ${threat.max_wind_gust_mph} mph`);
        } else if (hasSustained) {
            const min = threat.sustained_wind_min_mph;
            const max = threat.sustained_wind_max_mph;
            const sustained = min !== max ? `${min}-${max}` : `${max}`;
            impacts.push(`Wind: ${sustained} mph`);
        } else if (hasGusts) {
            impacts.push(`Gusts: ${threat.max_wind_gust_mph} mph`);
        }

        if (threat.max_hail_size_inches) {
            impacts.push(`Hail: ${threat.max_hail_size_inches}"`);
        }

        if (threat.snow_amount_max_inches) {
            const min = threat.snow_amount_min_inches || 0;
            const max = threat.snow_amount_max_inches;
            impacts.push(min !== max ? `Snow: ${min}-${max}"` : `Snow: ${max}"`);
        }

        if (threat.ice_accumulation_inches) {
            impacts.push(`Ice: ${threat.ice_accumulation_inches}"`);
        }

        return impacts;
    }

    // ── Formatting helpers ──

    formatTime(isoString) {
        if (!isoString) return '';
        const d = new Date(isoString);
        return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
    }

    formatExpiration(isoString) {
        if (!isoString) return '';
        const d = new Date(isoString);
        return d.toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true
        });
    }

    truncateLocations(locations, maxLength = 120) {
        if (!locations || locations.length <= maxLength) return locations;
        const parts = locations.split(/[;,]/).map(s => s.trim()).filter(Boolean);
        if (parts.length <= 3) return locations;
        const shown = parts.slice(0, 3).join('; ');
        const remaining = parts.length - 3;
        return `${shown}; and ${remaining} more`;
    }
}

// Boot
new AlertCardWidget();

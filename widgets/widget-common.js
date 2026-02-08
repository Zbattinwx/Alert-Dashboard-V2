/**
 * Common utilities for Alert Dashboard V2 widgets
 */

// State code to full name mapping
const STATE_MAP = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
    'PR': 'Puerto Rico', 'VI': 'Virgin Islands', 'GU': 'Guam'
};

// Alert type information - colors and display names
const ALERT_TYPE_INFO = {
    // Tornado
    'TO': { color: '#FF0000', name: 'Tornado Warning', shortName: 'TOR' },
    'TOR': { color: '#FF0000', name: 'Tornado Warning', shortName: 'TOR' },
    'TOA': { color: '#FFFF00', name: 'Tornado Watch', shortName: 'TOA' },

    // Severe Thunderstorm
    'SV': { color: '#FFA500', name: 'Severe Thunderstorm Warning', shortName: 'SVR' },
    'SVR': { color: '#FFA500', name: 'Severe Thunderstorm Warning', shortName: 'SVR' },
    'SVS': { color: '#FFA500', name: 'Severe Weather Statement', shortName: 'SVS' },
    'SVA': { color: '#DB7093', name: 'Severe Thunderstorm Watch', shortName: 'SVA' },

    // Flash Flood
    'FF': { color: '#8B0000', name: 'Flash Flood Warning', shortName: 'FFW' },
    'FFW': { color: '#8B0000', name: 'Flash Flood Warning', shortName: 'FFW' },
    'FFS': { color: '#8B0000', name: 'Flash Flood Statement', shortName: 'FFS' },
    'FFA': { color: '#2E8B57', name: 'Flash Flood Watch', shortName: 'FFA' },

    // Flood
    'FL': { color: '#00FF00', name: 'Flood Warning', shortName: 'FLW' },
    'FLW': { color: '#00FF00', name: 'Flood Warning', shortName: 'FLW' },
    'FLS': { color: '#00FF00', name: 'Flood Statement', shortName: 'FLS' },
    'FLA': { color: '#2E8B57', name: 'Flood Watch', shortName: 'FLA' },

    // Winter Storm
    'WS': { color: '#FF69B4', name: 'Winter Storm Warning', shortName: 'WSW' },
    'WSW': { color: '#FF69B4', name: 'Winter Storm Warning', shortName: 'WSW' },
    'WSA': { color: '#4682B4', name: 'Winter Storm Watch', shortName: 'WSA' },

    // Blizzard
    'BZ': { color: '#FF4500', name: 'Blizzard Warning', shortName: 'BZW' },
    'BZW': { color: '#FF4500', name: 'Blizzard Warning', shortName: 'BZW' },

    // Ice Storm
    'IS': { color: '#8B008B', name: 'Ice Storm Warning', shortName: 'ISW' },
    'ISW': { color: '#8B008B', name: 'Ice Storm Warning', shortName: 'ISW' },

    // Lake Effect Snow
    'LE': { color: '#00CED1', name: 'Lake Effect Snow Warning', shortName: 'LEW' },
    'LEW': { color: '#00CED1', name: 'Lake Effect Snow Warning', shortName: 'LEW' },

    // Winter Weather
    'WW': { color: '#7B68EE', name: 'Winter Weather Advisory', shortName: 'WWA' },
    'WWA': { color: '#7B68EE', name: 'Winter Weather Advisory', shortName: 'WWA' },

    // Wind Chill
    'WC': { color: '#B0C4DE', name: 'Wind Chill Warning', shortName: 'WCW' },
    'WCW': { color: '#B0C4DE', name: 'Wind Chill Warning', shortName: 'WCW' },

    // Cold Weather
    'CW': { color: '#AFEEEE', name: 'Cold Weather Advisory', shortName: 'CWA' },

    // High Wind
    'HW': { color: '#DAA520', name: 'High Wind Warning', shortName: 'HWW' },
    'HWW': { color: '#DAA520', name: 'High Wind Warning', shortName: 'HWW' },

    // Special Weather Statement
    'SPS': { color: '#FFE4B5', name: 'Special Weather Statement', shortName: 'SPS' },

    // Squall
    'SQ': { color: '#C71585', name: 'Snow Squall Warning', shortName: 'SQW' },
    'SQW': { color: '#C71585', name: 'Snow Squall Warning', shortName: 'SQW' },

    // Wind Advisory
    'WI': { color: '#D2B48C', name: 'Wind Advisory', shortName: 'WIA' },

    // Extreme Wind
    'EW': { color: '#FF8C00', name: 'Extreme Wind Warning', shortName: 'EWW' },

    // Excessive Heat
    'EH': { color: '#C71585', name: 'Excessive Heat Warning', shortName: 'EHW' },
    'EHA': { color: '#800000', name: 'Excessive Heat Watch', shortName: 'EHA' },

    // Heat Advisory
    'HT': { color: '#FF7F50', name: 'Heat Advisory', shortName: 'HTA' },

    // Fire Weather / Red Flag
    'FW': { color: '#FF1493', name: 'Red Flag Warning', shortName: 'RFW' },
    'FWA': { color: '#FFDEAD', name: 'Fire Weather Watch', shortName: 'FWA' },

    // Dense Fog
    'FG': { color: '#708090', name: 'Dense Fog Advisory', shortName: 'FGA' },

    // Dense Smoke
    'SM': { color: '#F0E68C', name: 'Dense Smoke Advisory', shortName: 'SMA' },

    // Freezing Fog
    'ZF': { color: '#008080', name: 'Freezing Fog Advisory', shortName: 'ZFA' },

    // Dust Storm
    'DS': { color: '#FFE4C4', name: 'Dust Storm Warning', shortName: 'DSW' },

    // Freeze
    'FZ': { color: '#483D8B', name: 'Freeze Warning', shortName: 'FZW' },
    'FZA': { color: '#00CED1', name: 'Freeze Watch', shortName: 'FZA' },

    // Frost
    'FR': { color: '#6495ED', name: 'Frost Advisory', shortName: 'FRA' },

    // Hard Freeze
    'HZ': { color: '#9400D3', name: 'Hard Freeze Warning', shortName: 'HZW' },

    // Extreme Cold
    'EC': { color: '#0000FF', name: 'Extreme Cold Warning', shortName: 'ECW' },

    // Freezing Rain
    'ZR': { color: '#DA70D6', name: 'Freezing Rain Advisory', shortName: 'ZRA' },

    // Air Stagnation
    'AS': { color: '#808080', name: 'Air Stagnation Advisory', shortName: 'ASA' },

    // Tropical Storm
    'TR': { color: '#B22222', name: 'Tropical Storm Warning', shortName: 'TRW' },

    // Hurricane
    'HU': { color: '#DC143C', name: 'Hurricane Warning', shortName: 'HUW' },

    // Storm Surge
    'SS': { color: '#B524F7', name: 'Storm Surge Warning', shortName: 'SSW' },

    // Coastal Flood
    'CF': { color: '#228B22', name: 'Coastal Flood Warning', shortName: 'CFW' },

    // High Surf
    'SU': { color: '#228B22', name: 'High Surf Warning', shortName: 'SUW' },

    // Tsunami
    'TS': { color: '#FD6347', name: 'Tsunami Warning', shortName: 'TSW' },

    // Default
    'default': { color: '#808080', name: 'Weather Alert', shortName: 'WX' }
};

/**
 * Get alert display information based on phenomenon code
 */
function getAlertDisplayInfo(alert) {
    const phenomena = alert.phenomena || alert.event_code || '';
    const info = ALERT_TYPE_INFO[phenomena] || ALERT_TYPE_INFO['default'];

    return {
        color: info.color,
        name: alert.event_name || info.name,
        shortName: info.shortName,
        phenomena: phenomena
    };
}

/**
 * Format expiration time as relative string
 */
function formatExpirationTime(expiresStr) {
    if (!expiresStr) return '';

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

/**
 * Format location string from alert data
 */
function formatLocation(alert) {
    // Use display_locations if available (from V2 API)
    if (alert.display_locations && alert.display_locations.length > 0) {
        return alert.display_locations.join(', ');
    }

    // Fall back to affected_areas
    if (alert.affected_areas && alert.affected_areas.length > 0) {
        return alert.affected_areas.join(', ');
    }

    // Fall back to area_description
    if (alert.area_description) {
        return alert.area_description;
    }

    return 'Unknown Location';
}

/**
 * Get state code from UGC zone code
 */
function getStateFromUGC(ugc) {
    if (!ugc || ugc.length < 2) return null;
    return ugc.substring(0, 2);
}

/**
 * Check if alert matches state filter
 */
function alertMatchesStateFilter(alert, filterStates) {
    if (!filterStates || filterStates.length === 0) return true;

    // Check affected_areas/ugc_codes for state matches
    const ugcCodes = alert.ugc_codes || alert.affected_areas || [];

    for (const ugc of ugcCodes) {
        const state = getStateFromUGC(ugc);
        if (state && filterStates.includes(state)) {
            return true;
        }
    }

    return false;
}

/**
 * Get base path prefix (e.g. "/v2" when served at /v2/widgets/)
 */
function getBasePath() {
    const path = window.location.pathname;
    const widgetIdx = path.indexOf('/widgets/');
    if (widgetIdx > 0) return path.substring(0, widgetIdx);
    return '';
}

/**
 * Get WebSocket URL for V2 backend
 */
function getWebSocketUrl() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${getBasePath()}/ws`;
}

/**
 * Get API URL with base path prefix
 */
function getApiUrl(path) {
    return `${getBasePath()}${path}`;
}

/**
 * Parse URL parameters
 */
function getUrlParams() {
    const params = new URLSearchParams(window.location.search);
    return {
        theme: params.get('theme') || 'classic',
        states: params.get('states') ? params.get('states').split(',') : null,
        speed: parseInt(params.get('speed')) || 10000,
        sponsor: params.get('sponsor') || null
    };
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        STATE_MAP,
        ALERT_TYPE_INFO,
        getAlertDisplayInfo,
        formatExpirationTime,
        formatLocation,
        getStateFromUGC,
        alertMatchesStateFilter,
        getBasePath,
        getWebSocketUrl,
        getApiUrl,
        getUrlParams
    };
}

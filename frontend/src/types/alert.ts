// Alert types matching the backend models

export interface VTEC {
  product_class: string;
  action: string;
  office: string;
  phenomenon: string;
  significance: string;
  event_tracking_number: number;
  begin_time: string | null;
  end_time: string | null;
  raw_vtec: string;
}

export interface ThreatInfo {
  // Tornado
  tornado_detection: string | null;  // "RADAR INDICATED", "OBSERVED", etc.
  tornado_damage_threat: string | null;
  // Convenience fields (computed from above)
  has_tornado?: boolean;
  tornado_possible?: boolean;
  tornado_observed?: boolean;

  // Wind - sustained and gusts
  sustained_wind_min_mph: number | null;  // e.g., 25 in "winds 25 to 35 mph"
  sustained_wind_max_mph: number | null;  // e.g., 35 in "winds 25 to 35 mph"
  max_wind_gust_mph: number | null;
  max_wind_gust_kts: number | null;
  wind_damage_threat: string | null;

  // Hail
  max_hail_size_inches: number | null;
  hail_damage_threat: string | null;

  // Winter weather
  snow_amount_min_inches: number | null;
  snow_amount_max_inches: number | null;
  ice_accumulation_inches: number | null;

  // Flood
  flash_flood_detection: string | null;
  flash_flood_damage_threat: string | null;

  // Storm motion
  storm_motion?: {
    direction_degrees: number | null;
    direction_from: string | null;
    speed_mph: number | null;
    speed_kts: number | null;
  } | null;
}

export type AlertPriority = 'critical' | 'high' | 'medium' | 'low';
export type AlertStatus = 'active' | 'cancelled' | 'expired' | 'updated';

export interface Alert {
  product_id: string;
  message_id: string | null;
  source: 'nwws' | 'api' | 'unknown';
  vtec: VTEC | null;
  phenomenon: string;
  significance: string;
  event_name: string;
  headline: string | null;
  description: string | null;
  instruction: string | null;
  issued_time: string | null;
  effective_time: string | null;
  onset_time: string | null;
  expiration_time: string | null;
  message_expires: string | null;
  affected_areas: string[];
  fips_codes: string[];
  display_locations: string;  // Human-readable location names
  polygon: number[][] | null;  // [[lat, lon], ...]
  centroid: [number, number] | null;
  sender_office: string;  // WFO code (e.g., "CLE") - primary/first office
  sender_name: string;    // Full office name(s) (e.g., "NWS Cleveland OH" or "NWS Cleveland OH | NWS Detroit MI")
  issuing_offices: string[];  // All offices that issued this (for watches with merged products)
  threat: ThreatInfo;
  priority: number;
  status: AlertStatus;
  is_active: boolean;
  is_high_priority: boolean;
  time_remaining: string;
  parsed_at: string;
  last_updated: string;
  update_count: number;
}

export interface AlertStats {
  total_alerts: number;
  warnings: number;
  watches: number;
  high_priority: number;
  by_phenomenon: Record<string, number>;
  by_source: Record<string, number>;
}

// WebSocket message types
export type WSMessageType =
  | 'alert_new'
  | 'alert_update'
  | 'alert_remove'
  | 'alert_bulk'
  | 'system_status'
  | 'connection_ack'
  | 'error'
  | 'pong';

export interface WSMessage {
  type: WSMessageType;
  data: unknown;
  timestamp: string;
}

export interface AlertBulkData {
  count: number;
  alerts: Alert[];
}

export interface AlertRemoveData {
  product_id: string;
  event_name: string;
  reason: 'expired' | 'cancelled';
}

// Alert styling configuration
export interface AlertStyle {
  backgroundColor: string;
  borderColor: string;
  textColor: string;
  counterClass: string;
}

// Phenomenon code to display name mapping
// Comprehensive list of NWS alert phenomena
export const PHENOMENON_NAMES: Record<string, string> = {
  // Tornado
  TO: 'Tornado Warning',
  TOR: 'Tornado Warning',
  TOA: 'Tornado Watch',
  // Severe Thunderstorm
  SV: 'Severe T-Storm Warning',
  SVR: 'Severe T-Storm Warning',
  SVS: 'Severe Weather Statement',
  SVA: 'Severe T-Storm Watch',
  // Flash Flood
  FF: 'Flash Flood Warning',
  FFW: 'Flash Flood Warning',
  FFS: 'Flash Flood Statement',
  FFA: 'Flash Flood Watch',
  // Flood
  FL: 'Flood Warning',
  FLW: 'Flood Warning',
  FLS: 'Flood Statement',
  FLA: 'Flood Watch',
  FA: 'Areal Flood Advisory',
  // Winter Storm
  WS: 'Winter Storm Warning',
  WSW: 'Winter Storm Warning',
  WSA: 'Winter Storm Watch',
  // Blizzard
  BZ: 'Blizzard Warning',
  // Ice Storm
  IS: 'Ice Storm Warning',
  // Lake Effect Snow
  LE: 'Lake Effect Snow Warning',
  // Winter Weather
  WW: 'Winter Weather Advisory',
  // Wind Chill
  WC: 'Wind Chill Warning',
  WCA: 'Wind Chill Watch',
  // Cold Weather
  CW: 'Cold Weather Advisory',
  // High Wind
  HW: 'High Wind Warning',
  HWA: 'High Wind Watch',
  // Wind Advisory
  WI: 'Wind Advisory',
  // Heat
  EH: 'Excessive Heat Warning',
  EHA: 'Excessive Heat Watch',
  HT: 'Heat Advisory',
  // Fire Weather
  FW: 'Fire Weather Warning',
  FWA: 'Fire Weather Watch',
  // Marine
  MA: 'Marine Warning',
  SC: 'Small Craft Advisory',
  GL: 'Gale Warning',
  SE: 'Hazardous Seas Warning',
  // Special
  SPS: 'Special Weather Statement',
  // Extreme Wind
  EW: 'Extreme Wind Warning',
  // Dust Storm
  DS: 'Dust Storm Warning',
  // Snow Squall
  SQ: 'Snow Squall Warning',
  // Dense Fog
  FG: 'Dense Fog Advisory',
  // Freeze
  FZ: 'Freeze Warning',
  FZA: 'Freeze Watch',
  FR: 'Frost Advisory',
  // Coastal/Tsunami
  TS: 'Tsunami Warning',
  TSA: 'Tsunami Watch',
  SU: 'High Surf Warning',
  CF: 'Coastal Flood Warning',
  CFA: 'Coastal Flood Watch',
  // Air Quality
  AQ: 'Air Quality Alert',
};

// Alert colors by phenomenon - Official NWS colors
// Reference: https://www.weather.gov/help-map
export const ALERT_COLORS: Record<string, AlertStyle> = {
  // Tornado - Red
  TO: { backgroundColor: '#ff0000', borderColor: '#cc0000', textColor: '#ffffff', counterClass: 'tor' },
  TOR: { backgroundColor: '#ff0000', borderColor: '#cc0000', textColor: '#ffffff', counterClass: 'tor' },
  TOA: { backgroundColor: '#ffff00', borderColor: '#cccc00', textColor: '#000000', counterClass: 'tor-watch' },

  // Severe Thunderstorm - Orange
  SV: { backgroundColor: '#ffa500', borderColor: '#cc8400', textColor: '#000000', counterClass: 'svr' },
  SVR: { backgroundColor: '#ffa500', borderColor: '#cc8400', textColor: '#000000', counterClass: 'svr' },
  SVS: { backgroundColor: '#00ffff', borderColor: '#00cccc', textColor: '#000000', counterClass: 'svs' },
  SVA: { backgroundColor: '#db7093', borderColor: '#b05a76', textColor: '#000000', counterClass: 'svr-watch' },

  // Flash Flood - Bright Green
  FF: { backgroundColor: '#8b0000', borderColor: '#6f0000', textColor: '#ffffff', counterClass: 'ffw' },
  FFW: { backgroundColor: '#8b0000', borderColor: '#6f0000', textColor: '#ffffff', counterClass: 'ffw' },
  FFS: { backgroundColor: '#8b0000', borderColor: '#6f0000', textColor: '#ffffff', counterClass: 'ffs' },
  FFA: { backgroundColor: '#2e8b57', borderColor: '#246f46', textColor: '#ffffff', counterClass: 'ffa' },

  // Flood - Green shades
  FL: { backgroundColor: '#00ff00', borderColor: '#00cc00', textColor: '#000000', counterClass: 'flw' },
  FLW: { backgroundColor: '#00ff00', borderColor: '#00cc00', textColor: '#000000', counterClass: 'flw' },
  FLS: { backgroundColor: '#00ff00', borderColor: '#00cc00', textColor: '#000000', counterClass: 'fls' },
  FLA: { backgroundColor: '#2e8b57', borderColor: '#246f46', textColor: '#ffffff', counterClass: 'fla' },
  FA: { backgroundColor: '#00ff7f', borderColor: '#00cc65', textColor: '#000000', counterClass: 'fa' },

  // Winter Storm - Pink
  WS: { backgroundColor: '#ff69b4', borderColor: '#cc5490', textColor: '#000000', counterClass: 'wsw' },
  WSW: { backgroundColor: '#ff69b4', borderColor: '#cc5490', textColor: '#000000', counterClass: 'wsw' },
  WSA: { backgroundColor: '#4682b4', borderColor: '#3a6a90', textColor: '#ffffff', counterClass: 'wsa' },

  // Blizzard - Orange Red
  BZ: { backgroundColor: '#ff4500', borderColor: '#cc3700', textColor: '#ffffff', counterClass: 'bz' },

  // Ice Storm - Dark Magenta
  IS: { backgroundColor: '#8b008b', borderColor: '#6f006f', textColor: '#ffffff', counterClass: 'is' },

  // Lake Effect Snow - Dark Cyan
  LE: { backgroundColor: '#008b8b', borderColor: '#006f6f', textColor: '#ffffff', counterClass: 'le' },

  // Winter Weather - Slate Blue
  WW: { backgroundColor: '#7b68ee', borderColor: '#6253be', textColor: '#ffffff', counterClass: 'ww' },

  // Wind Chill - Light Blue
  WC: { backgroundColor: '#b0c4de', borderColor: '#8d9db2', textColor: '#000000', counterClass: 'wc' },
  WCA: { backgroundColor: '#5f9ea0', borderColor: '#4c7e80', textColor: '#ffffff', counterClass: 'wca' },

  // Cold Weather - Pale Turquoise
  CW: { backgroundColor: '#afeeee', borderColor: '#8ccece', textColor: '#000000', counterClass: 'cw' },

  // High Wind - Goldenrod
  HW: { backgroundColor: '#daa520', borderColor: '#ae8419', textColor: '#000000', counterClass: 'hw' },
  HWA: { backgroundColor: '#b8860b', borderColor: '#936b09', textColor: '#ffffff', counterClass: 'hwa' },

  // Wind Advisory - Tan (official NWS color)
  WI: { backgroundColor: '#d2b48c', borderColor: '#a8906f', textColor: '#000000', counterClass: 'wi' },

  // Heat - Coral/Red
  EH: { backgroundColor: '#c71585', borderColor: '#9f1169', textColor: '#ffffff', counterClass: 'eh' },
  EHA: { backgroundColor: '#800000', borderColor: '#660000', textColor: '#ffffff', counterClass: 'eha' },
  HT: { backgroundColor: '#ff7f50', borderColor: '#cc6540', textColor: '#000000', counterClass: 'ht' },

  // Fire Weather / Red Flag - Deep Pink
  FW: { backgroundColor: '#ff1493', borderColor: '#cc1076', textColor: '#ffffff', counterClass: 'fw' },
  FWA: { backgroundColor: '#ffdead', borderColor: '#ccb28a', textColor: '#000000', counterClass: 'fwa' },

  // Marine - Various
  MA: { backgroundColor: '#ffa500', borderColor: '#cc8400', textColor: '#000000', counterClass: 'ma' },
  SC: { backgroundColor: '#d8bfd8', borderColor: '#ad99ad', textColor: '#000000', counterClass: 'sc' },
  GL: { backgroundColor: '#dda0dd', borderColor: '#b180b1', textColor: '#000000', counterClass: 'gl' },
  SE: { backgroundColor: '#d8bfd8', borderColor: '#ad99ad', textColor: '#000000', counterClass: 'se' },

  // Special Weather Statement - Wheat/Tan
  SPS: { backgroundColor: '#ffe4b5', borderColor: '#ccb691', textColor: '#000000', counterClass: 'sps' },

  // Extreme Wind - Dark Orange
  EW: { backgroundColor: '#ff8c00', borderColor: '#cc7000', textColor: '#000000', counterClass: 'ew' },

  // Dust Storm - Bisque
  DS: { backgroundColor: '#ffe4c4', borderColor: '#ccb69c', textColor: '#000000', counterClass: 'ds' },

  // Snow Squall - Medium Violet Red
  SQ: { backgroundColor: '#C71585', borderColor: '#9f1169', textColor: '#ffffff', counterClass: 'sq' },

  // Dense Fog - Gray
  FG: { backgroundColor: '#708090', borderColor: '#596673', textColor: '#ffffff', counterClass: 'fg' },

  // Freeze - Cyan
  FZ: { backgroundColor: '#483d8b', borderColor: '#3a3170', textColor: '#ffffff', counterClass: 'fz' },
  FZA: { backgroundColor: '#00ced1', borderColor: '#00a5a7', textColor: '#000000', counterClass: 'fza' },
  FR: { backgroundColor: '#64ffda', borderColor: '#50ccae', textColor: '#000000', counterClass: 'fr' },

  // Coastal/Tsunami
  TS: { backgroundColor: '#fd6347', borderColor: '#ca4f39', textColor: '#ffffff', counterClass: 'ts' },
  TSA: { backgroundColor: '#ff00ff', borderColor: '#cc00cc', textColor: '#ffffff', counterClass: 'tsa' },
  SU: { backgroundColor: '#228b22', borderColor: '#1b6f1b', textColor: '#ffffff', counterClass: 'su' },
  CF: { backgroundColor: '#228b22', borderColor: '#1b6f1b', textColor: '#ffffff', counterClass: 'cf' },
  CFA: { backgroundColor: '#66cdaa', borderColor: '#52a488', textColor: '#000000', counterClass: 'cfa' },

  // Extreme Cold - Blue
  EC: { backgroundColor: '#0000ff', borderColor: '#0000cc', textColor: '#ffffff', counterClass: 'ec' },

  // Freezing Rain - Orchid
  ZR: { backgroundColor: '#da70d6', borderColor: '#ae5aab', textColor: '#000000', counterClass: 'zr' },

  // Dense Smoke - Khaki
  SM: { backgroundColor: '#f0e68c', borderColor: '#c0b870', textColor: '#000000', counterClass: 'sm' },

  // Freezing Fog - Teal
  ZF: { backgroundColor: '#008080', borderColor: '#006666', textColor: '#ffffff', counterClass: 'zf' },

  // Air Stagnation - Gray
  AS: { backgroundColor: '#808080', borderColor: '#666666', textColor: '#ffffff', counterClass: 'as' },

  // Hard Freeze - Dark Violet
  HZ: { backgroundColor: '#9400d3', borderColor: '#7600a9', textColor: '#ffffff', counterClass: 'hz' },

  // Storm Surge - Purple
  SS: { backgroundColor: '#b524f7', borderColor: '#911dc5', textColor: '#ffffff', counterClass: 'ss' },

  // Air Quality
  AQ: { backgroundColor: '#808080', borderColor: '#666666', textColor: '#ffffff', counterClass: 'aq' },

  // Default
  DEFAULT: { backgroundColor: '#444444', borderColor: '#333333', textColor: '#ffffff', counterClass: 'default' },
};

export function getAlertStyle(phenomenon: string, significance?: string): AlertStyle {
  // For watches (significance 'A'), try the combined key first (e.g., "TOA" for Tornado Watch)
  if (significance === 'A') {
    const watchKey = `${phenomenon}A`;
    if (ALERT_COLORS[watchKey]) {
      return ALERT_COLORS[watchKey];
    }
  }
  return ALERT_COLORS[phenomenon] || ALERT_COLORS.DEFAULT;
}

export function getPhenomenonName(phenomenon: string): string {
  return PHENOMENON_NAMES[phenomenon] || phenomenon;
}

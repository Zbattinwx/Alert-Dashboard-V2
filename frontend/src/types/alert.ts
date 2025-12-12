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
  has_tornado: boolean;
  tornado_possible: boolean;
  tornado_observed: boolean;
  tornado_damage_threat: string | null;
  max_hail_size_inches: number | null;
  max_wind_gust_mph: number | null;
  flash_flood_damage_threat: string | null;
}

export type AlertPriority = 'critical' | 'high' | 'medium' | 'low';
export type AlertStatus = 'active' | 'cancelled' | 'expired' | 'updated';

export interface Alert {
  product_id: string;
  message_id: string;
  source: 'nwws' | 'api';
  vtec: VTEC | null;
  phenomenon: string;
  significance: string;
  event_name: string;
  headline: string | null;
  description: string | null;
  instruction: string | null;
  issued_time: string | null;
  effective_time: string | null;
  expiration_time: string | null;
  affected_areas: string[];
  issuing_office: string | null;
  polygon: number[][][] | null;
  threat: ThreatInfo;
  priority: AlertPriority;
  status: AlertStatus;
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
export const PHENOMENON_NAMES: Record<string, string> = {
  TO: 'Tornado Warning',
  SV: 'Severe Thunderstorm Warning',
  FF: 'Flash Flood Warning',
  MA: 'Marine Warning',
  EW: 'Extreme Wind Warning',
  // Watches
  TOA: 'Tornado Watch',
  SVA: 'Severe Thunderstorm Watch',
  FFA: 'Flash Flood Watch',
  // Winter
  WS: 'Winter Storm Warning',
  BZ: 'Blizzard Warning',
  IS: 'Ice Storm Warning',
  LE: 'Lake Effect Snow Warning',
  WW: 'Winter Weather Advisory',
  WC: 'Wind Chill Warning',
  WSA: 'Winter Storm Watch',
  // Other
  HW: 'High Wind Warning',
  EH: 'Excessive Heat Warning',
  FW: 'Fire Weather Warning',
  SPS: 'Special Weather Statement',
  // Add more as needed
};

// Alert colors by phenomenon
export const ALERT_COLORS: Record<string, AlertStyle> = {
  TO: {
    backgroundColor: '#ff0000',
    borderColor: '#cc0000',
    textColor: '#ffffff',
    counterClass: 'tor',
  },
  SV: {
    backgroundColor: '#ffa500',
    borderColor: '#cc8400',
    textColor: '#000000',
    counterClass: 'svr',
  },
  FF: {
    backgroundColor: '#00ff00',
    borderColor: '#00cc00',
    textColor: '#000000',
    counterClass: 'ffw',
  },
  WS: {
    backgroundColor: '#ff69b4',
    borderColor: '#cc5490',
    textColor: '#000000',
    counterClass: 'wsw-warn',
  },
  BZ: {
    backgroundColor: '#ff4500',
    borderColor: '#cc3700',
    textColor: '#ffffff',
    counterClass: 'bz-warn',
  },
  IS: {
    backgroundColor: '#8b008b',
    borderColor: '#6f006f',
    textColor: '#ffffff',
    counterClass: 'is-warn',
  },
  LE: {
    backgroundColor: '#00ffff',
    borderColor: '#00cccc',
    textColor: '#000000',
    counterClass: 'le-warn',
  },
  WW: {
    backgroundColor: '#7b68ee',
    borderColor: '#6253be',
    textColor: '#ffffff',
    counterClass: 'ww-advisory',
  },
  SPS: {
    backgroundColor: '#ffe4b5',
    borderColor: '#ccb691',
    textColor: '#000000',
    counterClass: 'sps',
  },
  // Default
  DEFAULT: {
    backgroundColor: '#444444',
    borderColor: '#333333',
    textColor: '#ffffff',
    counterClass: 'default',
  },
};

export function getAlertStyle(phenomenon: string): AlertStyle {
  return ALERT_COLORS[phenomenon] || ALERT_COLORS.DEFAULT;
}

export function getPhenomenonName(phenomenon: string): string {
  return PHENOMENON_NAMES[phenomenon] || phenomenon;
}

/**
 * TypeScript types for NEXRAD Level 2 radar and storm cell tracking.
 */

export interface RadarFrame {
  product: string;
  image_url: string;
  bounds: {
    south: number;
    north: number;
    west: number;
    east: number;
  };
  timestamp: string;
  site: string;
  elevation: number;
}

export interface RadarStatus {
  enabled: boolean;
  active_site: string;     // primary site (backward compat)
  active_sites: string[];  // all active sites
  last_update: string | null;
  processing: boolean;
  available_products: string[];
  error: string | null;
}

export interface NexradSite {
  id: string;
  name: string;
  lat: number;
  lon: number;
  state: string;
  distance_km?: number;
}

export interface StormCell {
  cell_id: string;
  lat: number;
  lon: number;
  max_reflectivity_dbz: number;
  area_km2: number;
  severity_score: number;
  threat_level: ThreatLevel;
  motion_direction_deg: number;
  motion_speed_kph: number;
  rotation_detected: boolean;
  rotation_velocity_ms: number | null;
  tvs_detected: boolean;
  qlcs_meso_detected: boolean;
  qlcs_meso_velocity_ms: number | null;
  llsd_rotation_detected?: boolean;
  llsd_max_shear?: number | null;
  llsd_elevation_deg?: number | null;
  max_rot_velocity_ms?: number | null;
  max_rot_height_km?: number | null;
  rotation_depth_km?: number | null;
  rotation_profile?: Array<{ height_km: number; elevation_deg: number; rot_velocity_ms: number }>;
  cell_base_km?: number | null;
  max_ref_height_km?: number | null;
  centroid_height_km?: number | null;
  depth_km?: number | null;
  hail_indicated: boolean;
  hail_max_dbz: number | null;
  debris_signature: boolean;
  vil_kg_m2: number | null;
  cell_top_km: number | null;
  track_history: TrackPoint[];
  forecast_track: ForecastPoint[];
  score_breakdown: Record<string, number>;
  first_detected: string;
  last_updated: string;
  trend: 'strengthening' | 'steady' | 'weakening';
  scan_count: number;
  mcs_system_id: string | null;
}

export interface MCSSystem {
  system_id: string;
  system_type: 'squall_line' | 'bow_echo' | 'mcs';
  cell_ids: string[];
  centroid_lat: number;
  centroid_lon: number;
  orientation_deg: number;
  length_km: number;
  bow_echo_detected: boolean;
  rear_inflow_notch: boolean;
  book_end_vortices: boolean;
  embedded_qlcs_mesos: number;
  max_severity_score: number;
  threat_level: ThreatLevel;
  motion_direction_deg: number;
  motion_speed_kph: number;
  timestamp: string;
}

export const MCS_TYPE_LABELS: Record<string, string> = {
  squall_line: 'Squall Line',
  bow_echo:    'Bow Echo',
  mcs:         'MCS',
};

export interface TrackPoint {
  lat: number;
  lon: number;
  timestamp: string;
}

export interface ForecastPoint {
  lat: number;
  lon: number;
  minutes_ahead: number;
}

export type ThreatLevel = 'minimal' | 'moderate' | 'significant' | 'severe' | 'extreme';

export type RadarProduct = 'reflectivity' | 'velocity' | 'cross_correlation_ratio';

export const RADAR_PRODUCT_LABELS: Record<RadarProduct, string> = {
  reflectivity: 'Reflectivity',
  velocity: 'Velocity',
  cross_correlation_ratio: 'Corr. Coeff.',
};

export const RADAR_PRODUCT_SHORT: Record<RadarProduct, string> = {
  reflectivity: 'Z',
  velocity: 'V',
  cross_correlation_ratio: 'CC',
};

export const RADAR_PRODUCT_UNITS: Record<RadarProduct, string> = {
  reflectivity: 'dBZ',
  velocity: 'm/s',
  cross_correlation_ratio: '',
};

export const THREAT_LEVEL_COLORS: Record<ThreatLevel, string> = {
  minimal: '#888888',
  moderate: '#FFD700',
  significant: '#FF8C00',
  severe: '#FF0000',
  extreme: '#FF00FF',
};

export const THREAT_LEVEL_LABELS: Record<ThreatLevel, string> = {
  minimal: 'Minimal',
  moderate: 'Moderate',
  significant: 'Significant',
  severe: 'Severe',
  extreme: 'Extreme',
};

export interface LightningFlash {
  lat: number;
  lon: number;
  energy: number;   // optical radiant energy (Joules)
  timestamp: string;
}

export const SCORE_FACTOR_LABELS: Record<string, string> = {
  reflectivity: 'Reflectivity',
  growth_trend: 'Growth Trend',
  hail: 'Hail',
  rotation: 'Rotation',
  debris: 'Debris Signature',
  vil: 'VIL',
  cell_top: 'Cell Top',
  lightning: 'Lightning Rate',
};

export const SCORE_FACTOR_WEIGHTS: Record<string, number> = {
  reflectivity: 18,
  growth_trend: 10,
  hail: 18,
  rotation: 24,
  debris: 15,
  vil: 5,
  cell_top: 5,
  lightning: 5,
};

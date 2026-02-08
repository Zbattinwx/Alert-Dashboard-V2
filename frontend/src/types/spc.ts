/**
 * SPC (Storm Prediction Center) TypeScript Types
 */

export interface OutlookPolygon {
  risk_level: string;
  risk_name: string;
  color: string;
  valid_time: string | null;
  expire_time: string | null;
  issue_time: string | null;
  geometry: GeoJSON.Geometry | null;
}

export interface OutlookData {
  day: number;
  outlook_type: string;
  valid_time: string | null;
  expire_time: string | null;
  issue_time: string | null;
  polygons: OutlookPolygon[];
  geojson?: GeoJSON.FeatureCollection;
}

export interface MesoscaleDiscussion {
  md_number: string;
  title: string;
  link: string;
  description: string;
  image_url: string;
  affected_states: string[];
}

export interface OutlooksResponse {
  outlooks: Record<string, OutlookData>;
  risk_colors: Record<string, string>;
  risk_names: Record<string, string>;
}

export interface OutlookResponse {
  outlook: OutlookData;
  risk_colors: Record<string, string>;
  risk_names: Record<string, string>;
}

export interface Day1Response {
  categorical: OutlookData | null;
  tornado?: OutlookData | null;
  wind?: OutlookData | null;
  hail?: OutlookData | null;
  risk_colors: Record<string, string>;
  risk_names: Record<string, string>;
}

export interface MesoscaleDiscussionsResponse {
  count: number;
  total_count: number;
  filter_states: string[];
  discussions: MesoscaleDiscussion[];
}

export interface StateImagesResponse {
  day: number;
  states: string[];
  images: Record<string, {
    categorical: string;
    tornado: string;
    wind: string;
    hail: string;
  }>;
}

export interface RiskAtPointResponse {
  lat: number;
  lon: number;
  outlook_key: string;
  risk: OutlookPolygon | null;
  message?: string;
}

// Risk level display order (higher = more severe)
export const RISK_ORDER: Record<string, number> = {
  'TSTM': 0,
  'MRGL': 1,
  'SLGT': 2,
  'ENH': 3,
  'MDT': 4,
  'HIGH': 5,
};

// Risk level colors (matching backend)
export const RISK_COLORS: Record<string, string> = {
  'TSTM': '#76C776',
  'MRGL': '#66A366',
  'SLGT': '#F6F67F',
  'ENH': '#E6C27A',
  'MDT': '#E67F7F',
  'HIGH': '#FF66FF',
};

// Risk level display names
export const RISK_NAMES: Record<string, string> = {
  'TSTM': 'General Thunderstorms',
  'MRGL': 'Marginal Risk',
  'SLGT': 'Slight Risk',
  'ENH': 'Enhanced Risk',
  'MDT': 'Moderate Risk',
  'HIGH': 'High Risk',
};

// Probabilistic outlook colors (for tornado, wind, hail)
export const PROB_COLORS: Record<string, string> = {
  '0.02': '#008B00',   // 2% - Dark Green
  '2': '#008B00',
  '0.05': '#8B4726',   // 5% - Brown
  '5': '#8B4726',
  '0.10': '#FFD700',   // 10% - Gold/Yellow
  '10': '#FFD700',
  '0.15': '#FF0000',   // 15% - Red
  '15': '#FF0000',
  '0.30': '#FF00FF',   // 30% - Magenta
  '30': '#FF00FF',
  '0.45': '#9400D3',   // 45% - Purple
  '45': '#9400D3',
  '0.60': '#882D60',   // 60% - Dark Magenta
  '60': '#882D60',
  'SIGN': '#000000',   // Significant - Black hatching
  'SIGPROB': '#000000',
};

// Probabilistic outlook display names
export const PROB_NAMES: Record<string, string> = {
  '0.02': '2% Probability',
  '2': '2% Probability',
  '0.05': '5% Probability',
  '5': '5% Probability',
  '0.10': '10% Probability',
  '10': '10% Probability',
  '0.15': '15% Probability',
  '15': '15% Probability',
  '0.30': '30% Probability',
  '30': '30% Probability',
  '0.45': '45% Probability',
  '45': '45% Probability',
  '0.60': '60% Probability',
  '60': '60% Probability',
  'SIGN': 'Significant',
  'SIGPROB': 'Significant',
};

// Probabilistic risk order
export const PROB_ORDER: Record<string, number> = {
  '0.02': 1, '2': 1,
  '0.05': 2, '5': 2,
  '0.10': 3, '10': 3,
  '0.15': 4, '15': 4,
  '0.30': 5, '30': 5,
  '0.45': 6, '45': 6,
  '0.60': 7, '60': 7,
  'SIGN': 8, 'SIGPROB': 8,
};

/**
 * Get text color (black or white) for a given background color
 */
export function getContrastColor(bgColor: string): string {
  const hex = bgColor.replace('#', '');
  const r = parseInt(hex.substr(0, 2), 16);
  const g = parseInt(hex.substr(2, 2), 16);
  const b = parseInt(hex.substr(4, 2), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.5 ? '#000000' : '#FFFFFF';
}

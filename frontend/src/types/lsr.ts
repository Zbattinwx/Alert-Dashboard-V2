/**
 * Types for Local Storm Reports (LSR)
 */

export interface StormReport {
  id: string;
  report_type: string;
  magnitude: string | null;
  city: string;
  county: string;
  state: string;
  lat: number;
  lon: number;
  valid_time: string | null;
  remark: string;
  source: string;
  wfo: string;
  color: string;
  icon: string;
  // Viewer report specific fields
  is_viewer: boolean;
  submitter: string;
  location_text: string;
}

export interface LSRResponse {
  count: number;
  reports: StormReport[];
  type_colors: Record<string, string>;
  viewer_count?: number;
}

export interface ViewerReportSubmission {
  report_type: string;
  lat: number;
  lon: number;
  magnitude?: string;
  remarks?: string;
  location?: string;
  submitter?: string;
}

export interface LSRStats {
  total_reports: number;
  manual_reports: number;
  by_type: Record<string, number>;
  cache_age_seconds: number | null;
}

// LSR type colors matching backend
export const LSR_TYPE_COLORS: Record<string, string> = {
  'TORNADO': '#FF0000',
  'FUNNEL CLOUD': '#FF6600',
  'WALL CLOUD': '#FF9900',
  'HAIL': '#00FF00',
  'TSTM WND GST': '#FFD700',
  'TSTM WND DMG': '#FFA500',
  'NON-TSTM WND GST': '#CCCC00',
  'NON-TSTM WND DMG': '#CC9900',
  'FLOOD': '#00FF7F',
  'FLASH FLOOD': '#8B0000',
  'HEAVY RAIN': '#0066FF',
  'LIGHTNING': '#FFFF00',
  'SNOW': '#00BFFF',
  'HEAVY SNOW': '#1E90FF',
  'BLIZZARD': '#FF69B4',
  'ICE STORM': '#8B008B',
  'SLEET': '#9370DB',
  'FREEZING RAIN': '#DA70D6',
};

// Get appropriate text color (black or white) based on background
export function getTextColorForBackground(bgColor: string): string {
  // Convert hex to RGB
  const hex = bgColor.replace('#', '');
  const r = parseInt(hex.substr(0, 2), 16);
  const g = parseInt(hex.substr(2, 2), 16);
  const b = parseInt(hex.substr(4, 2), 16);

  // Calculate luminance
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;

  return luminance > 0.5 ? '#000000' : '#FFFFFF';
}

/**
 * Types for ODOT (Ohio DOT) Cameras and Sensors
 */

export interface ODOTCamera {
  id: string;
  location: string;
  latitude: number;
  longitude: number;
  image_url: string;
  description: string;
  in_alert: boolean;
  alert_type: string;
  alert_name: string;
}

export interface RoadSensor {
  id: string;
  location: string;
  latitude: number;
  longitude: number;
  description: string;
  air_temp: number | null;
  wind_speed: number | null;
  wind_direction: string | null;
  precip_rate: number | null;
  pavement_temp: number | null;
  surface_status: string | null;
  is_cold: boolean;
  is_freezing: boolean;
}

export interface CamerasResponse {
  count: number;
  cameras: ODOTCamera[];
}

export interface SensorsResponse {
  count: number;
  sensors: RoadSensor[];
}

export interface ColdSensorsResponse {
  count: number;
  freezing_count: number;
  cold_threshold: number;
  freezing_threshold: number;
  sensors: RoadSensor[];
}

export interface CamerasInAlertsResponse {
  count: number;
  phenomena_filter: string[];
  cameras: ODOTCamera[];
}

export interface ODOTStats {
  total_cameras: number;
  total_sensors: number;
  cold_sensors: number;
  freezing_sensors: number;
  cameras_cache_age_seconds: number | null;
  sensors_cache_age_seconds: number | null;
}

export interface ChaserPosition {
  client_id: string;
  name: string;
  lat: number;
  lon: number;
  heading: number | null;
  speed: number | null;
  last_update: string;
}

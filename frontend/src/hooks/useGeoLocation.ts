import { useEffect, useRef, useState } from 'react';
import { msToMph } from '../utils/geoUtils';

export interface GeoLocationState {
  lat: number | null;
  lon: number | null;
  accuracy: number | null;
  heading: number | null;
  speed: number | null;
  speedMph: number | null;
  error: string | null;
  loading: boolean;
}

export function useGeoLocation(): GeoLocationState {
  const [state, setState] = useState<GeoLocationState>({
    lat: null,
    lon: null,
    accuracy: null,
    heading: null,
    speed: null,
    speedMph: null,
    error: null,
    loading: true,
  });

  const watchIdRef = useRef<number | null>(null);

  useEffect(() => {
    if (!navigator.geolocation) {
      setState(prev => ({
        ...prev,
        error: 'Geolocation is not supported by this browser',
        loading: false,
      }));
      return;
    }

    watchIdRef.current = navigator.geolocation.watchPosition(
      (position) => {
        const { latitude, longitude, accuracy, heading, speed } = position.coords;
        setState({
          lat: latitude,
          lon: longitude,
          accuracy: accuracy,
          heading: heading,
          speed: speed,
          speedMph: speed !== null ? msToMph(speed) : null,
          error: null,
          loading: false,
        });
      },
      (error) => {
        setState(prev => ({
          ...prev,
          error: error.message,
          loading: false,
        }));
      },
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 0,
      }
    );

    return () => {
      if (watchIdRef.current !== null) {
        navigator.geolocation.clearWatch(watchIdRef.current);
      }
    };
  }, []);

  return state;
}

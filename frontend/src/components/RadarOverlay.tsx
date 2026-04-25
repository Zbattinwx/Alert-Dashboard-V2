import { useEffect, useRef } from 'react';
import { useMap } from 'react-leaflet';
import L from 'leaflet';
import type { RadarFrame } from '../types/radar';
import { apiUrl } from '../utils/api';

interface RadarOverlayProps {
  frame: RadarFrame | null;
  opacity?: number;
  pane?: string;
}

export default function RadarOverlay({ frame, opacity = 0.6, pane = 'radarPane' }: RadarOverlayProps) {
  const map = useMap();
  const overlayRef = useRef<L.ImageOverlay | null>(null);

  // Ensure the pane exists
  useEffect(() => {
    if (!map.getPane(pane)) {
      map.createPane(pane);
      const paneEl = map.getPane(pane);
      if (paneEl) {
        paneEl.style.zIndex = '350';
      }
    }
  }, [map, pane]);

  useEffect(() => {
    if (!frame || !frame.image_url || !frame.bounds) {
      // Remove overlay if no frame
      if (overlayRef.current) {
        overlayRef.current.remove();
        overlayRef.current = null;
      }
      return;
    }

    const bounds: L.LatLngBoundsExpression = [
      [frame.bounds.south, frame.bounds.west],
      [frame.bounds.north, frame.bounds.east],
    ];

    const imageUrl = apiUrl(frame.image_url);

    if (overlayRef.current) {
      // Update existing overlay
      overlayRef.current.setUrl(imageUrl);
      overlayRef.current.setBounds(L.latLngBounds(bounds));
      overlayRef.current.setOpacity(opacity);
    } else {
      // Create new overlay
      overlayRef.current = L.imageOverlay(imageUrl, bounds, {
        opacity,
        pane,
        interactive: false,
      });
      overlayRef.current.addTo(map);
    }

    return () => {
      // Don't remove on every render - let it persist
    };
  }, [frame, opacity, map, pane]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (overlayRef.current) {
        overlayRef.current.remove();
        overlayRef.current = null;
      }
    };
  }, []);

  // Update opacity when it changes
  useEffect(() => {
    if (overlayRef.current) {
      overlayRef.current.setOpacity(opacity);
    }
  }, [opacity]);

  return null;
}

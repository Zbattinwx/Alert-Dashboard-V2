import { useEffect, useRef, useState, useCallback } from 'react';
import type { Alert, WSMessage, AlertBulkData, AlertRemoveData, AgentNotification } from '../types/alert';
import type { MesoscaleDiscussion } from '../types/spc';
import type { ChaserPosition } from '../types/chaser';
import type { RadarFrame, RadarBinaryFrame, RadarStatus, StormCell, LightningFlash, MCSSystem } from '../types/radar';
import { parseRadarBinaryFrame } from '../utils/radarBinaryParser';

interface UseWebSocketOptions {
  url: string;
  onAlert?: (alert: Alert) => void;
  onAlertUpdate?: (alert: Alert) => void;
  onAlertRemove?: (data: AlertRemoveData) => void;
  onBulkAlerts?: (alerts: Alert[]) => void;
  onMD?: (md: MesoscaleDiscussion) => void;
  onStatusChange?: (connected: boolean) => void;
  onChaserPosition?: (data: ChaserPosition) => void;
  onChaserDisconnect?: (data: { client_id: string }) => void;
  onRadarBinaryFrame?: (frame: RadarBinaryFrame) => void;
  onRadarFrame?: (frame: RadarFrame) => void;
  onRadarStatus?: (status: RadarStatus) => void;
  onStormCells?: (cells: StormCell[]) => void;
  onMcsSystems?: (systems: MCSSystem[]) => void;
  onAgentNotification?: (notification: AgentNotification) => void;
  onLightningStrikes?: (flashes: LightningFlash[]) => void;
  reconnectInterval?: number;
}

interface UseWebSocketReturn {
  connected: boolean;
  alerts: Alert[];
  sendMessage: (type: string, data?: unknown) => void;
  reconnect: () => void;
}

export function useWebSocket(options: UseWebSocketOptions): UseWebSocketReturn {
  const {
    url,
    onAlert,
    onAlertUpdate,
    onAlertRemove,
    onBulkAlerts,
    onMD,
    onStatusChange,
    onChaserPosition,
    onChaserDisconnect,
    onRadarBinaryFrame,
    onRadarFrame,
    onRadarStatus,
    onStormCells,
    onMcsSystems,
    onAgentNotification,
    onLightningStrikes,
    reconnectInterval = 5000,
  } = options;

  const [connected, setConnected] = useState(false);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);

  const connect = useCallback(() => {
    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.close();
    }

    console.log('Connecting to WebSocket:', url);
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('WebSocket connected');
      setConnected(true);
      onStatusChange?.(true);
    };

    ws.onclose = () => {
      console.log('WebSocket disconnected');
      setConnected(false);
      onStatusChange?.(false);

      // Schedule reconnect
      reconnectTimeoutRef.current = window.setTimeout(() => {
        console.log('Attempting reconnect...');
        connect();
      }, reconnectInterval);
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    ws.onmessage = (event) => {
      // Binary frames (RDRF wire format) arrive as ArrayBuffer
      if (event.data instanceof ArrayBuffer) {
        try {
          onRadarBinaryFrame?.(parseRadarBinaryFrame(event.data));
        } catch (err) {
          console.error('Failed to parse binary radar frame:', err);
        }
        return;
      }
      try {
        const message: WSMessage = JSON.parse(event.data as string);
        handleMessage(message);
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err);
      }
    };
  }, [url, reconnectInterval, onStatusChange]);

  const handleMessage = useCallback((message: WSMessage) => {
    // Dev-only: this fires for every message (incl. high-frequency radar_frame /
    // storm_cells / lightning_strikes / pong), so keep it out of production.
    if (import.meta.env.DEV) console.log('WS Message:', message.type, message.data);

    switch (message.type) {
      case 'connection_ack':
        console.log('Connection acknowledged');
        break;

      case 'alert_bulk': {
        const bulkData = message.data as AlertBulkData;
        console.log(`Received ${bulkData.count} alerts`);
        setAlerts(bulkData.alerts);
        onBulkAlerts?.(bulkData.alerts);
        break;
      }

      case 'alert_new': {
        const newAlert = message.data as Alert;
        setAlerts((prev) => {
          // Check if already exists
          const exists = prev.some((a) => a.product_id === newAlert.product_id);
          if (exists) {
            return prev.map((a) =>
              a.product_id === newAlert.product_id ? newAlert : a
            );
          }
          return [newAlert, ...prev];
        });
        onAlert?.(newAlert);
        break;
      }

      case 'alert_update': {
        const updatedAlert = message.data as Alert;
        setAlerts((prev) =>
          prev.map((a) =>
            a.product_id === updatedAlert.product_id ? updatedAlert : a
          )
        );
        onAlertUpdate?.(updatedAlert);
        break;
      }

      case 'alert_remove': {
        const removeData = message.data as AlertRemoveData;
        setAlerts((prev) =>
          prev.filter((a) => a.product_id !== removeData.product_id)
        );
        onAlertRemove?.(removeData);
        break;
      }

      case 'md_new': {
        const md = message.data as MesoscaleDiscussion;
        onMD?.(md);
        break;
      }

      case 'system_status':
        console.log('System status:', message.data);
        break;

      case 'pong':
        // Heartbeat response
        break;

      case 'chaser_position':
        onChaserPosition?.(message.data as ChaserPosition);
        break;

      case 'chaser_disconnect':
        onChaserDisconnect?.(message.data as { client_id: string });
        break;

      case 'radar_frame':
        onRadarFrame?.(message.data as RadarFrame);
        break;

      case 'radar_status':
        onRadarStatus?.(message.data as RadarStatus);
        break;

      case 'storm_cells':
        onStormCells?.(message.data as StormCell[]);
        break;

      case 'mcs_systems':
        onMcsSystems?.(message.data as MCSSystem[]);
        break;

      case 'agent_notification':
        onAgentNotification?.(message.data as AgentNotification);
        break;

      case 'lightning_strikes':
        onLightningStrikes?.(message.data as LightningFlash[]);
        break;

      case 'error':
        console.error('WebSocket error message:', message.data);
        break;

      case 'alert_zones':
        // Live zone-fill update; the React map polls /api/map/zones, so this
        // broadcast is intentionally ignored here (not an error).
        break;

      default:
        console.log('Unknown message type:', message.type);
    }
  }, [onAlert, onAlertUpdate, onAlertRemove, onBulkAlerts, onMD, onChaserPosition, onChaserDisconnect, onRadarBinaryFrame, onRadarFrame, onRadarStatus, onStormCells, onMcsSystems, onAgentNotification, onLightningStrikes]);

  const sendMessage = useCallback((type: string, data?: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type, data }));
    }
  }, []);

  const reconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }
    connect();
  }, [connect]);

  // Connect on mount
  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect]);

  // Heartbeat ping every 30 seconds
  useEffect(() => {
    if (!connected) return;

    const interval = setInterval(() => {
      sendMessage('ping');
    }, 30000);

    return () => clearInterval(interval);
  }, [connected, sendMessage]);

  return {
    connected,
    alerts,
    sendMessage,
    reconnect,
  };
}

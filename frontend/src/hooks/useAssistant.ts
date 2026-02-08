import { useState, useCallback, useEffect } from 'react';

interface AssistantStatus {
  enabled: boolean;
  available: boolean;
  model?: string;
  host?: string;
  message?: string;
}

interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
}

interface ChatResponse {
  success: boolean;
  response: string;
  model: string;
  duration_ms?: number;
}

interface UseAssistantOptions {
  autoCheckStatus?: boolean;
  statusCheckInterval?: number;
}

interface UseAssistantReturn {
  // Status
  status: AssistantStatus | null;
  isAvailable: boolean;
  isLoading: boolean;
  error: string | null;

  // Chat
  messages: ChatMessage[];
  sendMessage: (message: string) => Promise<string | null>;
  clearHistory: () => Promise<void>;

  // Insights
  getInsight: (type?: string) => Promise<string | null>;

  // Actions
  checkStatus: () => Promise<void>;
}

export function useAssistant(options: UseAssistantOptions = {}): UseAssistantReturn {
  const {
    autoCheckStatus = true,
    statusCheckInterval = 60000, // 1 minute
  } = options;

  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isAvailable = status?.enabled === true && status?.available === true;

  // Check assistant status
  const checkStatus = useCallback(async () => {
    try {
      const response = await fetch('/api/assistant/status');
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data: AssistantStatus = await response.json();
      setStatus(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check status');
      setStatus({ enabled: false, available: false, message: 'Failed to connect' });
    }
  }, []);

  // Send a chat message
  const sendMessage = useCallback(async (message: string): Promise<string | null> => {
    if (!isAvailable) {
      setError('Assistant not available');
      return null;
    }

    setIsLoading(true);
    setError(null);

    // Add user message to local state immediately
    const userMessage: ChatMessage = {
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMessage]);

    try {
      const response = await fetch('/api/assistant/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data: ChatResponse = await response.json();

      // Add assistant response to local state
      const assistantMessage: ChatMessage = {
        role: 'assistant',
        content: data.response,
        timestamp: new Date().toISOString(),
      };
      setMessages(prev => [...prev, assistantMessage]);

      return data.response;
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to send message';
      setError(errorMsg);

      // Add error message
      const errorMessage: ChatMessage = {
        role: 'system',
        content: `Error: ${errorMsg}`,
        timestamp: new Date().toISOString(),
      };
      setMessages(prev => [...prev, errorMessage]);

      return null;
    } finally {
      setIsLoading(false);
    }
  }, [isAvailable]);

  // Clear chat history
  const clearHistory = useCallback(async () => {
    try {
      await fetch('/api/assistant/history', { method: 'DELETE' });
      setMessages([]);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear history');
    }
  }, []);

  // Get a quick insight (adds to messages)
  const getInsight = useCallback(async (type: string = 'general'): Promise<string | null> => {
    if (!isAvailable) {
      setError('Assistant not available');
      return null;
    }

    setIsLoading(true);
    setError(null);

    // Add a "request" message to show what we're doing
    const typeLabels: Record<string, string> = {
      general: 'Quick weather insight',
      safety: 'Safety recommendations',
      wind: 'Wind conditions',
      pattern: 'Pattern analysis',
    };
    const userMessage: ChatMessage = {
      role: 'user',
      content: typeLabels[type] || 'Insight request',
      timestamp: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMessage]);

    try {
      const response = await fetch(`/api/assistant/insight?insight_type=${type}`);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();

      // Add the insight as an assistant message
      const assistantMessage: ChatMessage = {
        role: 'assistant',
        content: data.insight,
        timestamp: new Date().toISOString(),
      };
      setMessages(prev => [...prev, assistantMessage]);

      return data.insight;
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to get insight';
      setError(errorMsg);

      // Add error message
      const errorMessage: ChatMessage = {
        role: 'system',
        content: `Error: ${errorMsg}`,
        timestamp: new Date().toISOString(),
      };
      setMessages(prev => [...prev, errorMessage]);

      return null;
    } finally {
      setIsLoading(false);
    }
  }, [isAvailable]);

  // Load existing history on mount
  useEffect(() => {
    const loadHistory = async () => {
      try {
        const response = await fetch('/api/assistant/history');
        if (response.ok) {
          const data = await response.json();
          if (data.history && Array.isArray(data.history)) {
            setMessages(data.history);
          }
        }
      } catch (err) {
        console.error('Failed to load chat history:', err);
      }
    };

    loadHistory();
  }, []);

  // Auto-check status on mount and periodically
  useEffect(() => {
    if (autoCheckStatus) {
      checkStatus();

      const interval = setInterval(checkStatus, statusCheckInterval);
      return () => clearInterval(interval);
    }
  }, [autoCheckStatus, statusCheckInterval, checkStatus]);

  return {
    status,
    isAvailable,
    isLoading,
    error,
    messages,
    sendMessage,
    clearHistory,
    getInsight,
    checkStatus,
  };
}

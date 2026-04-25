import { useState, useCallback, useEffect } from 'react';
import { apiUrl } from '../utils/api';

interface AssistantStatus {
  enabled: boolean;
  available: boolean;
  model?: string;
  host?: string;
  message?: string;
  tool_count?: number;
}

export interface ToolCall {
  tool: string;
  arguments: Record<string, any>;
  result?: string;
  status: 'executing' | 'success' | 'error';
  duration_ms?: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system' | 'proactive';
  content: string;
  timestamp: string;
  toolCalls?: ToolCall[];
  cellIds?: string[];
}

interface ChatResponse {
  success: boolean;
  response: string;
  model: string;
  duration_ms?: number;
}

interface AgentChatResponse {
  success: boolean;
  response: string;
  tool_calls: ToolCall[];
  rounds: number;
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

  // Mode
  agentMode: boolean;
  setAgentMode: (mode: boolean) => void;

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
  const [agentMode, setAgentMode] = useState(true); // Default to agent mode

  const isAvailable = status?.enabled === true && status?.available === true;

  // Check status (checks agent or assistant depending on mode)
  const checkStatus = useCallback(async () => {
    try {
      // Check both endpoints, prefer agent
      const agentRes = await fetch(apiUrl('/api/agent/status'));
      if (agentRes.ok) {
        const agentData = await agentRes.json();
        if (agentData.available) {
          setStatus({
            enabled: true,
            available: true,
            model: agentData.model,
            host: agentData.host,
            tool_count: agentData.tool_count,
          });
          setError(null);
          return;
        }
      }

      // Fall back to assistant status
      const response = await fetch(apiUrl('/api/assistant/status'));
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data: AssistantStatus = await response.json();
      setStatus(data);
      if (data.available && agentMode) {
        // Agent not available but assistant is -- switch mode
        setAgentMode(false);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to check status');
      setStatus({ enabled: false, available: false, message: 'Failed to connect' });
    }
  }, [agentMode]);

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
      if (agentMode) {
        // Use agent endpoint with tool calling
        const response = await fetch(apiUrl('/api/agent/chat'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message }),
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP ${response.status}`);
        }

        const data: AgentChatResponse = await response.json();

        // Add assistant response with tool calls
        const assistantMessage: ChatMessage = {
          role: 'assistant',
          content: data.response,
          timestamp: new Date().toISOString(),
          toolCalls: data.tool_calls.length > 0 ? data.tool_calls : undefined,
        };
        setMessages(prev => [...prev, assistantMessage]);

        return data.response;
      } else {
        // Use simple assistant endpoint
        const response = await fetch(apiUrl('/api/assistant/chat'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message }),
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP ${response.status}`);
        }

        const data: ChatResponse = await response.json();

        const assistantMessage: ChatMessage = {
          role: 'assistant',
          content: data.response,
          timestamp: new Date().toISOString(),
        };
        setMessages(prev => [...prev, assistantMessage]);

        return data.response;
      }
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to send message';
      setError(errorMsg);

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
  }, [isAvailable, agentMode]);

  // Clear chat history
  const clearHistory = useCallback(async () => {
    try {
      // Clear both histories
      await Promise.all([
        fetch(apiUrl('/api/assistant/history'), { method: 'DELETE' }),
        fetch(apiUrl('/api/agent/history'), { method: 'DELETE' }),
      ]);
      setMessages([]);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear history');
    }
  }, []);

  // Get a quick insight (uses simple assistant, not agent)
  const getInsight = useCallback(async (type: string = 'general'): Promise<string | null> => {
    if (!isAvailable) {
      setError('Assistant not available');
      return null;
    }

    setIsLoading(true);
    setError(null);

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
      const response = await fetch(apiUrl(`/api/assistant/insight?insight_type=${type}`));

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();

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
        // Try agent history first if in agent mode
        if (agentMode) {
          const response = await fetch(apiUrl('/api/agent/history'));
          if (response.ok) {
            const data = await response.json();
            if (data.history && Array.isArray(data.history) && data.history.length > 0) {
              setMessages(data.history);
              return;
            }
          }
        }

        // Fall back to assistant history
        const response = await fetch(apiUrl('/api/assistant/history'));
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
  }, [agentMode]);

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
    agentMode,
    setAgentMode,
    messages,
    sendMessage,
    clearHistory,
    getInsight,
    checkStatus,
  };
}

import React, { useState, useRef, useEffect, useMemo } from 'react';
import { useAssistant, ToolCall, ChatMessage } from '../hooks/useAssistant';
import type { AgentNotification } from '../types/alert';

interface AssistantPanelProps {
  isOpen: boolean;
  onToggle: () => void;
  agentNotifications?: AgentNotification[];
  onNavigateToCell?: (cellId: string) => void;
}

const ToolCallCard: React.FC<{ toolCall: ToolCall; index: number }> = ({ toolCall }) => {
  const [expanded, setExpanded] = useState(false);

  const statusIcon = toolCall.status === 'success' ? 'fa-check-circle' :
                     toolCall.status === 'error' ? 'fa-times-circle' :
                     'fa-spinner fa-spin';
  const statusColor = toolCall.status === 'success' ? '#4caf50' :
                      toolCall.status === 'error' ? '#f44336' :
                      '#ff9800';

  // Format tool name for display
  const displayName = toolCall.tool.replace(/_/g, ' ').replace(/\bget\b/i, '').trim();

  return (
    <div className="tool-call-card" onClick={() => setExpanded(!expanded)}>
      <div className="tool-call-header">
        <i className="fas fa-wrench" style={{ color: '#64b5f6', fontSize: '0.7em' }}></i>
        <span className="tool-call-name">{displayName}</span>
        <i className={`fas ${statusIcon}`} style={{ color: statusColor, marginLeft: 'auto', fontSize: '0.75em' }}></i>
        {toolCall.duration_ms !== undefined && toolCall.duration_ms > 0 && (
          <span className="tool-call-duration">{Math.round(toolCall.duration_ms)}ms</span>
        )}
        <i className={`fas fa-chevron-${expanded ? 'up' : 'down'}`} style={{ fontSize: '0.65em', opacity: 0.5 }}></i>
      </div>
      {expanded && (
        <div className="tool-call-details">
          {Object.keys(toolCall.arguments).length > 0 && (
            <div className="tool-call-args">
              <span className="tool-call-label">Args:</span>
              <code>{JSON.stringify(toolCall.arguments)}</code>
            </div>
          )}
          {toolCall.result && (
            <div className="tool-call-result">
              <span className="tool-call-label">Result:</span>
              <pre>{toolCall.result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// Merge chat messages and proactive notifications into a single time-sorted list.
// Proactive notifications get role 'proactive' so they render distinctly.
function mergeMessages(
  chatMessages: ChatMessage[],
  notifications: AgentNotification[],
): ChatMessage[] {
  const notifMessages: ChatMessage[] = notifications.map((n) => ({
    role: 'proactive' as ChatMessage['role'],
    content: n.content,
    timestamp: n.timestamp,
    cellIds: n.cells.map((c) => c['cell_id'] as string).filter(Boolean),
  }));
  return [...chatMessages, ...notifMessages].sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
  );
}

export const AssistantPanel: React.FC<AssistantPanelProps> = ({
  isOpen,
  onToggle,
  agentNotifications = [],
  onNavigateToCell,
}) => {
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Track how many notifications were seen when the panel was last open
  const seenCountRef = useRef(0);
  const unseenCount = agentNotifications.length - seenCountRef.current;

  // Mark all notifications as seen when the panel opens
  useEffect(() => {
    if (isOpen) {
      seenCountRef.current = agentNotifications.length;
    }
  }, [isOpen, agentNotifications.length]);

  const {
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
  } = useAssistant();

  // Unified message list: chat + proactive notifications, sorted by time
  const allMessages = useMemo(
    () => mergeMessages(messages, agentNotifications),
    [messages, agentNotifications],
  );

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [allMessages]);

  // Focus input when panel opens
  useEffect(() => {
    if (isOpen && inputRef.current) {
      inputRef.current.focus();
    }
  }, [isOpen]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputValue.trim() || isLoading) return;

    const message = inputValue.trim();
    setInputValue('');
    await sendMessage(message);
  };

  const handleQuickInsight = async (type: string) => {
    await getInsight(type);
  };

  const suggestedQuestions = agentMode ? [
    "Give me a full weather briefing",
    "What warnings are active right now?",
    "Are there any tornado or storm reports?",
    "What does the SPC outlook look like today?",
  ] : [
    "What warnings are active right now?",
    "Is there any tornado activity?",
    "What's the most severe threat?",
    "Any damaging wind or hail?",
  ];

  return (
    <>
      {/* Floating toggle button */}
      <button
        className={`assistant-toggle ${isOpen ? 'open' : ''} ${isAvailable ? 'available' : 'unavailable'}`}
        onClick={onToggle}
        title={isAvailable ? (agentMode ? 'Weather Agent' : 'Weather Assistant') : 'Assistant unavailable'}
      >
        <i className={`fas ${isOpen ? 'fa-times' : 'fa-robot'}`}></i>
        {!isOpen && isAvailable && unseenCount > 0 && (
          <span className="assistant-toggle-badge notification-badge">{unseenCount}</span>
        )}
        {!isOpen && isAvailable && unseenCount === 0 && (
          <span className="assistant-toggle-badge">{agentMode ? 'AGT' : 'AI'}</span>
        )}
      </button>

      {/* Assistant panel */}
      <div className={`assistant-panel ${isOpen ? 'open' : ''}`}>
        <div className="assistant-header">
          <div className="assistant-header-info">
            <h3>
              <i className="fas fa-robot"></i>
              {agentMode ? 'Weather Agent' : 'Weather Assistant'}
            </h3>
            <span className={`assistant-status ${isAvailable ? 'online' : 'offline'}`}>
              {isAvailable ? 'Online' : 'Offline'}
              {isAvailable && status?.model && (
                <span className="assistant-model-badge" title={status.model}>
                  {status.model}
                </span>
              )}
            </span>
          </div>
          <div className="assistant-header-actions">
            <button
              onClick={() => setAgentMode(!agentMode)}
              title={agentMode ? 'Switch to simple chat' : 'Switch to agent mode (tool calling)'}
              className={`agent-mode-toggle ${agentMode ? 'active' : ''}`}
            >
              <i className={`fas ${agentMode ? 'fa-tools' : 'fa-comment'}`}></i>
            </button>
            <button
              onClick={clearHistory}
              title="Clear chat history"
              disabled={messages.length === 0}
            >
              <i className="fas fa-trash-alt"></i>
            </button>
            <button onClick={onToggle} title="Close">
              <i className="fas fa-times"></i>
            </button>
          </div>
        </div>

        {!isAvailable && (
          <div className="assistant-unavailable">
            <i className="fas fa-exclamation-triangle"></i>
            <p>Assistant is unavailable</p>
            <small>
              {status?.message || 'Make sure Ollama is running with the required model'}
            </small>
          </div>
        )}

        {isAvailable && (
          <>
            <div className="assistant-messages">
              {allMessages.length === 0 && (
                <div className="assistant-welcome">
                  <div className="assistant-welcome-icon">
                    <i className={`fas ${agentMode ? 'fa-microchip' : 'fa-cloud-sun-rain'}`}></i>
                  </div>
                  <h4>{agentMode ? 'Weather Agent Ready' : 'Weather Assistant Ready'}</h4>
                  <p>
                    {agentMode
                      ? 'I can query live weather data using tools -- alerts, storm reports, SPC outlooks, wind gusts, and more. Ask me anything.'
                      : 'Ask me about active warnings, severe weather threats, or what\'s happening in your monitored areas.'
                    }
                  </p>
                  {agentMode && status?.tool_count && (
                    <div className="agent-tools-badge">
                      <i className="fas fa-tools"></i> {status.tool_count} tools available
                    </div>
                  )}
                  <div className="assistant-suggestions">
                    <span>Try asking:</span>
                    {suggestedQuestions.map((q, i) => (
                      <button
                        key={i}
                        onClick={() => {
                          setInputValue(q);
                          inputRef.current?.focus();
                        }}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {allMessages.map((msg, index) => (
                <div
                  key={index}
                  className={`assistant-message ${msg.role}`}
                >
                  {msg.role === 'user' && (
                    <div className="message-avatar user">
                      <i className="fas fa-user"></i>
                    </div>
                  )}
                  {msg.role === 'assistant' && (
                    <div className="message-avatar assistant">
                      <i className="fas fa-robot"></i>
                    </div>
                  )}
                  {msg.role === 'system' && (
                    <div className="message-avatar system">
                      <i className="fas fa-exclamation-circle"></i>
                    </div>
                  )}
                  {msg.role === 'proactive' && (
                    <div className="message-avatar proactive">
                      <i className="fas fa-bolt"></i>
                    </div>
                  )}
                  <div className="message-content">
                    {msg.role === 'proactive' && (
                      <div className="proactive-label">
                        <i className="fas fa-satellite-dish"></i> Storm Monitor
                      </div>
                    )}
                    {msg.role === 'proactive' && msg.cellIds && msg.cellIds.length > 0 && onNavigateToCell && (
                      <div className="proactive-cell-chips">
                        {msg.cellIds.map((id) => (
                          <button
                            key={id}
                            className="proactive-cell-chip"
                            onClick={() => onNavigateToCell(id)}
                            title="Jump to this cell on radar"
                          >
                            <i className="fas fa-crosshairs"></i>
                            {id}
                          </button>
                        ))}
                      </div>
                    )}
                    {/* Tool calls rendered before the response text */}
                    {msg.toolCalls && msg.toolCalls.length > 0 && (
                      <div className="tool-calls-container">
                        <div className="tool-calls-label">
                          <i className="fas fa-cogs"></i>
                          Used {msg.toolCalls.length} tool{msg.toolCalls.length > 1 ? 's' : ''}
                        </div>
                        {msg.toolCalls.map((tc, i) => (
                          <ToolCallCard key={i} toolCall={tc} index={i} />
                        ))}
                      </div>
                    )}
                    <div className="message-text">{msg.content}</div>
                    <div className="message-time">
                      {new Date(msg.timestamp).toLocaleTimeString()}
                    </div>
                  </div>
                </div>
              ))}

              {isLoading && (
                <div className="assistant-message assistant">
                  <div className="message-avatar assistant">
                    <i className="fas fa-robot"></i>
                  </div>
                  <div className="message-content">
                    <div className="message-text typing">
                      {agentMode ? (
                        <span className="agent-thinking">
                          <i className="fas fa-cogs fa-spin"></i> Agent is working...
                        </span>
                      ) : (
                        <>
                          <span></span>
                          <span></span>
                          <span></span>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {error && (
              <div className="assistant-error">
                <i className="fas fa-exclamation-triangle"></i>
                {error}
              </div>
            )}

            <div className="assistant-quick-actions">
              <button onClick={() => sendMessage("What warnings are active?")} disabled={isLoading}>
                <i className="fas fa-exclamation-triangle"></i> Active Alerts
              </button>
              <button onClick={() => sendMessage("Summarize the most dangerous threats")} disabled={isLoading}>
                <i className="fas fa-bolt"></i> Top Threats
              </button>
              {agentMode ? (
                <button onClick={() => sendMessage("Give me a full weather briefing")} disabled={isLoading}>
                  <i className="fas fa-clipboard-list"></i> Briefing
                </button>
              ) : (
                <button onClick={() => handleQuickInsight('safety')} disabled={isLoading}>
                  <i className="fas fa-shield-alt"></i> Safety
                </button>
              )}
            </div>

            <form className="assistant-input" onSubmit={handleSubmit}>
              <input
                ref={inputRef}
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder={agentMode ? "Ask the agent..." : "Ask about weather..."}
                disabled={isLoading}
              />
              <button type="submit" disabled={!inputValue.trim() || isLoading}>
                <i className={`fas ${isLoading ? 'fa-spinner fa-spin' : 'fa-paper-plane'}`}></i>
              </button>
            </form>
          </>
        )}
      </div>
    </>
  );
};

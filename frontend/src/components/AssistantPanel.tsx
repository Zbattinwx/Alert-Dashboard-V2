import React, { useState, useRef, useEffect } from 'react';
import { useAssistant } from '../hooks/useAssistant';

interface AssistantPanelProps {
  isOpen: boolean;
  onToggle: () => void;
}

export const AssistantPanel: React.FC<AssistantPanelProps> = ({ isOpen, onToggle }) => {
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const {
    status,
    isAvailable,
    isLoading,
    error,
    messages,
    sendMessage,
    clearHistory,
    getInsight,
  } = useAssistant();

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

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
    const insight = await getInsight(type);
    if (insight) {
      // Display insight as assistant message
      // The sendMessage already handles adding to messages, so we'll use it
    }
  };

  const suggestedQuestions = [
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
        title={isAvailable ? 'Weather Assistant' : 'Assistant unavailable'}
      >
        <i className={`fas ${isOpen ? 'fa-times' : 'fa-robot'}`}></i>
        {!isOpen && isAvailable && (
          <span className="assistant-toggle-badge">AI</span>
        )}
      </button>

      {/* Assistant panel */}
      <div className={`assistant-panel ${isOpen ? 'open' : ''}`}>
        <div className="assistant-header">
          <div className="assistant-header-info">
            <h3>
              <i className="fas fa-robot"></i>
              Weather Assistant
            </h3>
            <span className={`assistant-status ${isAvailable ? 'online' : 'offline'}`}>
              {isAvailable ? 'Online' : 'Offline'}
            </span>
          </div>
          <div className="assistant-header-actions">
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
              {status?.message || 'Make sure Ollama is running with the gemma3:4b model'}
            </small>
          </div>
        )}

        {isAvailable && (
          <>
            <div className="assistant-messages">
              {messages.length === 0 && (
                <div className="assistant-welcome">
                  <div className="assistant-welcome-icon">
                    <i className="fas fa-cloud-sun-rain"></i>
                  </div>
                  <h4>Weather Assistant Ready</h4>
                  <p>
                    Ask me about active warnings, severe weather threats,
                    or what's happening in your monitored areas.
                  </p>
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

              {messages.map((msg, index) => (
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
                  <div className="message-content">
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
                      <span></span>
                      <span></span>
                      <span></span>
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
              <button onClick={() => handleQuickInsight('safety')} disabled={isLoading}>
                <i className="fas fa-shield-alt"></i> Safety
              </button>
            </div>

            <form className="assistant-input" onSubmit={handleSubmit}>
              <input
                ref={inputRef}
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder="Ask about weather..."
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

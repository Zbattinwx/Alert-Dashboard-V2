import React from 'react';

interface NavItem {
  id: string;
  label: string;
  icon: string;
  external?: boolean;
  href?: string;
}

const navItems: NavItem[] = [
  { id: 'alerts', label: 'Active Alerts', icon: 'fa-exclamation-triangle' },
  { id: 'map', label: 'Alert Map', icon: 'fa-map-marked-alt' },
  { id: 'lsr', label: 'Storm Reports', icon: 'fa-bullhorn' },
  { id: 'spc', label: 'SPC Outlooks', icon: 'fa-cloud-sun-rain' },
  { id: 'md', label: 'Mesoscale Discussions', icon: 'fa-file-alt' },
  { id: 'afd', label: 'Forecast Discussions', icon: 'fa-book-open' },
  { id: 'gusts', label: 'Top Wind Gusts', icon: 'fa-wind' },
  { id: 'snow-emergency', label: 'Snow Emergencies', icon: 'fa-car-crash' },
  { id: 'nwws-feed', label: 'NWWS Products', icon: 'fa-rss' },
  { id: 'daily-recap', label: 'Daily Recap', icon: 'fa-calendar-day' },
  { id: 'settings', label: 'Settings', icon: 'fa-sliders-h' },
];

interface SidebarProps {
  activeSection: string;
  onSectionChange: (section: string) => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  activeSection,
  onSectionChange,
}) => {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <h2 style={{ fontSize: '1.1rem', color: 'var(--accent-blue)' }}>
          <i className="fas fa-broadcast-tower" style={{ marginRight: '8px' }}></i>
          Weather Dashboard
        </h2>
      </div>

      <nav className="sidebar-nav">
        {navItems.map((item) => (
          <div
            key={item.id}
            className={`nav-item ${activeSection === item.id ? 'active' : ''}`}
            onClick={() => {
              if (item.external && item.href) {
                window.open(item.href, '_blank');
              } else {
                onSectionChange(item.id);
              }
            }}
          >
            <i className={`fas ${item.icon}`}></i>
            <span>{item.label}</span>
            {item.external && (
              <i
                className="fas fa-external-link-alt"
                style={{ marginLeft: 'auto', fontSize: '10px' }}
              ></i>
            )}
          </div>
        ))}
      </nav>

      <div style={{
        padding: 'var(--spacing-md)',
        borderTop: '1px solid var(--border-color)',
        fontSize: '0.75rem',
        color: 'var(--text-muted)'
      }}>
        Alert Dashboard V2
      </div>
    </aside>
  );
};

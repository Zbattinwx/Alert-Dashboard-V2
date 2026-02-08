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
  { id: 'odot', label: 'ODOT Cameras', icon: 'fa-road' },
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
      <div className="sidebar-header" style={{ flexDirection: 'column', gap: '8px' }}>
        <img
          src={`${import.meta.env.BASE_URL}tbf_logo.png`}
          alt="The Battin Front"
          style={{
            maxWidth: '160px',
            height: 'auto',
          }}
        />
        <span style={{
          fontSize: '0.75rem',
          color: 'var(--text-muted)',
          letterSpacing: '0.5px'
        }}>
          Alert Dashboard
        </span>
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
        fontSize: '0.7rem',
        color: 'var(--text-muted)',
        textAlign: 'center'
      }}>
        <a
          href="https://www.thebattinfront.com"
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: 'var(--text-muted)', textDecoration: 'none' }}
        >
          thebattinfront.com
        </a>
      </div>
    </aside>
  );
};

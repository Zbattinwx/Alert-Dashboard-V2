import React, { useState, useEffect, useCallback } from 'react';
import type { MesoscaleDiscussion } from '../types/spc';

interface NewMDNotificationProps {
  md: MesoscaleDiscussion | null;
  onDismiss?: () => void;
  duration?: number;
}

export const NewMDNotification: React.FC<NewMDNotificationProps> = ({
  md,
  onDismiss,
  duration = 15000,
}) => {
  const [visible, setVisible] = useState(false);
  const [currentMD, setCurrentMD] = useState<MesoscaleDiscussion | null>(null);

  const dismiss = useCallback(() => {
    setVisible(false);
    setTimeout(() => {
      setCurrentMD(null);
      onDismiss?.();
    }, 700);
  }, [onDismiss]);

  useEffect(() => {
    if (md && md.md_number !== currentMD?.md_number) {
      setCurrentMD(md);
      requestAnimationFrame(() => {
        setVisible(true);
      });

      const timer = setTimeout(dismiss, duration);
      return () => clearTimeout(timer);
    }
  }, [md, currentMD, duration, dismiss]);

  if (!currentMD) return null;

  // Blue-ish color for SPC MD
  const alertStyle = {
    backgroundColor: '#3a6a90', // Steel Blue
    borderColor: '#244560',
  };

  return (
    <div
      className={`new-alert-notification ${visible ? 'visible' : ''}`}
      style={{
        borderLeftColor: alertStyle.borderColor,
        ['--alert-bg' as string]: alertStyle.backgroundColor,
      }}
      onClick={dismiss}
    >
      <div className="new-alert-header">
        <span className="new-alert-badge">NEW MD</span>
        <span className="new-alert-title">Mesoscale Discussion #{currentMD.md_number}</span>
      </div>
      <div className="new-alert-body">
        <div className="new-alert-location">
          <i className="fas fa-map-marker-alt"></i>
          <span>{currentMD.affected_states.join(', ') || 'See Text'}</span>
        </div>
        <div className="new-alert-threat">
           <i className="fas fa-info-circle"></i>
           {currentMD.title.length > 50 ? currentMD.title.substring(0, 50) + '...' : currentMD.title}
        </div>
      </div>
      <div className="new-alert-dismiss">
        Click to dismiss
      </div>
    </div>
  );
};

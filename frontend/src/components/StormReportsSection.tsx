import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { MapContainer, TileLayer, CircleMarker, Popup, useMap, useMapEvents } from 'react-leaflet';
import L from 'leaflet';
import type { StormReport, LSRResponse, ViewerReportSubmission } from '../types/lsr';
import { LSR_TYPE_COLORS, getTextColorForBackground } from '../types/lsr';
import 'leaflet/dist/leaflet.css';

interface StormReportsSectionProps {
  // Optional callback when a report is selected
  onReportSelect?: (report: StormReport) => void;
}

// Component to fit map bounds to reports
const FitBoundsToReports: React.FC<{ reports: StormReport[] }> = ({ reports }) => {
  const map = useMap();

  useEffect(() => {
    if (reports.length === 0) return;

    const validReports = reports.filter(r => r.lat && r.lon);
    if (validReports.length === 0) return;

    const bounds = L.latLngBounds(
      validReports.map(r => [r.lat, r.lon] as L.LatLngExpression)
    );

    map.fitBounds(bounds, { padding: [50, 50] });
  }, [reports, map]);

  return null;
};

// Component to handle map clicks for location selection
const LocationPicker: React.FC<{
  onLocationSelect: (lat: number, lon: number) => void;
  isActive: boolean;
}> = ({ onLocationSelect, isActive }) => {
  useMapEvents({
    click: (e) => {
      if (isActive) {
        onLocationSelect(e.latlng.lat, e.latlng.lng);
      }
    },
  });
  return null;
};

// Format timestamp for display
const formatTime = (isoString: string | null): string => {
  if (!isoString) return 'Unknown';
  const date = new Date(isoString);
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
};

// Report types for the add form
const REPORT_TYPES = [
  { value: 'TORNADO', label: 'Tornado', icon: 'fa-tornado' },
  { value: 'HAIL', label: 'Hail', icon: 'fa-cloud-meatball' },
  { value: 'TSTM WND GST', label: 'Wind', icon: 'fa-wind' },
  { value: 'FLASH FLOOD', label: 'Flooding', icon: 'fa-house-flood-water' },
  { value: 'SNOW', label: 'Snow', icon: 'fa-snowflake' },
  { value: 'OTHER', label: 'Other', icon: 'fa-exclamation-circle' },
];

// Viewer report color
const VIEWER_REPORT_COLOR = '#bb9af7';

export const StormReportsSection: React.FC<StormReportsSectionProps> = ({
  onReportSelect,
}) => {
  const [reports, setReports] = useState<StormReport[]>([]);
  const [typeColors, setTypeColors] = useState<Record<string, string>>(LSR_TYPE_COLORS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState(24);
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [selectedReport, setSelectedReport] = useState<StormReport | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [viewerCount, setViewerCount] = useState(0);

  // Add report modal state
  const [showAddModal, setShowAddModal] = useState(false);
  const [isPickingLocation, setIsPickingLocation] = useState(false);
  const [newReport, setNewReport] = useState<ViewerReportSubmission>({
    report_type: 'HAIL',
    lat: 0,
    lon: 0,
    magnitude: '',
    remarks: '',
    location: '',
    submitter: '',
  });
  const [submitting, setSubmitting] = useState(false);

  // Default center: Ohio region
  const defaultCenter: L.LatLngExpression = [39.9612, -82.9988];
  const defaultZoom = 7;

  // Fetch reports from API
  const fetchReports = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        hours: hours.toString(),
        refresh: refresh.toString(),
      });

      // Use /api/lsr/all to get both official and viewer reports
      const response = await fetch(`/api/lsr/all?${params}`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data: LSRResponse = await response.json();
      setReports(data.reports);
      setTypeColors(data.type_colors);
      setViewerCount(data.viewer_count || 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch reports');
    } finally {
      setLoading(false);
    }
  }, [hours]);

  // Initial fetch and periodic refresh
  useEffect(() => {
    fetchReports();

    // Refresh every 5 minutes
    const interval = setInterval(() => fetchReports(), 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [fetchReports]);

  // Filter reports by selected type
  const filteredReports = useMemo(() => {
    if (!selectedType) return reports;
    if (selectedType === 'VIEWER') {
      return reports.filter(r => r.is_viewer);
    }
    return reports.filter(r => r.report_type === selectedType);
  }, [reports, selectedType]);

  // Get unique report types with counts
  const reportTypeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    reports.forEach(r => {
      counts[r.report_type] = (counts[r.report_type] || 0) + 1;
    });
    return counts;
  }, [reports]);

  // Handle report click
  const handleReportClick = (report: StormReport) => {
    setSelectedReport(report);
    onReportSelect?.(report);
  };

  // Handle location selection from map
  const handleLocationSelect = (lat: number, lon: number) => {
    setNewReport(prev => ({ ...prev, lat, lon }));
    setIsPickingLocation(false);
  };

  // Submit new viewer report
  const handleSubmitReport = async () => {
    if (!newReport.lat || !newReport.lon) {
      alert('Please select a location on the map');
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch('/api/lsr/viewer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newReport),
      });

      if (!response.ok) {
        throw new Error('Failed to submit report');
      }

      // Reset form and close modal
      setNewReport({
        report_type: 'HAIL',
        lat: 0,
        lon: 0,
        magnitude: '',
        remarks: '',
        location: '',
        submitter: '',
      });
      setShowAddModal(false);

      // Refresh reports
      fetchReports(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to submit report');
    } finally {
      setSubmitting(false);
    }
  };

  // Remove viewer report
  const handleRemoveReport = async (reportId: string) => {
    if (!confirm('Are you sure you want to remove this report?')) return;

    try {
      const response = await fetch(`/api/lsr/viewer/${reportId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to remove report');
      }

      // Refresh reports
      fetchReports(true);
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to remove report');
    }
  };

  // Get marker color - purple for viewer reports
  const getMarkerColor = (report: StormReport): string => {
    if (report.is_viewer) return VIEWER_REPORT_COLOR;
    return typeColors[report.report_type] || '#FFFFFF';
  };

  return (
    <div className="lsr-section">
      {/* Controls */}
      <div className="lsr-controls">
        <div className="lsr-controls-left">
          <label>
            <span>Time Range:</span>
            <select
              value={hours}
              onChange={(e) => setHours(Number(e.target.value))}
              className="lsr-select"
            >
              <option value={6}>Last 6 hours</option>
              <option value={12}>Last 12 hours</option>
              <option value={24}>Last 24 hours</option>
              <option value={48}>Last 48 hours</option>
              <option value={72}>Last 3 days</option>
              <option value={168}>Last 7 days</option>
            </select>
          </label>
          <button
            onClick={() => fetchReports(true)}
            className="lsr-refresh-btn"
            disabled={loading}
          >
            <i className={`fas fa-sync-alt ${loading ? 'fa-spin' : ''}`}></i>
            Refresh
          </button>
          <button
            onClick={() => setShowAddModal(true)}
            className="lsr-add-btn"
          >
            <i className="fas fa-plus"></i>
            Add Report
          </button>
        </div>
        <div className="lsr-stats">
          <span className="lsr-count">{reports.length} Reports</span>
          {viewerCount > 0 && (
            <span className="lsr-viewer-count">
              <i className="fas fa-eye"></i> {viewerCount} Viewer
            </span>
          )}
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="lsr-error">
          <i className="fas fa-exclamation-triangle"></i>
          Error loading reports: {error}
        </div>
      )}

      {/* Type filters */}
      <div className="lsr-type-filters">
        <button
          className={`lsr-type-btn ${selectedType === null ? 'active' : ''}`}
          onClick={() => setSelectedType(null)}
        >
          All ({reports.length})
        </button>
        {viewerCount > 0 && (
          <button
            className={`lsr-type-btn ${selectedType === 'VIEWER' ? 'active' : ''}`}
            style={{
              backgroundColor: selectedType === 'VIEWER' ? VIEWER_REPORT_COLOR : undefined,
              color: selectedType === 'VIEWER' ? '#000' : undefined,
              borderColor: VIEWER_REPORT_COLOR,
            }}
            onClick={() => setSelectedType(selectedType === 'VIEWER' ? null : 'VIEWER')}
          >
            <i className="fas fa-eye"></i> Viewer ({viewerCount})
          </button>
        )}
        {Object.entries(reportTypeCounts)
          .sort((a, b) => b[1] - a[1])
          .map(([type, count]) => (
            <button
              key={type}
              className={`lsr-type-btn ${selectedType === type ? 'active' : ''}`}
              style={{
                backgroundColor: selectedType === type ? (typeColors[type] || '#666') : undefined,
                color: selectedType === type ? getTextColorForBackground(typeColors[type] || '#666') : undefined,
                borderColor: typeColors[type] || '#666',
              }}
              onClick={() => setSelectedType(selectedType === type ? null : type)}
            >
              {type} ({count})
            </button>
          ))}
      </div>

      {/* Main content area */}
      <div className="lsr-content">
        {/* Map */}
        <div className="lsr-map-container">
          {isPickingLocation && (
            <div className="lsr-location-picker-overlay">
              <div className="lsr-location-picker-message">
                <i className="fas fa-map-marker-alt"></i>
                Click on the map to select location
                <button onClick={() => setIsPickingLocation(false)}>Cancel</button>
              </div>
            </div>
          )}
          <MapContainer
            center={defaultCenter}
            zoom={defaultZoom}
            style={{ height: '100%', width: '100%', borderRadius: 'var(--radius-md)' }}
            whenReady={() => setMapReady(true)}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            />

            {mapReady && <FitBoundsToReports reports={filteredReports} />}
            <LocationPicker
              isActive={isPickingLocation}
              onLocationSelect={handleLocationSelect}
            />

            {/* Render report markers */}
            {filteredReports.map((report) => (
              <CircleMarker
                key={report.id}
                center={[report.lat, report.lon]}
                radius={report.is_viewer ? 10 : 8}
                pathOptions={{
                  color: getMarkerColor(report),
                  fillColor: getMarkerColor(report),
                  fillOpacity: 0.8,
                  weight: selectedReport?.id === report.id ? 3 : (report.is_viewer ? 2 : 1),
                }}
                eventHandlers={{
                  click: () => handleReportClick(report),
                }}
              >
                <Popup>
                  <div className="lsr-popup">
                    <h4 style={{ color: getMarkerColor(report) }}>
                      {report.is_viewer && <i className="fas fa-eye" style={{ marginRight: '6px' }}></i>}
                      {report.report_type}
                      {report.is_viewer && <span className="lsr-viewer-badge">Viewer Report</span>}
                    </h4>
                    {report.magnitude && (
                      <p className="lsr-popup-magnitude">{report.magnitude}</p>
                    )}
                    <p className="lsr-popup-location">
                      {report.is_viewer && report.location_text
                        ? report.location_text
                        : `${report.city}, ${report.county} County, ${report.state}`}
                    </p>
                    <p className="lsr-popup-time">
                      <strong>Time:</strong> {formatTime(report.valid_time)}
                    </p>
                    {report.remark && (
                      <p className="lsr-popup-remark">{report.remark}</p>
                    )}
                    <p className="lsr-popup-source">
                      {report.is_viewer
                        ? `Submitted by: ${report.submitter || 'Anonymous'}`
                        : `Source: ${report.source} | WFO: ${report.wfo}`}
                    </p>
                    {report.is_viewer && (
                      <button
                        className="lsr-popup-remove-btn"
                        onClick={() => handleRemoveReport(report.id)}
                      >
                        <i className="fas fa-trash"></i> Remove
                      </button>
                    )}
                  </div>
                </Popup>
              </CircleMarker>
            ))}

            {/* Show selected location for new report */}
            {newReport.lat !== 0 && newReport.lon !== 0 && showAddModal && (
              <CircleMarker
                center={[newReport.lat, newReport.lon]}
                radius={12}
                pathOptions={{
                  color: '#fff',
                  fillColor: typeColors[newReport.report_type] || VIEWER_REPORT_COLOR,
                  fillOpacity: 0.9,
                  weight: 3,
                }}
              />
            )}
          </MapContainer>

          {/* No reports message */}
          {!loading && filteredReports.length === 0 && (
            <div className="lsr-no-reports">
              <i className="fas fa-cloud-sun"></i>
              <p>No storm reports in the selected time range</p>
            </div>
          )}
        </div>

        {/* Reports list */}
        <div className="lsr-list">
          <h3 className="lsr-list-title">
            Recent Reports
            {selectedType && <span className="lsr-list-filter"> - {selectedType}</span>}
          </h3>
          <div className="lsr-list-items">
            {loading ? (
              <div className="lsr-loading">
                <i className="fas fa-spinner fa-spin"></i>
                Loading reports...
              </div>
            ) : filteredReports.length === 0 ? (
              <div className="lsr-empty">No reports found</div>
            ) : (
              filteredReports.slice(0, 50).map((report) => (
                <div
                  key={report.id}
                  className={`lsr-list-item ${selectedReport?.id === report.id ? 'selected' : ''} ${report.is_viewer ? 'viewer' : ''}`}
                  onClick={() => handleReportClick(report)}
                >
                  <div
                    className="lsr-list-item-indicator"
                    style={{ backgroundColor: getMarkerColor(report) }}
                  >
                    {report.is_viewer && <i className="fas fa-eye"></i>}
                  </div>
                  <div className="lsr-list-item-content">
                    <div className="lsr-list-item-header">
                      <span className="lsr-list-item-type">
                        {report.is_viewer && <span className="lsr-viewer-tag">VIEWER</span>}
                        {report.report_type}
                      </span>
                      {report.magnitude && (
                        <span className="lsr-list-item-magnitude">{report.magnitude}</span>
                      )}
                    </div>
                    <div className="lsr-list-item-location">
                      {report.is_viewer && report.location_text
                        ? report.location_text
                        : `${report.city}, ${report.county} Co.`}
                    </div>
                    <div className="lsr-list-item-time">
                      {formatTime(report.valid_time)}
                      {report.is_viewer && (
                        <span className="lsr-list-item-submitter">
                          by {report.submitter || 'Anonymous'}
                        </span>
                      )}
                    </div>
                  </div>
                  {report.is_viewer && (
                    <button
                      className="lsr-list-item-remove"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleRemoveReport(report.id);
                      }}
                    >
                      <i className="fas fa-times"></i>
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Add Report Modal */}
      {showAddModal && (
        <div className="lsr-modal-overlay" onClick={() => setShowAddModal(false)}>
          <div className="lsr-modal" onClick={(e) => e.stopPropagation()}>
            <div className="lsr-modal-header">
              <h3><i className="fas fa-plus-circle"></i> Add Storm Report</h3>
              <button className="lsr-modal-close" onClick={() => setShowAddModal(false)}>
                <i className="fas fa-times"></i>
              </button>
            </div>
            <div className="lsr-modal-body">
              {/* Report Type */}
              <div className="lsr-form-group">
                <label>Report Type</label>
                <div className="lsr-type-grid">
                  {REPORT_TYPES.map((type) => (
                    <button
                      key={type.value}
                      className={`lsr-type-option ${newReport.report_type === type.value ? 'selected' : ''}`}
                      style={{
                        borderColor: typeColors[type.value] || VIEWER_REPORT_COLOR,
                        backgroundColor: newReport.report_type === type.value
                          ? typeColors[type.value] || VIEWER_REPORT_COLOR
                          : undefined,
                        color: newReport.report_type === type.value
                          ? getTextColorForBackground(typeColors[type.value] || VIEWER_REPORT_COLOR)
                          : undefined,
                      }}
                      onClick={() => setNewReport(prev => ({ ...prev, report_type: type.value }))}
                    >
                      <i className={`fas ${type.icon}`}></i>
                      {type.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Location */}
              <div className="lsr-form-group">
                <label>Location</label>
                <div className="lsr-location-input">
                  <input
                    type="text"
                    placeholder="Enter location description"
                    value={newReport.location || ''}
                    onChange={(e) => setNewReport(prev => ({ ...prev, location: e.target.value }))}
                  />
                  <button
                    className={`lsr-pick-location-btn ${isPickingLocation ? 'active' : ''}`}
                    onClick={() => setIsPickingLocation(!isPickingLocation)}
                  >
                    <i className="fas fa-map-marker-alt"></i>
                    {newReport.lat && newReport.lon
                      ? `${newReport.lat.toFixed(4)}, ${newReport.lon.toFixed(4)}`
                      : 'Pick on Map'}
                  </button>
                </div>
              </div>

              {/* Magnitude */}
              <div className="lsr-form-group">
                <label>Magnitude (optional)</label>
                <input
                  type="text"
                  placeholder="e.g., 1.00 INCH, 65 MPH, 6 inches"
                  value={newReport.magnitude || ''}
                  onChange={(e) => setNewReport(prev => ({ ...prev, magnitude: e.target.value }))}
                />
              </div>

              {/* Remarks */}
              <div className="lsr-form-group">
                <label>Details / Remarks (optional)</label>
                <textarea
                  placeholder="Describe what you observed..."
                  value={newReport.remarks || ''}
                  onChange={(e) => setNewReport(prev => ({ ...prev, remarks: e.target.value }))}
                  rows={3}
                />
              </div>

              {/* Submitter Name */}
              <div className="lsr-form-group">
                <label>Your Name (optional)</label>
                <input
                  type="text"
                  placeholder="Anonymous"
                  value={newReport.submitter || ''}
                  onChange={(e) => setNewReport(prev => ({ ...prev, submitter: e.target.value }))}
                />
              </div>
            </div>
            <div className="lsr-modal-footer">
              <button
                className="lsr-modal-cancel"
                onClick={() => setShowAddModal(false)}
              >
                Cancel
              </button>
              <button
                className="lsr-modal-submit"
                onClick={handleSubmitReport}
                disabled={submitting || !newReport.lat || !newReport.lon}
              >
                {submitting ? (
                  <>
                    <i className="fas fa-spinner fa-spin"></i> Submitting...
                  </>
                ) : (
                  <>
                    <i className="fas fa-paper-plane"></i> Submit Report
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

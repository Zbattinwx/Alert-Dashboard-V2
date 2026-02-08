# Alert Dashboard V2 - Development Session History

**Last Updated:** December 21, 2025 (Session 7 - Ticker Widgets & Wind Gusts)
**Project:** Alert Dashboard V2 - Complete Rewrite from V1

---

## Project Overview

Rewriting Alert Dashboard V1 (PyQt5-based desktop app) to V2 (FastAPI backend + React frontend). The goal is a modern, web-based architecture that maintains all V1 features while improving maintainability and scalability.

---

## CURRENT STATUS

### Backend + Frontend - CORE COMPLETE!

**ALL CORE BACKEND SERVICES COMPLETED:**
1. NWS API Client (`backend/services/nws_api_client.py`) - ✅ DONE
2. NWWS-OI Client (`backend/services/nwws_client.py`) - ✅ DONE
3. Alert Manager (`backend/services/alert_manager.py`) - ✅ DONE
4. WebSocket Message Broker (`backend/services/message_broker.py`) - ✅ DONE
5. Zone Geometry Service (`backend/services/zone_geometry_service.py`) - ✅ DONE
6. Main Application Entry Point (`backend/main.py`) - ✅ DONE

**FRONTEND COMPLETED:**
1. React app with TypeScript + Vite - ✅ DONE
2. Alert list/grid component with filtering - ✅ DONE
3. Alert counter bar - ✅ DONE
4. Alert map with Leaflet - ✅ DONE
5. WebSocket hook for real-time updates - ✅ DONE
6. Alert detail slide-out pane - ✅ DONE
7. Sidebar navigation - ✅ DONE
8. Static file serving from FastAPI - ✅ DONE

**BUG FIXES COMPLETED (Session 5):**
1. ~~Wire UGC service to populate `display_locations` in alert API responses~~ - DONE
2. ~~Fix threat parsing (wind showing blank, hail showing incorrect values)~~ - DONE
3. ~~Extract snow amounts from alert descriptions~~ - DONE
4. ~~Fix issuing office showing "Unknown"~~ - DONE

**BUG FIXES COMPLETED (Session 6):**
1. ~~Fix AlertDetailPane z-index (opening behind map)~~ - DONE
2. ~~Fix hail/snow parsing confusion ("1 inch of quick snow" detected as hail)~~ - DONE

**NEW FEATURES (Session 6):**
1. Local Storm Reports (LSR) section - ✅ DONE
   - Backend LSR service fetching from Iowa State Mesonet API
   - API endpoints: `/api/lsr`, `/api/lsr/stats`, `/api/lsr/types`
   - Frontend StormReportsSection component with map and list view
   - Type filtering, time range selection, auto-refresh

2. Viewer Storm Reports - ✅ DONE
   - Dashboard users can manually add storm reports via "Add Report" button
   - Map click to select location, report type grid, magnitude/remarks fields
   - Viewer reports displayed with purple eye icon to distinguish from official
   - Viewer reports can be removed individually
   - Website submission endpoint: `POST /api/submit_storm_report` (for belparkmedia.com form)
   - State filtering: Official reports filtered by state settings, viewer reports always shown
   - API endpoints: `/api/lsr/all`, `/api/lsr/viewer`, `DELETE /api/lsr/viewer/{id}`

**NEW FEATURES (Session 7):**
1. Streaming Ticker Widgets - ✅ DONE
   - Non-sponsored ticker: `/widgets/ticker.html` - Full-width alert ticker for live streams
   - Sponsored ticker: `/widgets/ticker-sponsored.html` - Same with sponsor slot
   - Real-time alerts via WebSocket connection
   - Multiple themes: classic, atmospheric, storm-chaser, meteorologist, winter
   - Smart rotation (waits for scroll animation to complete)
   - Key details subtitle showing wind/hail/impact information

2. Wind Gusts Section - ✅ DONE
   - Backend service fetching from Iowa State Mesonet ASOS stations
   - API endpoints: `/api/wind-gusts`, `/api/wind-gusts/by-state`, `/api/wind-gusts/stats`
   - Frontend WindGustsSection component with state-organized grid
   - Severity color coding (significant=red, severe=orange, advisory=yellow)
   - "Highest Gust" highlight card

**NEXT STEPS:**
1. Add remaining dashboard sections (Mesoscale Discussions, Area Forecast Discussions)
2. Add audio alerts for new warnings
3. Add settings panel for customization
4. Test with NWWS-OI credentials for real-time streaming

---

## What Each Completed File Contains

### 1. NWS API Client (`backend/services/nws_api_client.py`)
- Async HTTP client using `httpx`
- Retry logic with `tenacity` (exponential backoff)
- Rate limiting (min 1 second between requests)
- Methods: `get_active_alerts()`, `get_alert_by_id()`, `get_zone_geometry()`, `get_county_geometry()`, `fetch_and_parse_alerts()`
- Singleton pattern with `get_nws_client()`

### 2. NWWS-OI Client (`backend/services/nwws_client.py`)
- XMPP client using `slixmpp` for Weather Wire real-time streaming
- Auto-reconnect with exponential backoff
- MUC (Multi-User Chat) room joining
- Callback system for alerts: `add_alert_callback()`, `add_raw_callback()`
- High-level `NWWSAlertHandler` wrapper class
- Singleton: `get_nwws_handler()`

### 3. Alert Manager (`backend/services/alert_manager.py`)
- Central alert state management
- Deduplication by product_id
- Automatic expiration cleanup (background task)
- Persistence to JSON file
- Callback system: `on_alert_added()`, `on_alert_updated()`, `on_alert_removed()`, `on_alerts_changed()`
- Methods: `add_alert()`, `remove_alert()`, `get_alerts_sorted()`, `get_alerts_by_phenomenon()`, `get_alerts_by_state()`, `get_statistics()`
- Singleton: `get_alert_manager()`

### 4. WebSocket Message Broker (`backend/services/message_broker.py`)
- FastAPI WebSocket connection management
- Message types: ALERT_NEW, ALERT_UPDATE, ALERT_REMOVE, ALERT_BULK, SYSTEM_STATUS, etc.
- Client subscriptions (filter by topic)
- Broadcast methods: `broadcast_alert_new()`, `broadcast_alert_update()`, `broadcast_alert_remove()`, `broadcast_alerts_bulk()`
- Ping/pong for connection health
- Singleton: `get_message_broker()`

### 5. Zone Geometry Service (`backend/services/zone_geometry_service.py`)
- Fetches and caches zone boundary geometries from NWS API
- In-memory cache with TTL (24 hours default)
- Disk persistence (`zone_geometry_cache.json`)
- Support for forecast zones (OHZ049) AND counties (OHC049)
- Parallel fetching with `fetch_multiple_zones()`
- Methods: `fetch_zone_geometry()`, `populate_alert_geometry()`, `populate_multiple_alerts()`
- Converts GeoJSON [lon, lat] to Leaflet [lat, lon] format
- Singleton: `get_zone_geometry_service()`

### 6. Main Application (`backend/main.py`)
- FastAPI app with lifespan context manager
- CORS middleware configured
- Service lifecycle management (startup/shutdown)
- REST API endpoints:
  - `GET /` - Server info
  - `GET /health` - Health check
  - `GET /api/alerts` - Get all alerts (with filters)
  - `GET /api/alerts/{product_id}` - Get specific alert
  - `GET /api/stats` - Alert statistics
  - `GET /api/recent` - Recent products
  - `GET /api/status` - Detailed system status
- WebSocket endpoint: `WS /ws` - Real-time updates
- Wires Alert Manager callbacks to WebSocket broadcasts
- Background API polling loop (5 min default)
- Initial alert fetch on startup

---

## Project Structure (Current)

```
Alert Dashboard V2/
├── CHAT_HISTORY.md               # THIS FILE - Development progress
├── Alert Dashboard V2/           # V2 Project Root
│   ├── requirements.txt          # Python dependencies
│   ├── backend/
│   │   ├── __init__.py
│   │   ├── main.py               # FastAPI app + static serving
│   │   ├── config/
│   │   │   ├── __init__.py
│   │   │   ├── settings.py       # Pydantic settings
│   │   │   └── branding.py       # Alert type styling
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   └── alert.py          # Alert dataclass
│   │   ├── parsers/
│   │   │   ├── __init__.py
│   │   │   ├── patterns.py       # Regex patterns
│   │   │   ├── vtec_parser.py    # VTEC parsing
│   │   │   ├── ugc_parser.py     # UGC zone parsing
│   │   │   ├── threat_parser.py  # Tornado/wind threat
│   │   │   └── alert_parser.py   # Main parser
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── nws_api_client.py
│   │   │   ├── nwws_client.py
│   │   │   ├── alert_manager.py
│   │   │   ├── message_broker.py
│   │   │   ├── zone_geometry_service.py
│   │   │   ├── ugc_service.py        # UGC to county name mapping
│   │   │   ├── lsr_service.py        # Local Storm Reports service
│   │   │   ├── odot_service.py       # ODOT cameras/sensors
│   │   │   ├── spc_service.py        # SPC outlooks
│   │   │   └── wind_gusts_service.py # Wind gusts from ASOS
│   │   ├── data/
│   │   │   └── ugc_map.json          # UGC code lookup data
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── timezone.py
│   │       └── logging.py
│   ├── frontend/                  # React Frontend
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── tsconfig.json
│   │   ├── index.html
│   │   ├── dist/                 # Built production files
│   │   └── src/
│   │       ├── main.tsx
│   │       ├── App.tsx
│   │       ├── types/
│   │       │   ├── alert.ts
│   │       │   ├── lsr.ts            # Storm report types
│   │       │   └── odot.ts           # ODOT types
│   │       ├── hooks/useWebSocket.ts
│   │       ├── components/
│   │       │   ├── Sidebar.tsx
│   │       │   ├── CounterBar.tsx
│   │       │   ├── AlertCard.tsx
│   │       │   ├── AlertsSection.tsx
│   │       │   ├── AlertDetailPane.tsx
│   │       │   ├── AlertMap.tsx
│   │       │   ├── StormReportsSection.tsx
│   │       │   ├── ODOTSection.tsx
│   │       │   ├── SPCSection.tsx
│   │       │   └── WindGustsSection.tsx  # Wind gusts section
│   │       └── styles/main.css
│   ├── widgets/                   # Streaming widgets for OBS/etc
│   │   ├── widget-common.js       # Shared widget utilities
│   │   ├── ticker.html            # Non-sponsored ticker
│   │   ├── ticker.js
│   │   ├── ticker.css
│   │   ├── ticker-sponsored.html  # Sponsored ticker
│   │   ├── ticker-sponsored.js
│   │   └── ticker-sponsored.css
│   ├── tests/
│   │   └── parsers/
│   └── run_tests.py
└── Alert Dashboard V1/            # Original V1 for reference
```

---

## How to Run the Backend

```bash
# Navigate to V2 project
cd "Alert Dashboard V2/Alert Dashboard V2"

# Install dependencies
pip install -r requirements.txt

# Create .env file with credentials (optional for NWWS)
# NWWS_USERNAME=your_username
# NWWS_PASSWORD=your_password

# Run the server
python -m backend.main

# Or with uvicorn directly
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

**API will be available at:**
- http://localhost:8000 - Root
- http://localhost:8000/docs - Swagger UI (auto-generated)
- http://localhost:8000/health - Health check
- ws://localhost:8000/ws - WebSocket

---

## Files Still To Create

### Backend (Optional Refactoring):
- [ ] `backend/routes/alerts.py` - Extract alert routes
- [ ] `backend/routes/websocket.py` - Extract WebSocket handler
- [ ] `backend/routes/health.py` - Extract health routes

### Frontend (Future Phase):
- [ ] React app with TypeScript
- [ ] Alert list component
- [ ] Map component (Leaflet)
- [ ] Settings/preferences panel
- [ ] Real-time WebSocket connection

---

## Architecture Decisions

1. **Dual Alert Sources**: NWWS-OI (real-time XMPP) + NWS API (polling backup)
2. **WebSocket for Frontend**: Real-time push updates
3. **Singleton Services**: Easy access across application
4. **Callback Pattern**: Loose coupling between services
5. **Async Throughout**: Full asyncio support
6. **Pydantic Settings**: Type-safe configuration
7. **FastAPI Lifespan**: Proper startup/shutdown management

---

## How to Resume Work

When starting a new session, tell Claude:

1. "Read CHAT_HISTORY.md in the Alert Dashboard V2 folder"
2. "Continue where we left off"

**Current Next Steps:**
- ~~Test the backend by running it~~ ✅ DONE - Backend working!
- ~~Start on the React frontend~~ ✅ DONE - Frontend working!
- ~~Bug fixes from Session 4 & 5~~ ✅ DONE - All parsing issues fixed!
- **Redesign alert cards with impacts** <- NEXT
  - Show snow/wind/hail amounts
  - Add expiration time
  - Show county names (now working with UGC service)

---

## Session Log

### Session 1 (Date Unknown - Previous Context Lost)
- Created initial project structure
- Implemented parsers (VTEC, UGC, threat, alert)
- Created Alert model
- Set up configuration system

### Session 2 (December 12, 2025)
- Created NWS API Client with retry logic
- Created NWWS-OI XMPP client
- Created Alert Manager service
- Created WebSocket Message Broker
- Created Zone Geometry Service
- Created Main Application (main.py)
- Fixed NWS API parameters (removed deprecated `limit` and `status` params)
- Backend fully tested and working

### Session 3 (December 12, 2025)
- **Created complete React frontend:**
  - Set up Vite + TypeScript + React project
  - Created `useWebSocket` hook for real-time alert updates
  - Created TypeScript types matching backend Alert model
  - Built alert list/grid with filtering (All, Tornado, Severe, etc.)
  - Built alert counter bar showing counts by type
  - Built AlertCard component with color coding
  - Built AlertDetailPane slide-out for full alert details
  - Built AlertMap with Leaflet for polygon visualization
  - Built Sidebar navigation with all menu items
  - Dark theme CSS matching V1 look and feel
- **Configured FastAPI to serve static frontend:**
  - Added static file serving from `frontend/dist`
  - Frontend builds to production bundle
  - Team can access via `http://atmosphericx.ddns.net:8000/index.html`
- **STATUS**: Core dashboard working! Ready for additional features.

### Session 4 (December 12, 2025)
- **Bug Fixes Based on User Feedback:**
  - ✅ **Fixed CounterBar**: Now only shows alert types with active alerts (count > 0)
  - ✅ **Fixed Map Polygon Rendering**: Each polygon now rendered separately for proper multi-zone display
  - ✅ **UGC Service Created**: `backend/services/ugc_service.py` - Maps UGC codes to county/zone names

- **Files Modified:**
  - `frontend/src/components/CounterBar.tsx` - Only show counters with count > 0
  - `frontend/src/components/AlertMap.tsx` - Render each polygon separately
  - `backend/services/ugc_service.py` - NEW - UGC code to name mapping
  - `backend/services/__init__.py` - Added UGC service exports
  - `backend/data/ugc_map.json` - NEW - Copied from V1

### Session 5 (December 18, 2025) - CURRENT
- **Backend Bug Fixes:**
  - ✅ **Fixed config import error**: Modified `main.py` to handle both direct and module execution
  - ✅ **Wired UGC Service**: Integrated into alert parser to populate `display_locations` with human-readable county/zone names
  - ✅ **Fixed Wind Threat Parsing**: Improved regex patterns, lowered min threshold to 10 mph for winter weather
  - ✅ **Fixed Hail Threat Parsing**: Updated patterns to handle multiple formats
  - ✅ **Fixed Snow Amount Parsing**: Enhanced patterns for various formats
  - ✅ **Fixed Issuing Office "Unknown"**: Added WFO code to name mapping with 100+ NWS offices

- **Frontend Fixes:**
  - ✅ **Created .env file**: State filtering now works - edit `.env` to change `FILTER_STATES`
  - ✅ **Fixed Severe Thunderstorm color**: Added `.alert-card.sv` and `.alert-card.to` CSS classes
  - ✅ **Updated AlertCard**: Now shows display_locations, expiration, sender_name, and impacts (wind/hail/snow/ice)
  - ✅ **Updated TypeScript types**: Added all new fields to Alert and ThreatInfo interfaces
  - ✅ **Fixed AlertMap**: Updated polygon handling, shows display_locations in popups

- **Files Modified (Backend):**
  - `backend/main.py` - Added dual-mode imports, load UGC map on startup
  - `backend/parsers/alert_parser.py` - Integrated UGC service, added WFO name fallback
  - `backend/parsers/patterns.py` - Improved wind, hail, and snow regex patterns
  - `backend/parsers/threat_parser.py` - Updated parsing logic, lowered wind threshold to 10mph
  - `backend/models/alert.py` - Added WFO_NAMES mapping and `get_wfo_name()` function
  - `.env` - NEW - Configuration file for state filtering and server settings

- **Files Modified (Frontend):**
  - `frontend/src/types/alert.ts` - Updated ThreatInfo and Alert interfaces
  - `frontend/src/components/AlertCard.tsx` - Redesigned with impacts, expiration, display_locations
  - `frontend/src/components/AlertDetailPane.tsx` - Updated for new field names
  - `frontend/src/components/AlertMap.tsx` - Fixed polygon handling, updated field references
  - `frontend/src/styles/main.css` - Added alert card colors for sv, to, wc, hw, cw, ec phenomena

### Session 6 (December 19, 2025) - CURRENT
- **Bug Fixes:**
  - ✅ **Fixed AlertDetailPane z-index**: Moved component outside app-layout div to avoid stacking context issues with Leaflet map (z-index up to 1000). Used React fragment to render at root level.
  - ✅ **Fixed hail/snow parsing confusion**: Pattern was matching "quarter" from "quarter mile" as hail. Fixed PATTERN_HAIL_DESC to require "HAIL" or "SIZE" keyword. Also fixed PATTERN_SNOW_AMOUNT to handle adjectives like "Up to 1 inch of quick snow".

- **New Feature: Local Storm Reports (LSR) Section:**
  - ✅ **Created LSR Service** (`backend/services/lsr_service.py`):
    - `StormReport` dataclass with fields for type, magnitude, location, timestamp
    - `LSRService` class with caching, API fetching from Iowa State Mesonet
    - API URL: `https://mesonet.agron.iastate.edu/geojson/lsr.geojson`
    - LSR type colors matching NWS conventions (tornado=red, hail=green, etc.)
    - Singleton pattern with `get_lsr_service()`, `start_lsr_service()`, `stop_lsr_service()`

  - ✅ **Added LSR API Endpoints** (`backend/main.py`):
    - `GET /api/lsr` - Get storm reports (hours, report_type, refresh params)
    - `GET /api/lsr/stats` - Report statistics by type
    - `GET /api/lsr/types` - Available types and their colors

  - ✅ **Created Frontend Types** (`frontend/src/types/lsr.ts`):
    - `StormReport` interface matching backend
    - `LSR_TYPE_COLORS` constant
    - `getTextColorForBackground()` utility function

  - ✅ **Created StormReportsSection Component** (`frontend/src/components/StormReportsSection.tsx`):
    - Map view with CircleMarkers for each report
    - List view with report details
    - Type filtering buttons (Tornado, Hail, Wind, etc.)
    - Time range selector (6h, 12h, 24h, 48h, 7d)
    - Auto-refresh toggle
    - Loading and empty states

  - ✅ **Added LSR CSS Styles** (`frontend/src/styles/main.css`):
    - `.lsr-section`, `.lsr-controls`, `.lsr-type-filters`
    - `.lsr-content`, `.lsr-map-container`, `.lsr-list`
    - `.lsr-report-card`, `.lsr-empty-state`

- **New Feature: Viewer Storm Reports:**
  - ✅ **Updated LSR Service** (`backend/services/lsr_service.py`):
    - Added `is_viewer`, `submitter`, `location_text` fields to StormReport
    - Added `remove_manual_report()` method
    - State filtering: official reports filtered by state, viewer reports always shown

  - ✅ **Added Viewer Report Endpoints** (`backend/main.py`):
    - `GET /api/lsr/all` - Get all reports (official + viewer) with state filtering
    - `GET /api/lsr/viewer` - Get only viewer reports
    - `POST /api/lsr/viewer` - Submit viewer report from dashboard
    - `DELETE /api/lsr/viewer/{id}` - Remove viewer report
    - `DELETE /api/lsr/viewer` - Clear all viewer reports
    - `POST /api/submit_storm_report` - Website submission with reCAPTCHA

  - ✅ **Updated StormReportsSection** (`frontend/src/components/StormReportsSection.tsx`):
    - "Add Report" button with modal form
    - Map click to pick location
    - Report type selection grid
    - Viewer reports shown with purple eye icon
    - Remove button for viewer reports

  - ✅ **Added Viewer Report CSS** (`frontend/src/styles/main.css`):
    - Modal styles (`.lsr-modal-*`)
    - Viewer report styling (`.lsr-viewer-*`)
    - Location picker overlay

- **Files Created:**
  - `backend/services/lsr_service.py` - NEW - LSR fetching and caching service
  - `frontend/src/types/lsr.ts` - NEW - TypeScript types for storm reports
  - `frontend/src/components/StormReportsSection.tsx` - NEW - Full LSR section component

- **Files Modified:**
  - `backend/services/__init__.py` - Added LSR service exports
  - `backend/main.py` - Added LSR service lifecycle and API endpoints, viewer report endpoints
  - `backend/parsers/patterns.py` - Fixed PATTERN_HAIL_DESC and PATTERN_SNOW_AMOUNT
  - `backend/parsers/threat_parser.py` - Updated parse_hail_size for new pattern groups, null check for text
  - `frontend/src/App.tsx` - Moved AlertDetailPane to root level, imported StormReportsSection
  - `frontend/src/types/lsr.ts` - Added viewer report fields and ViewerReportSubmission interface
  - `frontend/src/styles/main.css` - Added ~600 lines of LSR and viewer report styles

### Session 7 (December 21, 2025) - CURRENT
- **New Feature: Streaming Ticker Widgets:**
  - ✅ **Created widget-common.js** (`widgets/widget-common.js`):
    - Shared WebSocket connection handling with auto-reconnect
    - Base TickerWidget class with alert filtering
    - URL parameter parsing for theme, states, speed settings
    - Connection status indicator management

  - ✅ **Created Non-Sponsored Ticker** (`widgets/ticker.html`, `ticker.js`, `ticker.css`):
    - Full-width ticker bar for live streams
    - Connects to V2 WebSocket for real-time alerts
    - Alert type badge with color coding (TOR=red, SVR=orange, etc.)
    - Title, subtitle (key details), location, expiration time display
    - Multiple theme support: classic, atmospheric, storm-chaser, meteorologist, winter
    - State filtering via URL parameters
    - Auto-rotation with smart timing (waits for scroll animation to complete)

  - ✅ **Created Sponsored Ticker** (`widgets/ticker-sponsored.html`, `ticker-sponsored.js`, `ticker-sponsored.css`):
    - Same as non-sponsored with sponsor slot on left side
    - Supports image and text sponsors with rotation
    - Sponsor objects via URL-encoded JSON parameter

  - ✅ **Added Widget API Endpoints** (`backend/main.py`):
    - `GET /api/widgets/config` - Widget configuration (filter_states, themes)
    - `GET /api/widgets/sponsors` - Sponsor configuration
    - Mounted widgets directory at `/widgets` for static serving

- **Ticker Bug Fixes:**
  - ✅ **Fixed message type handling**: Backend uses `alert_bulk`, `alert_new`, `alert_remove` not `bulk_alerts`, `new_alert`, `alert_expired`
  - ✅ **Fixed display_locations type**: Added handling for both string and array types
  - ✅ **Fixed expiration time**: V2 uses `expiration_time` not `expires`
  - ✅ **Fixed alert colors**: V2 uses `phenomenon` (singular) not `phenomena`
  - ✅ **Added Snow Squall Warning**: Added missing SQ color (MediumVioletRed #C71585)
  - ✅ **Added key details subtitle**: Yellow line showing extracted wind/hail/impact details from WHAT section
  - ✅ **Smart rotation timing**: Ticker now waits for scroll animation to complete + 2 seconds buffer before rotating

- **New Feature: Wind Gusts Section:**
  - ✅ **Created Wind Gusts Service** (`backend/services/wind_gusts_service.py`):
    - `WindGustReport` dataclass with station, city, state, gust_mph, severity
    - `WindGustsService` class fetching from Iowa State Mesonet ASOS API
    - URL: `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py`
    - Station name mappings for OH, WA, OR (100+ stations)
    - Severity thresholds: significant (70 mph), severe (58 mph), advisory (46 mph)
    - Caching with 5-minute TTL
    - Singleton pattern with `get_wind_gusts_service()`, `start_wind_gusts_service()`, `stop_wind_gusts_service()`

  - ✅ **Added Wind Gusts API Endpoints** (`backend/main.py`):
    - `GET /api/wind-gusts` - Get top wind gusts with flat list and by-state grouping
    - `GET /api/wind-gusts/by-state` - Get gusts organized by state with per-state limits
    - `GET /api/wind-gusts/stats` - Service statistics

  - ✅ **Created WindGustsSection Component** (`frontend/src/components/WindGustsSection.tsx`):
    - Time range selector (1h, 3h, 6h, 12h, 24h)
    - Severity legend with color coding
    - "Highest Gust" highlight card with trophy icon
    - State-organized grid layout
    - Gust items with speed, city, and time
    - Color-coded severity indicators

  - ✅ **Added Wind Gusts CSS** (`frontend/src/styles/main.css`):
    - `.wind-gusts-section`, `.wind-gusts-controls`
    - `.wind-gusts-legend`, `.wind-gusts-highest`
    - `.wind-gusts-states-grid`, `.wind-gusts-state-card`
    - `.wind-gusts-item` with severity-specific backgrounds

- **Files Created:**
  - `widgets/widget-common.js` - Shared widget utilities
  - `widgets/ticker.html`, `widgets/ticker.js`, `widgets/ticker.css` - Non-sponsored ticker
  - `widgets/ticker-sponsored.html`, `widgets/ticker-sponsored.js`, `widgets/ticker-sponsored.css` - Sponsored ticker
  - `backend/services/wind_gusts_service.py` - Wind gusts data service
  - `frontend/src/components/WindGustsSection.tsx` - Wind gusts dashboard section

- **Files Modified:**
  - `backend/services/__init__.py` - Added wind gusts service exports
  - `backend/main.py` - Added widget endpoints, wind gusts service lifecycle and endpoints
  - `frontend/src/App.tsx` - Imported and integrated WindGustsSection
  - `frontend/src/styles/main.css` - Added ~350 lines of wind gusts section styles

---

## Test Results (December 12, 2025)

```
Server: http://127.0.0.1:8000
Swagger Docs: http://127.0.0.1:8000/docs

GET /health:
{"status":"healthy","services":{"alert_manager":{"active_alerts":14},"websocket":{"connected_clients":0},"nwws":{"connected":false}}}

GET /api/stats:
{"total_alerts":14,"warnings":5,"watches":1,"high_priority":0,"by_phenomenon":{"LE":2,"WW":7,"WS":4,"CW":1},"by_source":{"nwws":0,"api":14}}

Alert types loaded: Lake Effect (LE), Winter Weather (WW), Winter Storm (WS), Cold Weather (CW)
```

---

## Notes for Future Sessions

- V1 code is in `Alert Dashboard V1/` folder for reference
- Tests exist in `tests/parsers/` - run with `python run_tests.py`
- Settings are in `backend/config/settings.py`
- FastAPI auto-generates API docs at `/docs`
- WebSocket sends ALERT_BULK on connect with all current alerts
- **To run backend**: `cd "Alert Dashboard V2/Alert Dashboard V2" && uvicorn backend.main:app --reload`

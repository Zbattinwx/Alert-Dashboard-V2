# Alert Dashboard V2 - Development Session History

**Last Updated:** December 12, 2025 (Session 4 - Bug Fixes In Progress)
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

**NEXT STEPS (Immediate - Bug Fixes):**
1. Wire UGC service to populate `display_locations` in alert API responses
2. Fix threat parsing (wind showing blank, hail showing incorrect values)
3. Extract snow amounts from alert descriptions
4. Fix issuing office showing "Unknown"
5. Redesign alert cards with impacts instead of headline

**FUTURE STEPS:**
- Add remaining dashboard sections (SPC, Storm Reports, etc.)
- Add audio alerts for new warnings
- Add settings panel for customization
- Test with NWWS-OI credentials for real-time streaming

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
│   │   │   └── ugc_service.py        # NEW - UGC to county name mapping
│   │   ├── data/
│   │   │   └── ugc_map.json          # NEW - UGC code lookup data
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── timezone.py
│   │       └── logging.py
│   ├── frontend/                  # React Frontend (NEW!)
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── tsconfig.json
│   │   ├── index.html
│   │   ├── dist/                 # Built production files
│   │   └── src/
│   │       ├── main.tsx
│   │       ├── App.tsx
│   │       ├── types/alert.ts
│   │       ├── hooks/useWebSocket.ts
│   │       ├── components/
│   │       │   ├── Sidebar.tsx
│   │       │   ├── CounterBar.tsx
│   │       │   ├── AlertCard.tsx
│   │       │   ├── AlertsSection.tsx
│   │       │   ├── AlertDetailPane.tsx
│   │       │   └── AlertMap.tsx
│   │       └── styles/main.css
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
- **Continue bug fixes from Session 4** <- NEXT
  - UGC service created but not yet wired to API
  - Need to fix threat parsing and add snow amounts
  - Redesign alert cards with impacts

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

### Session 4 (December 12, 2025) - CURRENT
- **Bug Fixes Based on User Feedback:**
  - ✅ **Fixed CounterBar**: Now only shows alert types with active alerts (count > 0)
  - ✅ **Fixed Map Polygon Rendering**: Each polygon now rendered separately for proper multi-zone display
  - 🔄 **UGC Service Created**: `backend/services/ugc_service.py` - Maps UGC codes to county/zone names
    - Copied `ugc_map.json` from V1 to `backend/data/ugc_map.json`
    - Functions: `get_ugc_name()`, `get_display_locations()`, `get_county_names_list()`
    - STILL NEEDED: Wire into alert model and API response

- **REMAINING TASKS (from user feedback):**
  - [ ] Wire UGC service to provide `display_locations` field in alerts
  - [ ] Fix threat parsing - wind showing blank when should be 35mph
  - [ ] Fix threat parsing - hail showing incorrect 1" value
  - [ ] Extract snow amounts from alert description
  - [ ] Fix issuing office showing "Unknown"
  - [ ] Redesign alert cards:
    - Remove headline (already have event type)
    - Add impacts (snow/wind/hail amounts)
    - Add expiration time
    - Add storm motion if applicable
    - Show county names instead of UGC codes

- **Files Modified This Session:**
  - `frontend/src/components/CounterBar.tsx` - Only show counters with count > 0
  - `frontend/src/components/AlertMap.tsx` - Render each polygon separately
  - `backend/services/ugc_service.py` - NEW - UGC code to name mapping
  - `backend/services/__init__.py` - Added UGC service exports
  - `backend/data/ugc_map.json` - NEW - Copied from V1

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

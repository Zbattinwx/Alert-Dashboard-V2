# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Development
```bash
# Backend (dev, from project root)
python backend/main.py

# Frontend (dev, proxies /api and /ws to localhost:3074)
cd frontend && npm run dev

# Production build (sets base path for Caddy reverse proxy)
cd frontend && VITE_BASE_PATH=/v2/ npm run build

# Docker (full stack)
docker compose up -d --build
```

### Tests
```bash
pytest                                                              # all tests
pytest tests/parsers/test_alert_parser.py -v                       # single module
pytest tests/parsers/test_alert_parser.py::TestClass::test_name -v # single test
pytest --cov=backend tests/                                        # with coverage
```

Tests are in `tests/parsers/` (alert, threat, UGC, VTEC parsing) and `tests/services/` (zone geometry).

---

## Architecture

### Stack
- **Backend**: FastAPI + Uvicorn on port 3074, Python 3.12
- **Frontend**: React 18 + TypeScript + Vite + Leaflet (maps), dev server on port 3000
- **Reverse proxy**: Caddy routes `atmosphericx.ddns.net/v2/*` → container
- **Deployment**: Docker multi-stage (Node build → Python/Rust build → slim runtime), Raspberry Pi target via `deploy.bat`

### Data Flow
Alerts arrive from two sources and fan out via WebSocket:
1. **NWWS-OI** (primary) — XMPP weather wire in `nwws_client.py`, parses raw NWS text products
2. **NWS API** (fallback/polling) — HTTP via `nws_api_client.py`

Both feed `AlertManager`, which persists alerts to `data/alerts.json` and fires callbacks to `MessageBroker`. `MessageBroker` owns all WebSocket connections and broadcasts typed messages (`ALERT_NEW`, `ALERT_UPDATE`, `ALERT_REMOVE`, `ALERT_BULK`, radar frames, storm cells, lightning, etc.).

The frontend connects to `/ws` on load, receives `ALERT_BULK` for initial state, then reacts to incremental updates.

### Backend Services (`backend/services/`)
Services are singletons accessed via `get_*_service()`. They start/stop during FastAPI lifespan. Key services:

| Service | Purpose |
|---|---|
| `alert_manager.py` | State store; loads/persists alerts, fires callbacks |
| `nwws_client.py` | XMPP connection to NWWS-OI weather wire |
| `nws_api_client.py` | HTTP client for weather.gov API |
| `zone_geometry_service.py` | Lazy-fetches and caches zone polygon geometries |
| `message_broker.py` | WebSocket connection manager and broadcast hub |
| `spc_service.py` | SPC outlooks, mesoscale discussions, risk polygons |
| `lsr_service.py` | Local Storm Reports (hail, wind, tornado) |
| `nexrad_service.py` | NEXRAD Level 2 radar via AWS + ARM PyART |
| `storm_tracking_service.py` | Storm cell detection from radar volumes |
| `glm_service.py` | GOES-16 GLM lightning data |
| `agent_service.py` | Tool-calling AI agent (Qwen 2.5 Coder) via Ollama |
| `llm_service.py` | Alert context/analysis via Ollama (Gemma3 4B) |
| `spotter_network_service.py` | SpotterNetwork chaser position polling |

Optional services (radar, AI, social media) are gated by `.env` flags and skip gracefully if dependencies are missing.

### Parsing Pipeline (`backend/parsers/`)
Raw NWS text → `AlertParser` → `VtecParser` (extracts VTEC codes) → `ThreatParser` (hail/wind/tornado threats) → `Alert` model. Phenomenon filtering is in `AlertParser._is_target_phenomenon()`.

### Settings
`backend/config/settings.py` is a Pydantic `BaseSettings` loaded from `.env`. Cached via `@lru_cache` on `get_settings()`; call `reload_settings()` to bust the cache. User overrides (phenomena toggles) are layered on top from `data/user_settings.json` via `GET/POST /api/settings/phenomena`.

### Branding / White-Label
`backend/config/branding.py` loads a brand JSON from `config/brands/`. The active brand is set in `.env`. Brand config drives UI names, colors, and logo — allows deploying the same codebase for multiple properties (ONW, TBF, etc.).

### Alert Colors
Alert severity/type colors are defined in **6 places** that must stay in sync. Prefer a project-wide search before editing any color value.

### Frontend (`frontend/src/`)
- `App.tsx` — root component; owns `activeSection` state that controls which panel renders
- `hooks/useWebSocket.ts` — WebSocket lifecycle and message dispatch
- `hooks/useAssistant.ts` — AI assistant panel state
- Components render conditionally based on `activeSection`; sidebar nav drives it
- Pure CSS (no UI library); styles in `styles/main.css`

### Routing (Vite proxy in dev)
```
/api/*  →  http://localhost:3074/api/*
/ws     →  ws://localhost:3074/ws
```
In production, Caddy strips the `/v2` prefix before passing to the container.

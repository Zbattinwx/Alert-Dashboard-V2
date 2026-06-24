# Deploying Alert Dashboard V2 to the ONW Windows PC

This guide covers running V2 as a standalone Windows **EXE** (no Python, no
Docker) on the same PC that runs the ONW (V1) dashboard, reachable publicly at
**https://atmosphericx.ddns.net/v2/**.

## How it fits together

The ONW PC ends up running three things:

| Process | Port | Purpose |
|---|---|---|
| ONW dashboard (V1) | 8000 (HTTPS), 8765 (WSS) | Existing dashboard, **unchanged** |
| `dashboard-backend.exe` (V2) | 3074 (loopback) | This app, frozen EXE |
| `caddy.exe` | 80 + 443 | Reverse proxy / HTTPS for the domain |

Caddy terminates HTTPS for `atmosphericx.ddns.net` and routes:

- `/v2/*` → V2 backend on `localhost:3074`
- `/v1/*` and `/api/*` → ONW on `localhost:8000` / `:8765`
- `/` → redirect to `/v2/`

ONW is **also** still reachable the old way at `https://atmosphericx.ddns.net:8000`.

Why Caddy: a path like `/v2` can only be served by a reverse proxy. The V2
frontend is built with base path `/v2/`, and all its API/WebSocket calls are
base-path-aware (`frontend/src/utils/api.ts`), so everything works behind the
proxy with no per-deploy URL edits.

---

## A. Build the bundle (on the dev PC — this machine)

```cmd
build-windows.bat
```

> Run it from a normal **Command Prompt**, not git-bash. git-bash mangles the
> `/v2/` base path into `C:/Program Files/Git/v2/` and the build's asset URLs
> break. `build-windows.bat` uses cmd's `set`, which is safe.

It does three things:

1. `npm run build` with `VITE_BASE_PATH=/v2/` → `frontend/dist`
2. PyInstaller freeze → `packaging/dist/dashboard-backend/`
3. Assembles + zips → **`dist-windows/AlertDashboardV2-Server.zip`**

First build pulls a large scientific stack (PyART, MetPy, Cartopy, …); it takes
several minutes and a few GB of disk. `caddy.exe` is bundled if present in the
repo root (run `setup_caddy.bat` once to fetch it).

---

## B. First-time install (on the ONW PC)

1. Copy `AlertDashboardV2-Server.zip` over (RDP/share/USB) and extract to a
   stable folder, e.g. `C:\AlertDashboardV2-Server\`.
2. Put your `.env` next to `start-server.bat` — copy your working `.env` from
   the dev project, or rename `.env.example` and fill it in. (`BRAND=onw` is the
   default so the ONW logo and colors are used.)
3. **Router port-forwarding:** forward TCP **80** and **443** to this PC's LAN
   IP (`ipconfig` → IPv4 Address). Leave ONW's port-8000 forward as-is.
4. Confirm `atmosphericx.ddns.net` resolves to your public IP (DDNS updater).
5. Double-click **`start-server.bat`**.
   - Approve the Windows Firewall prompt for `caddy.exe`.
   - First launch, Caddy spends ~30–60 s provisioning a Let's Encrypt cert.
6. Open **https://atmosphericx.ddns.net/v2/**.

### Don't want to forward port 80 / prefer ONW's existing cert?

The `Caddyfile` has a commented alternative at the bottom that points Caddy at
ONW's existing cert (`C:\ssl-certs\atmosphericx.ddns.net-*.pem`) instead of
running ACME. Swap the site opening line as described there; then only 443 is
needed. (Trade-off: restart Caddy after each cert renewal.)

---

## C. Updating later

Manual-copy flow (no SSH needed):

1. On the dev PC: `build-windows.bat` → new `AlertDashboardV2-Server.zip`.
2. Copy + extract on the ONW PC.
3. Put the new `dashboard-backend` folder into a folder named `_update` next to
   `start-server.bat` (so `_update\dashboard-backend\dashboard-backend.exe`).
4. Run **`update.bat`**.

`update.bat` stops the server, swaps in the new app, **preserves `.env`, `data\`
and `Caddyfile`**, backs the old app up to `_backup\`, and restarts.

**Widget-only tweak (no rebuild):** the widgets are static files — drop changed
files into `dashboard-backend\_internal\widgets\` and refresh the OBS source.
(Backend Python changes do need a rebuild, since they're frozen into the EXE.)

---

## D. OBS / streaming widget URLs

All served under `/v2/widgets/`:

| Widget | URL |
|---|---|
| **Ticker V2** (favored two-bar) | `https://atmosphericx.ddns.net/v2/widgets/ticker-v2.html` |
| Ticker | `https://atmosphericx.ddns.net/v2/widgets/ticker.html` |
| Sponsored ticker | `https://atmosphericx.ddns.net/v2/widgets/ticker-sponsored.html` |
| Alert card | `https://atmosphericx.ddns.net/v2/widgets/alert-card.html` |
| Impact panel | `https://atmosphericx.ddns.net/v2/widgets/impact.html` |

`ticker-v2.html` query options: `?states=OH,KY,IN`, `?exclude=TO_A,SV_A` (hide
watches), `?speed=12000`, `?message=...`, `?test=emergency` (preview the tornado
emergency takeover). Recommended OBS browser-source size: 1920×100.

---

## E. Troubleshooting

- **`/v2/` shows a blank page / 404 assets** — the frontend was built without the
  `/v2/` base. Rebuild with `build-windows.bat` (cmd, not git-bash).
- **Cert never provisions** — ports 80/443 aren't reaching this PC, or the DDNS
  name doesn't point at your public IP. Verify the forward and DNS, or use the
  existing-cert variant in the `Caddyfile`.
- **`/v1/` 502s** — ONW isn't running, or isn't on HTTPS :8000 / WSS :8765. ONW
  stays reachable directly at `https://atmosphericx.ddns.net:8000` regardless.
- **WebSocket won't connect** — confirm you're hitting `https://` (so the widget
  uses `wss://`), and that Caddy is running.
- **AI chat/agent missing** — Ollama isn't installed/running on this PC. All AI
  features degrade gracefully; the dashboard otherwise runs fine.
- **Stop everything** — close the `start-server.bat` window, then
  `taskkill /f /im caddy.exe` and `taskkill /f /im dashboard-backend.exe`.

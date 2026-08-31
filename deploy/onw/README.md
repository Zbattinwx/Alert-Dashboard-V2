# ONW Alert Dashboard — fork and move it off the house

Cut ONW loose from TheBattinFront: its own repo, its own server, its own NWWS
account, no shared anything. After this, TBF development cannot reach ONW and
ONW is a maintenance-only tree.

Written 2026-08-30 against `Alert-Dashboard-V2 @ 8572dd3`.

> **This file lives in the TBF repo only until the fork is cut.** It is a copy
> source, so it travels with the tree in §2 and then gets deleted from this side
> (§2.5). Once ONW has its own repo, this document belongs to ONW.

---

## 0. Why this is happening

The ONW box currently sits on Zach's home network and is moving to a university,
where inbound port forwarding is off the table. But the move is the trigger, not
the reason. The reason is that ONW and TBF are one codebase on one release feed,
and ONW is going into maintenance while TBF keeps moving.

**The thing that actually forces this:** the `dashboard_update_*` block in
`backend/config/settings.py` (~line 72–82).

```python
dashboard_update_enabled     = True     # default
dashboard_update_manifest_url = ".../Zbattinwx/dashboard-releases/releases/latest/download/latest.json"
dashboard_update_check_interval_minutes = 30
```

There is no channel or per-deployment concept — every frozen standalone
deployment polls the same manifest every 30 minutes. Publishing a TBF build
ships it to ONW within the half hour. That is not theoretical: the `latest.json`
in the last built bundle is build `20260726`, and its release notes describe GLM
lightning, MRMS RadarOnly, NBM/GEFS and the NHC spaghetti proxy — TBF work that
already reached ONW automatically.

Fork + pin is what stops that.

---

## 1. Decisions already made, and the ones still open

| | |
|---|---|
| **Separation** | Hard fork. New repo, TBF and COWx brands stripped, frozen. |
| **Host** | Linux VPS, x86_64. Not the university network (§3). |
| **Updates** | Deliberate `git pull` + restart. Self-updater removed. |
| **OBS control** | **Nothing to do** — see §1.1. |
| **NWWS account** | ONW needs its **own**. Start this first (§8) — it has lead time. |
| **Open** | What happens to the legacy `/v1/` dashboard (§9). |
| **Open** | Final hostname + who owns the DNS record (§7). |

### 1.1 There is no OBS control in this codebase

The Director Panel — OBS scene switching, source toggles — was a feature of the
**legacy** `ONWAlertDashboard` repo (`main_app.py:160`, `OBS_WEBSOCKET_URL`).
AlertDashboardV2 has no OBS control in either the backend or the frontend; a
grep for `4455` in this tree hits latitude/longitude values in `cities_db.py` and
`us-states.json`, nothing else.

So "remove OBS control from the ONW version" is already true. Widgets are
unaffected — they are OBS *browser sources*, one-way, and keep working over
HTTPS from wherever the dashboard is hosted.

---

## 2. Cut the fork

### 2.1 Copy the tree

Work from a clean checkout at a known commit, not the working directory — the
dev tree carries build output, `.env`, and `data/` with live state.

```bash
cd /c/Users/troja/Documents
git clone https://github.com/Zbattinwx/Alert-Dashboard-V2.git onw-dashboard
cd onw-dashboard
git checkout <the commit ONW is known-good on>    # see §2.2
rm -rf .git
git init -b main
```

### 2.2 Pick the fork point deliberately

Do **not** fork from `main` just because it is newest. Fork from the build ONW is
currently running well, so the move changes location only — not behaviour. If
those are the same commit, good; if not, the delta is a thing you are choosing to
ship to ONW, and it should be reviewed as such.

Check what they are on: `version.json` next to the running exe gives the build
stamp; match it against `dashboard-releases`.

### 2.3 Strip

Remove:

- `config/brands/battinfront.json`, `config/brands/cowx.json`, `config/brands/cowx/`
- `backend/services/update_service.py` and its routes in `main.py`, plus the
  three `dashboard_update_*` fields in `settings.py`. It is Windows-only anyway
  (`is_frozen()` + `apply-update.ps1`), so on Linux it is dead code — but delete
  it rather than leave a disabled path pointing at TBF's feed.
- `packaging/windows/`, `build-windows.bat`, `apply-update.ps1`, `update.bat` —
  the frozen-Windows pipeline. Linux runs from source.
- `backend/services/hub_auth.py` — that gate is for the TBF Hub. ONW keeps
  whatever auth it already uses.
- `LiveStreamProject/` — TBF's 24/7 stream.

### 2.4 Do NOT strip the radar/model services yet

Tempting, because a lot of this backend exists to serve the TBF radar app, and
dropping it would shrink the install a lot. Resist it on the first pass.

The dashboard frontend genuinely calls `/api/radar/binary/`, `/api/radar/frames/`,
`/api/mrms/frame/`, `/api/proxy/mesoanalysis`, `/api/asos/observations`,
`/api/odot/cameras`, `/api/lsr/*`, `/api/spc/day1`, `/api/afd` — the alert map is
not just polygons. What is safe to cut depends on what ONW's operators actually
use, which is not knowable from this tree.

**Get it running identically first, then strip with evidence.** Turn on request
logging for a severe-weather week and remove what never gets hit. Likely
candidates once you have that data: `hrrr_field_service`, `hrrr_service`
(soundings), `mesoanalysis_service` (the RAP engine — note this is *not*
`/api/proxy/mesoanalysis`, which only proxies SPC images and IS used),
`goes_meso_service`, `glm_service`, `cameras_511_service`, `cameras_cars_service`.

### 2.5 Publish and disconnect

```bash
git add -A
git commit -m "Fork of Alert-Dashboard-V2 @ <sha> for ONW. Maintenance only."
gh repo create Zbattinwx/onw-dashboard --private --source=. --push
```

Then on the **TBF** side, delete `deploy/onw/` and remove the ONW brand from the
radar app (`src-tauri/brands/onw.conf.json`, `src-tauri/brands/onw/`,
`ONW_Logo.png`) — ONW does not use the radar app, and leaving a brand nobody
builds is how stale config rots.

Leave `config/brands/onw.json` in the TBF dashboard repo alone for now if the
`/v2/` deployment is still live during the transition; remove it at cutover.

---

## 3. Provision the VPS

**Take the machine off the university network entirely.** A tunnel would solve
inbound, but the dashboard holds a persistent outbound XMPP session to
`nwws-oi.weather.gov` on **5222** (`backend/services/nwws_client.py:38`), NWS
documents no BOSH or HTTP alternative, and campus networks routinely block
non-standard outbound ports. The failure mode is the bad one: the dashboard is
reachable and silently has no alerts. Add typical AUP rules against running
services, plus dorm reboots and summer, and it is the wrong home for an alerting
system.

**Spec: x86_64, 8 GB.** Not ARM — `arm-pyart`, `netCDF4`, `scipy` and
`matplotlib` all have clean manylinux x86_64 wheels; on ARM you risk source
builds. 8 GB rather than 4 because `nexrad_service` decodes NEXRAD Level 2
server-side via Py-ART, which is memory-hungry. If §2.4's logging later shows
single-site radar is unused, drop to the 4 GB tier and save the difference.

Hetzner CX22 (2 vCPU / 4 GB) is ~€4.35/mo as a floor; take the next tier up for
the headroom. Any provider is fine — the requirement is a datacenter network, not
a brand.

```bash
adduser onw && usermod -aG sudo onw
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

Outbound stays open — that is what NWWS needs.

---

## 4. Install

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git caddy nodejs npm
# only if a wheel is missing and pip falls back to building:
# sudo apt install -y build-essential libgeos-dev libhdf5-dev

sudo -u onw -i
git clone https://github.com/Zbattinwx/onw-dashboard.git /home/onw/dashboard
cd /home/onw/dashboard
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

`uvloop` is already guarded `sys_platform != 'win32'`, so Linux picks it up —
this tree was never Windows-only.

### 4.1 Rebuild the frontend for the new base path — do not skip this

**ONW's current frontend is built for `/v2/`. Serving that build at the root of a
new hostname breaks every request in the app.**

`frontend/src/utils/api.ts` derives both helpers from `import.meta.env.BASE_URL`,
which Vite bakes in **at build time**:

```ts
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');
export function apiUrl(path)  { return `${BASE}${path}`; }          // → /v2/api/alerts
export function wsUrl()       { return `${proto}//${host}${BASE}/ws`; } // → /v2/ws
```

So a `/v2/` build served at `/` asks for `/v2/api/alerts` and `/v2/ws`, and every
one of them 404s — including the alert WebSocket, which is the whole product.

Serving at the root of `dashboard.example.org`, build with base `/`:

```bash
cd /home/onw/dashboard/frontend
VITE_BASE_PATH=/ npm ci && VITE_BASE_PATH=/ npm run build
grep -o 'src="[^"]*assets[^"]*"' dist/index.html     # want /assets/... NOT /v2/assets/...
```

That grep is the check. If it says `/v2/assets/`, the build did not take the base
and the deployment will fail in a way that looks like a backend problem.

If you instead keep serving under a path prefix, build with that exact prefix and
make the Caddy `handle_path` match it.

---

## 5. Configure

`/home/onw/dashboard/.env`, mode `600`:

```ini
BRAND=onw

# ONW's OWN NWWS-OI subscriber account — see §8. Not Zach's.
NWWS_USERNAME=...
NWWS_PASSWORD=...

ALERT_SOURCE=nwws            # nws_api is the fallback; ~30 s slower

HOST=127.0.0.1               # Caddy fronts it
PORT=8000

GOOGLE_CHAT_WEBHOOK=...      # ONW's own space
```

`settings.py:63` — `brand` defaults to `"default"`, so `BRAND=onw` is what picks
up `config/brands/onw.json`. If it is missing you get generic styling, not an
error, so verify it in §10 rather than assuming.

**Two config traps:**

1. `data/user_settings.json` **overrides `.env`** for operator settings —
   monitored phenomena, states, counties, the Google Chat send-list, sounds
   (`settings.py:439`). If a setting will not take from `.env`, that file is why.
   It does *not* cover credentials or the brand.
2. Copy ONW's existing `data/user_settings.json` across (§9) or they lose their
   county/phenomena configuration and start alerting on the wrong area.

---

## 6. systemd

`/etc/systemd/system/onw-dashboard.service`:

```ini
[Unit]
Description=ONW Alert Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=onw
WorkingDirectory=/home/onw/dashboard
EnvironmentFile=/home/onw/dashboard/.env
ExecStart=/home/onw/dashboard/.venv/bin/python backend/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`backend/main.py`, not `-m backend.main` — `main()` calls
`uvicorn.run("backend.main:app", ...)` by string import, which needs the project
root on the path. `packaging/run_backend.py` exists for the *frozen* build and
works from source too, but the plain script is the documented dev/Docker path and
one less thing to keep.

`WorkingDirectory` is load-bearing: `settings.py:21` resolves
`data/user_settings.json` relative to the project root, and the NWWS session key
and alert history live under `data/` too.

**Ignore `websocket_port` (8765) in `settings.py`.** It is declared and never
read — nothing in the backend uses it. The real socket is `@app.websocket("/ws")`
on the main FastAPI app (`main.py:5148`), so there is **one** port to proxy and
one to open. Exposing 8765 would achieve nothing.

```bash
systemctl enable --now onw-dashboard && journalctl -u onw-dashboard -f
```

`Restart=always` matters more here than it did at the house — an XMPP session
that drops needs the process back, and nobody is sitting next to this machine.

---

## 7. Caddy, TLS, DNS

`/etc/caddy/Caddyfile`:

```
dashboard.example.org {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1        # WebSocket alerts + radar frames; do not buffer
    }
}
```

`flush_interval -1` is the same thing the Hub deployment needs — without it the
alert WebSocket buffers and alerts arrive late in bursts.

DNS: an **A record** to the VPS IP. This is a static datacenter address, so no
DDNS and no CNAME-to-DDNS indirection — that was a home-IP workaround. Decide who
owns the record; ONW's site is on InMotion, so a subdomain there is the obvious
home, and it must be a record ONW can still administer after Zach steps back.

**InMotion cannot host the app itself.** Their shared WordPress plans run Python
scripts but not a persistent daemon holding an XMPP socket and a WebSocket
server. Website stays at InMotion; the dashboard is the VPS.

---

## 8. NWWS-OI — do this first

Zach's deploy notes say plainly not to put NWWS credentials on a second machine,
because it is one account and the desk Hub holds it. A 24/7 ONW instance needs
**its own subscriber account** from NWS. Request it before anything else here —
it is the only step with an external lead time, and everything else is worthless
without a feed.

Interim, and only interim: `ALERT_SOURCE=nws_api` polls the public API instead.
It works and needs no account, but it is roughly 30 s behind the wire, which for
a tornado warning is the difference the product exists to close.

Verify from the VPS before cutover:

```bash
nc -zv nwws-oi.weather.gov 5222        # must connect
journalctl -u onw-dashboard | grep -i nwws
```

---

## 9. Migrate state, and decide about `/v1/`

No database — flat files. Copy from the old box:

- `data/user_settings.json` — counties, phenomena, Google Chat list, sounds
- `data/alert_history.json`, `data/event_stats_state.json`
- anything under `data/sounds/` they customised

The legacy `/v1/` dashboard (the separate `onw-alert-dashboard` repo) is still
served from the ONW PC today. It does not come along by default. Either retire it
at cutover or give it the same treatment — but decide, because when that PC
leaves the house it stops answering either way.

---

## 10. Verify

- [ ] `systemctl status onw-dashboard` active, survives `reboot`
- [ ] `journalctl` shows NWWS **connected and joined the room**, not just started
- [ ] A live warning appears without a refresh (proves the WebSocket, not polling)
- [ ] Branding is ONW — logo and colors, not generic (proves `BRAND=onw` took)
- [ ] Monitored counties/phenomena match the old box exactly
- [ ] Google Chat fires to ONW's space — and **only** ONW's
- [ ] Every OBS widget URL loads from the new hostname; update the OBS scenes
- [ ] Alert map draws polygons and radar
- [ ] TLS valid; `http://` redirects
- [ ] `grep -rn "dashboard-releases" .` returns **nothing**
- [ ] Browser devtools: **no 404s** on `/api/*` and the `/ws` upgrade succeeds —
      this is the §4.1 base-path check, and it is the most likely thing to be
      wrong after the move

---

## 11. Rollback

The old box keeps running until every §10 box is ticked. Rollback is pointing DNS
back and restarting it. Do not decommission until ONW has been through a real
severe-weather event on the new host.

---

## 12. What "maintenance only" means

This tree is frozen. The point of the fork is that TBF work cannot arrive here by
accident, so nothing arrives here by accident at all — including fixes.

When a real bug is fixed in TBF and it matters to ONW, it is a deliberate act:

```bash
git remote add tbf https://github.com/Zbattinwx/Alert-Dashboard-V2.git
git fetch tbf
git cherry-pick <sha>          # one commit, reviewed, tested here
systemctl restart onw-dashboard
```

Never merge `tbf/main`. The trees diverge on purpose, and a merge undoes the
separation in one command.

Worth watching regardless of the freeze: NWS changing NWWS-OI, CAP/UGC format
changes, and Python or dependency EOL. Those arrive whether or not anyone is
developing this.

---

## Related

- TBF Hub deployment: `deploy/README.md` in the **RadarApp** repo — the mirror of
  this document for the TBF side.
- **The Hub gets simpler when ONW leaves.** Today `deploy/Caddyfile.onw-snippet`
  routes `hub.thebattinfront.com` through the ONW box, because 80/443 forward
  there and you cannot double-forward a port. When that machine goes, forward
  80/443 straight to the media server: one less hop, and the Hub stops depending
  on a machine it does not own. Update the RadarApp deploy docs at cutover.

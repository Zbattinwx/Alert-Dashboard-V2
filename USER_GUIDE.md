# ONW Alert Dashboard — User Guide

A real-time National Weather Service severe-weather dashboard for **Ohio News &
Weather (ONW)** — live warnings, Level-2 radar, storm reports, surface data, and
ready-to-use OBS broadcast graphics.

This guide is for the ONW team. It covers how to get in, what every section does,
and how to wire the streaming widgets into OBS.

---

## 1. Getting in

**Open this link in any browser:**

> ## https://atmosphericx.ddns.net/v2/

- Works in Chrome, Edge, Firefox, and Safari, on desktop and mobile.
- **No password or login.** Anyone with the link can view it, so keep the link
  inside the team.
- The first time it loads it connects to the server and immediately shows the
  current alerts; after that it updates live (you don't need to refresh).
- A small **connection indicator** shows whether you're connected. If it shows
  disconnected, it will keep trying to reconnect on its own.
- **Bookmark it** / add to your phone's home screen for quick access.

> **Note:** the dashboard runs on the ONW computer at the station. If the site
> won't load at all, that computer or its server is probably off — see
> *Troubleshooting* and contact the admin.

---

## 2. The dashboard at a glance

The left **sidebar** switches between sections. Here's what each one does.

| Section | What it shows |
|---|---|
| **Active Alerts** | The live list of all current warnings, watches, and advisories, color-coded by type and sorted by severity. Click any alert to open its full details (what/where/when, threat tags like wind/hail/tornado, the raw NWS text). |
| **Alert Map** | An interactive map of the warning polygons with the live NEXRAD radar behind them. Pan/zoom, click a polygon for details. |
| **NEXRAD Radar** | High-resolution Level-2 radar rendered natively (reflectivity, velocity, and other products), with detected storm cells, lightning, and rotation/hail signatures. Switch radar sites and products. |
| **Storm Reports** | Local Storm Reports (LSR) — tornado, hail, wind, and flooding reports from the NWS and spotters, plotted on a map and listed with magnitudes. |
| **ODOT Cameras** | Live Ohio DOT traffic-camera feeds. Search by location; useful for visual confirmation of conditions on air. |
| **SPC Outlooks** | Storm Prediction Center convective outlooks (Day 1–3), risk categories/polygons, and mesoscale discussions. |
| **Forecast Discussions** | The full NWS Area Forecast Discussion (AFD) text, selectable by forecast office. |
| **Top Wind Gusts** | The highest wind gusts being reported by surface stations during an event. |
| **Surface Obs** | A METAR surface-observation map — temperature, wind, and conditions at airports/stations. |
| **Snow Emergencies** | County-by-county snow-emergency levels (1/2/3) for Ohio during winter weather. | (Not Yet Implemented)
| **NWWS Products** | A live feed of the raw products coming across the NWS Weather Wire — product type, issuing office, and time. |
| **Social Media** | Status of auto-posted alert graphics (Facebook / Bluesky), if posting is enabled. | (Not Yet Implemented)
| **Event Stats** | Running totals for the current event/session — alert counts by type, number of storm reports, biggest hail and wind reported. Can be reset to start a fresh event. |
| **Alert Graphics** | A gallery of broadcast graphics that the system auto-generates for active warnings (with the radar + polygon baked in) — ready to pull into a show. |
| **Settings** | Choose which alert types (phenomena) the dashboard tracks, and configure the ticker filters. |

### Things you can do with an alert
- **Open it** for the full breakdown (threat tags, impacted areas, expiration, raw text).
- **Broadcast graphics** are generated automatically for warnings (tornado, severe
  t-storm, flash flood, winter, etc.) — find them under **Alert Graphics**.
- **"In the Path" scan** — for a storm-based warning you can scan the polygon for
  towns, schools, and other places in its path; the result feeds the on-stream
  **Impact Panel** widget (see §4).

### AI assistant (if enabled on the server)
If the station server has the AI assistant turned on, there's a chat panel where
you can ask plain-language questions about the current alerts and conditions. If
it's not enabled, the rest of the dashboard works normally without it.

---

## 3. Special pages

These are separate URLs (not in the sidebar):

| Page | URL | Use |
|---|---|---|
| **Chase Mode** | `https://atmosphericx.ddns.net/v2/chase` | A stripped-down, mobile-friendly map for the field. Open it on a phone to share your GPS position back to the dashboard so the team can see where you are. |
| **OBS New-Alert Overlay** | `https://atmosphericx.ddns.net/v2/obs` | A transparent pop-up banner that flashes on screen when a new warning is issued, then auto-hides. Add it as an OBS browser source over your scene. |

---

## 4. OBS / streaming widgets

These are transparent web pages built to drop straight into OBS as **Browser
Sources**. They connect to the dashboard and update themselves live.

| Widget | URL | What it is | Suggested size |
|---|---|---|---|
| **Ticker V2** *(preferred)* | `…/v2/widgets/ticker-v2.html` | Two-bar lower ticker: a colored bar with the alert + impact tags on top, and a black bar with the ONW logo, scrolling locations, and an optional sponsor on the bottom. | 1920 × 100 |
| **Ticker** | `…/v2/widgets/ticker.html` | A single scrolling alert ticker (no sponsor slot). | 1920 × 90 |
| **Sponsored Ticker** | `…/v2/widgets/ticker-sponsored.html` | Single-bar ticker with a sponsor slot. | 1920 × 100 |
| **Alert Card** | `…/v2/widgets/alert-card.html` | A clean single-alert "card" graphic (event, locations, expiration, threat tags) for featuring one warning. | 800 × 600 |
| **Impact Panel** | `…/v2/widgets/impact.html` | An "IN THE PATH" side panel listing the towns/places in the path of the active warning — populated by the "In the Path" scan in the dashboard. | 500 × 1080 |
| **New-Alert Overlay** | `…/v2/obs` | Full-screen pop-up banner for newly issued warnings (see §3). | 1920 × 1080 |

(Full URLs start with `https://atmosphericx.ddns.net` — e.g.
`https://atmosphericx.ddns.net/v2/widgets/ticker-v2.html`.)

### Adding a widget to OBS
1. In OBS, **+ → Browser**.
2. Paste the widget URL.
3. Set the **Width/Height** (see the table).
4. Leave **Local file** unchecked (you're using a web URL).
5. Check **"Shutdown source when not visible"** to save resources.
6. OK. The widget connects and updates on its own — no refreshing needed.

### Ticker V2 options (add to the URL after a `?`)
- `?states=OH,KY,IN` — only show alerts touching these states
- `?exclude=TO_A,SV_A` — hide certain types (this example hides Tornado/Severe **watches**)
- `?speed=10000` — rotation speed in milliseconds when text fits without scrolling
- `?test=emergency` — preview the Tornado Emergency takeover styling
- Combine them with `&`, e.g. `…/ticker-v2.html?states=OH,KY&exclude=TO_A,SV_A`

### Sponsor logos on the ticker
The Ticker V2 has a sponsor slot on the bottom-right. To use it:
1. Put your logo PNGs in the server's widgets folder (the admin can do this, or
   see the deploy README): they become available at `…/v2/widgets/yourlogo.png`.
2. Create a `sponsors.json` in that same folder:
   ```json
   { "sponsors": [ { "logo": "canopy_sponsor.png" }, { "text": "Local Business" } ] }
   ```
3. The logos rotate in the sponsor slot. Edit the file / swap PNGs anytime —
   no restart. Use transparent PNGs sized roughly to **190 × 55 px**.
   Rotation speed: add `?sponsor_speed=15000` (ms) to the ticker URL.

---

## 5. Alert colors

Alerts are color-coded by type. The most common:

| Color | Alert |
|---|---|
| **Red** | Tornado Warning |
| **Orange** | Severe Thunderstorm Warning |
| **Dark Red** | Flash Flood Warning |
| **Pink** | Winter Storm Warning |
| **Yellow** | Tornado Watch |
| **Rose/Pink** | Severe Thunderstorm Watch |
| **Sea Green** | Flash Flood Watch |
| **Steel Blue** | Winter Storm Watch |
| **Purple** | Snow Squall Warning |
| **Light Blue** | Winter Weather Advisory |
| **Gray** | Special Weather Statement |

A **Tornado Emergency** (the most severe tornado warning) is highlighted
specially and takes over the ticker.

---

## 6. Tips & best practices

- **Monitoring:** keep **Active Alerts** or the **Alert Map** open in a dedicated
  window during severe weather. New alerts appear automatically.
- **On air:** put the **Ticker V2** up whenever you're covering weather. Use the
  **Alert Card** to feature one specific warning, and the **Impact Panel** to show
  what's in a storm's path.
- **Quiet days:** the dashboard still shows watches, advisories, SPC outlooks, and
  surface data — good for planning the next event.
- **Reset Event Stats** at the start of a new event so the running totals are clean.
- **Mobile:** the main dashboard and Chase Mode both work on phones/tablets.

---

## 7. Troubleshooting

| Problem | Try this |
|---|---|
| **Page won't load at all** | The station server/computer may be off. Check your internet, then contact the admin. |
| **Alerts look frozen** | Check the connection indicator. Refresh with **F5**; if still stuck, check your internet. |
| **A widget in OBS is blank / "connecting"** | Confirm the URL is exactly right (starts with `https://atmosphericx.ddns.net/v2/widgets/…`). Right-click the source → **Refresh**. Open the same URL in a normal browser tab to confirm it works. |
| **Map shows data but no background map** | Hard-refresh (**Ctrl+F5**). If it persists, tell the admin (the basemap may need attention). |
| **Logo or branding looks wrong** | Hard-refresh (**Ctrl+F5**) to clear a cached page. |
| **Security warning about the certificate** | Shouldn't happen on the normal link; if it does, you may have an old `:8000` link — use `https://atmosphericx.ddns.net/v2/`. |

> This dashboard is a monitoring and production tool. **Always follow official NWS
> guidance and local emergency management for life-safety decisions.**

---
---

## 9. Quick links

| | |
|---|---|
| **Dashboard** | https://atmosphericx.ddns.net/v2/ |
| **Chase Mode** | https://atmosphericx.ddns.net/v2/chase | (WIP)
| **Ticker V2 (OBS)** | https://atmosphericx.ddns.net/v2/widgets/ticker-v2.html |
| **New-Alert Overlay (OBS)** | https://atmosphericx.ddns.net/v2/obs |

**Stay safe and monitor wisely.**

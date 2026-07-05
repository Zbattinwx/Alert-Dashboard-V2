============================================================
 Alert Dashboard V2 - Windows Server bundle
============================================================

WHAT'S IN HERE
  dashboard-backend\   The frozen app (EXE + bundled frontend, widgets, brands)
  caddy.exe            Reverse proxy that puts the app on /v2 over HTTPS
  Caddyfile            Caddy routing config (atmosphericx.ddns.net)
  start-server.bat     Starts Caddy + the backend (with auto-restart)
  update.bat           Installs a newer build by hand (see MANUAL UPDATE below)
  apply-update.ps1     Used by the in-dashboard auto-updater (see AUTOMATIC UPDATES)
  version.json         This build's id (the updater compares it to the latest)
  .env.example         Settings template -> rename to .env (or copy your real one)


FIRST-TIME SETUP (on the ONW PC)
  1. Extract this whole folder somewhere stable, e.g.  C:\AlertDashboardV2-Server\
  2. Put your .env next to start-server.bat:
        - copy your working .env from the dev PC,  OR
        - rename .env.example to .env and fill it in.
  3. Router: forward TCP 80 and 443 to this PC's LAN IP (run ipconfig to find it).
        (ONW's own port 8000 forward, if you use it, stays as-is.)
  4. Make sure atmosphericx.ddns.net points at your public IP (DDNS).
  5. Double-click start-server.bat.
        - Windows Firewall may prompt for caddy.exe -> Allow.
        - First run, Caddy spends ~30-60s getting an HTTPS certificate.
  6. Visit  https://atmosphericx.ddns.net/v2/

  ONW (V1) keeps working at https://atmosphericx.ddns.net:8000  and is also
  reachable through Caddy at https://atmosphericx.ddns.net/v1/ .


OBS / STREAMING WIDGET URLs
  Ticker V2 (favored): https://atmosphericx.ddns.net/v2/widgets/ticker-v2.html
  Ticker:              https://atmosphericx.ddns.net/v2/widgets/ticker.html
  Sponsored ticker:    https://atmosphericx.ddns.net/v2/widgets/ticker-sponsored.html
  (ticker-v2 options:  ?states=OH,KY,IN   ?exclude=TO_A,SV_A   ?test=emergency)


SPONSOR LOGOS ON THE TICKER (ticker-v2)
  Put your logo PNGs in the widgets folder, alongside the ticker:
      dashboard-backend\_internal\widgets\yourlogo.png
  Then create a sponsors.json in that SAME folder (see sponsors.example.json):
      { "sponsors": [ { "logo": "yourlogo.png" }, { "text": "Local Business" } ] }
  Refresh the browser source - logos rotate in the bottom-right slot. Edit the
  JSON / swap PNGs anytime; no rebuild needed. Logos auto-fit a ~190x55 slot, so
  use transparent PNGs sized roughly to that. Rotation speed: ?sponsor_speed=15000
  (ms). You can also pass sponsors inline via the URL: ?sponsors=[{"logo":"x.png"}]
  (URL-encoded).


AUTOMATIC UPDATES (recommended)
  Once you're running a build that includes apply-update.ps1 + version.json (this
  one and newer), the dashboard updates itself from GitHub - no more copying zips.

  How it works:
    - The dashboard checks the release channel every ~30 min. When a newer build
      is published it shows a blue "Dashboard update available" banner at the top.
    - Click "Update now" (from the dashboard on THIS PC or your LAN - the button is
      blocked over the public /v2 URL so nobody outside can force a restart). It
      downloads the new bundle, verifies its SHA-256, backs up the current app to
      _backup\, swaps it in, and restarts. Your .env / data\ / Caddyfile are kept.
    - The page reconnects on the new version automatically.

  Nothing to install - it's built in. To publish a new build from the dev PC:
      bash packaging/windows/build ... (build-windows.bat) then
      bash packaging/windows/publish-dashboard-bundle.sh
  which uploads the zip + manifest to the dashboard-releases GitHub repo. Within
  ~30 min (or on next dashboard load) the banner appears here.

  Turn it off:  set  DASHBOARD_UPDATE_ENABLED=false  in .env.


MANUAL UPDATE (fallback / first-time bootstrap)
  1. On the dev PC run build-windows.bat -> produces AlertDashboardV2-Server.zip
  2. Copy that zip to this PC and extract it.
  3. Make a folder named  _update  next to start-server.bat, and put the new
     "dashboard-backend" folder inside it  (=> _update\dashboard-backend\...).
  4. Run update.bat. It stops the server, swaps in the new app, keeps your
     .env / data\ / Caddyfile, backs up the old app to _backup\, and restarts.

  Quick shortcut for widget-only tweaks (no rebuild): drop the changed files
  into  dashboard-backend\_internal\widgets\  and refresh the browser source.


STOPPING
  Close the start-server.bat window (stops the backend). Close the Caddy window
  too, or run:  taskkill /f /im caddy.exe  &  taskkill /f /im dashboard-backend.exe

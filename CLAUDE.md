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

### Radar and Meteoroligcal Information

Advanced Meteorological Visualization: Transitioning to Native WebGL Volumetric Radar Rendering
1. The Paradigm Shift in Meteorological Application Architecture
The evolution of digital weather visualization has reached a critical inflection point, transitioning from legacy, server-side rasterized imagery to native, client-side volumetric rendering. Historically, weather dashboards and web applications have relied on intermediate Application Programming Interfaces (APIs), such as the Iowa State Mesonet RadMap API, to fetch pre-rendered, smoothed Portable Network Graphics (PNG) or Graphics Interchange Format (GIF) images.1 While this methodology is functional for low-bandwidth environments and rudimentary visualizations, it inherently limits visual fidelity, introduces severe API polling latency, and entirely precludes dynamic, client-side data manipulation. Generating PNG images on a backend using libraries like Pillow bloats payload sizes and destroys the underlying floating-point precision of the raw meteorological data, converting scientific measurements into flat color pixels.1
Elite meteorological applications have abandoned this rasterized intermediary approach entirely. Instead, they ingest raw, binary weather radar data directly from primary dissemination networks, decode the radial sweeps in memory, and utilize the client device's Graphics Processing Unit (GPU) to render the data in its original polar coordinate space.2 This approach ensures that users see exactly what the radar sees, preserving the high-resolution integrity of severe weather signatures. To elevate an existing alert dashboard to this professional tier, developers must engineer a sophisticated data pipeline capable of handling atmospheric physics, advanced algorithms, and high-performance WebGL visualization.
This report provides an exhaustive analysis of the data acquisition, algorithmic processing, and graphics rendering pipelines required to achieve this transformation. It deconstructs the infrastructure of industry-leading applications, analyzes the mathematical and programmatic handling of raw weather data, evaluates the current state of typical application architectures, and outlines a comprehensive technical migration strategy. This strategy culminates in a highly optimized, directive-based prompt designed for autonomous coding agents to execute the complex system upgrade.
2. Forensic Analysis of Elite Radar Applications
To engineer a superior radar system, one must first deconstruct the methodologies, technical architectures, and feature sets of the industry leaders. The applications discussed below represent the pinnacle of consumer and professional meteorological visualization, each focusing on distinct paradigms of data ingestion, algorithmic overlay, and rendering technology.
2.1 RadarScope: The Benchmark for Native Radial Rendering
RadarScope is widely considered the gold standard for mobile and desktop radar visualization among meteorologists and severe weather enthusiasts.3 Its primary architectural differentiator is its strict adherence to displaying native radar data in its original radial format, completely bypassing the smoothing and interpolation techniques used by mass-market weather applications.2
RadarScope ingests both Next-Generation Radar (NEXRAD) Level III and Super-Resolution Level II data directly from the National Weather Service (NWS) and specialized third-party aggregators.2 Rather than converting this data into a Cartesian grid on a remote server, the application utilizes native graphics libraries, such as Metal on Apple platforms and DirectX on Windows, to map the radial data dynamically.2 This direct-to-GPU approach ensures that critical mesoscale features remain sharply defined. When an observer is scanning for the tell-tale hook echo of a mesocyclone, identifying a velocity couplet, or searching for the high-reflectivity debris ball associated with a Tornado Vortex Signature (TVS), the lack of pixel bleeding is paramount.2
Furthermore, RadarScope excels in its data aggregation redundancy. It natively integrates the AllisonHouse API, which serves as a highly reliable private data server, bypassing the latency and occasional downtime experienced on public NWS servers.5 The application also natively incorporates the Spotter Network API, an integration that allows the system to overlay the real-time Global Positioning System (GPS) coordinates of trained storm spotters and chasers directly onto the radar display.3 This integration transforms the radar application from a passive viewing tool into an active, ground-truth verification system, enabling public safety officials to coordinate reporting and verify radar-indicated signatures with visual confirmation.
2.2 WeatherWise: Volumetric 3D and Ultra-Low Latency
While RadarScope sets the standard for 2D radial accuracy, WeatherWise introduces advanced spatial rendering and ultra-low latency ingestion pipelines, redefining how users interact with the atmosphere.8 A defining feature of WeatherWise is its FastScan technology, which is engineered to provide ultra-low-latency radar scan updates.9 Instead of waiting for a radar site to complete a full 5-to-10-minute volume scan, FastScan processes partial volume scans, or "chunks," the moment they are generated and transmitted by the radar site.8 This event-driven architecture ensures that users are the first to see rapidly developing phenomena, such as a tightening circulation or a newly formed hook echo, drastically reducing the lead time for severe weather identification.9
WeatherWise also pioneers the use of Volumetric 3D Radar within consumer applications.8 Moving beyond traditional two-dimensional Plan Position Indicators (PPI), the application utilizes a WebGL-driven 3D rendering engine to simulate the radar beam's gradual rise from the tower.8 Users can manipulate the camera pitch to view storm structures vertically, allowing them to evaluate the physical depth of high-reflectivity cores and calculate real-time cross-sections.8 By providing a lifelike, three-dimensional format, WeatherWise allows meteorologists to see how storms stack up in the sky, providing critical insights into updraft strength and hail production potential.8 Additionally, while RadarScope relies on raw pixels, WeatherWise offers configurable GPU-level smoothing, blending adjacent pixels via fragment shaders to reduce visual noise while maintaining the underlying mathematical data integrity.8
2.3 GRLevel3: Exhaustive Algorithmic Overlays
Gibson Ridge's GRLevel3 is a Windows-based application that remains a dominant force within the professional storm-chasing and meteorological research communities.11 Its architecture is distinguished by its unparalleled ability to parse, interpret, and overlay derived algorithmic products directly on top of base reflectivity data.13
GRLevel3 actively parses algorithms generated by the radar's internal computational systems, such as the New Mesocyclone Detection (NMD) algorithm.13 The application translates this complex numerical data into immediately actionable symbology. The rendering engine overlays geometric shapes to denote specific hazards: a filled triangle indicates a Tornado Vortex Signature (TVS), a hollow triangle denotes an Elevated TVS, and a red ring signifies a detected mesocyclone.13 Furthermore, the application provides deep data introspection. When a user hovers over an NMD icon, the system reveals the underlying kinematics, including the Low-Level Rotational Velocity (LLRV), the Low-Level Delta Velocity (LLDV), the base and depth of the rotation, and the Mesocyclone Strength Index (MSI).13 GRLevel3 also excels in processing probability algorithms, displaying distinct icons for the Probability of Severe Hail (POSH) and the Probability of Hail (POH), transforming raw sweeps into comprehensive severe weather tracking workstations.13
2.4 WeatherFront: Multi-Dimensional Data Fusion
WeatherFront approaches meteorological visualization by emphasizing data fusion, seamlessly integrating high-resolution radar data with advanced numerical weather prediction models.15 The architecture allows users to overlay real-time Super-Resolution radar products, such as Reflectivity and Storm-Relative Velocity, directly onto complex model outputs.15
The application ingests datasets from the High-Resolution Rapid Refresh (HRRR), the Rapid Refresh Forecast System (RRFS), and the Global Forecast System (GFS), enabling a comparative analysis of current radar observations against short-term computational forecasts.15 Furthermore, WeatherFront integrates 1-minute Rapid Scan Meso Sectors from the GOES-16 and GOES-18 satellites, utilizing the Advanced Baseline Imager (ABI) to provide a synchronized view of optical cloud tops and the underlying radar returns.15 This fusion of geostationary satellite data, Doppler radar, and numerical modeling provides a holistic, multi-dimensional view of the atmospheric environment.
2.5 Feature Comparison Matrix
The following table synthesizes the architectural and feature-level distinctions among the analyzed applications, serving as a benchmark for the proposed target architecture.
Feature Matrix
RadarScope
WeatherWise
GRLevel3
WeatherFront
Proposed Target Architecture
Rendering Engine
Native (Metal/DirectX)
WebGL 3D / Native
Windows GDI/DirectX
Native iOS
WebGL 2.0 / Custom Shaders
Data Resolution
Super-Res (250m)
Super-Res (250m)
Level III / Super-Res
Super-Res (250m)
Super-Res (250m) & Level II
Latency Paradigm
Polling
Event-Driven (FastScan)
Polling / Streaming
Polling
Event-Driven (WebSockets)
Volumetric 3D
No
Yes (Pro Tier)
No (Requires GREarth)
No
Yes (Ray-Marching Shaders)
Algorithm Overlays
Yes (NMD, TVS, Tracks)
Yes (Derived Tracks)
Exhaustive (NMD, Hail)
Moderate
Exhaustive (SCIT, LLSD, TDS)
Model Data Fusion
No
No
Minimal
Extensive (HRRR, GFS)
Moderate (Parameter Spaces)

3. The Physics and Operations of the NEXRAD System
To accurately render and interpret weather radar data, the software architecture must mathematically account for the physical realities of the Weather Surveillance Radar - 1988, Doppler (WSR-88D) system. The NEXRAD network comprises 160 high-resolution Doppler radars operated by the NWS, the Federal Aviation Administration (FAA), and the U.S. Air Force.16
3.1 Hardware, Wavelengths, and Volume Coverage Patterns
The WSR-88D is an S-Band radar, emitting electromagnetic energy at a 10 cm wavelength and operating at a frequency between 2,700 and 3,000 MHz.16 This specific wavelength is optimal for meteorological applications because it experiences minimal attenuation, allowing the beam to penetrate heavy precipitation without severe signal degradation, unlike shorter-wavelength C-Band or X-Band radars.16
The radar system operates in two fundamental modes. Clear Air Mode (Mode B) is a slow-scanning routine used for analyzing air movements, atmospheric boundaries, and biological scatterers when precipitation is absent.16 Precipitation Mode (Mode A) utilizes a faster scanning strategy to track active weather.16 Within these modes, the radar employs various Volume Coverage Patterns (VCPs).16 A VCP dictates a specific series of 360-degree azimuthal sweeps at pre-determined elevation angles and pulse repetition frequencies.16
The radar beam is sent into the atmosphere at varying angles, or tilts, relative to the horizon.18 The lowest elevation angle is typically 0.5 degrees, while the highest tilt in a severe weather VCP can reach nearly 20 degrees.18 A complete VCP volume scan requires between 4.5 and 10 minutes to finish, depending on the operational mode and the number of elevation slices required.16 As of 2008, the network was upgraded to provide Super Resolution data, drastically improving the fidelity of the sweeps. Super Resolution provides radar reflectivity at 0.5-degree azimuthal by 250-meter range gate resolution out to a range of 460 kilometers, representing a massive increase in data density over the legacy 1.0-degree by 1-kilometer resolution.17
3.2 Dual-Polarization Variables and Microphysics
The transition to dual-polarization technology represents a monumental leap in radar meteorology. Traditional radars transmitted only horizontal radio waves, measuring the total scattered energy to estimate precipitation intensity. Dual-polarization radars transmit and receive both horizontal and vertical pulses, allowing the system to determine not just the intensity of a target, but its shape, orientation, and phase.17
Understanding and rendering these dual-polarization variables is critical for advanced hydrometeorological analysis. The primary variables include Base Reflectivity (Z), which remains the standard measure of target density and echo intensity measured in decibels (dBZ).19 However, the analytical power lies in the differential metrics. Differential Reflectivity (ZDR) measures the ratio of reflected horizontal power to vertical power.19 Because large raindrops flatten into oblate spheroids as they fall, they return a stronger horizontal signal, yielding a positive ZDR. Conversely, hail tumbles randomly and appears spherically symmetrical to the radar, yielding a ZDR near zero.19
The Correlation Coefficient (CC) measures the consistency of the shapes and sizes of targets within a specific radar volume.19 A uniform area of liquid rain will have a CC very close to 1.0 (typically 0.98 or higher). A mixture of rain, melting snow, and hail creates physical diversity within the beam, lowering the CC. Crucially, non-meteorological targets—such as the debris lofted by a tornado—feature highly irregular, non-uniform shapes, causing the CC to drop significantly, often below 0.80.19 Finally, Specific Differential Phase (KDP) measures the difference in phase shift between the horizontal and vertical pulses as they propagate through the precipitation, providing highly accurate estimates of heavy liquid rainfall rates without being contaminated by the presence of hail.19
The following table summarizes the primary dual-polarization variables and their critical severe weather applications.
Dual-Pol Variable
Definition
Unit
Key Meteorological Application
Reflectivity (Z)
Echo intensity and target density
dBZ
Precipitation detection, storm structure, boundary identification.
Velocity (V)
Mean radial movement toward/away from radar
m/s
Mesocyclone detection, straight-line winds, divergence.
Differential Reflectivity (ZDR)
Ratio of horizontal to vertical returned power
dB
Drop size distribution, hail identification, updraft mapping.
Correlation Coefficient (CC)
Consistency of target shapes within a volume
Unitless
Tornado Debris Signatures (TDS), melting layer identification.
Specific Differential Phase (KDP)
Phase shift difference between H/V pulses
°/km
Extreme rainfall estimation, differentiating rain from hail.

3.3 Radar Cross Section and Beam Propagation Mathematics
To implement true volumetric rendering, the software architecture must mathematically account for beam propagation. The curvature of the Earth dictates that even if a radar beam is emitted at a 0.0-degree tilt, it will continually sample higher altitudes the further it travels from the radome.18 Furthermore, the atmosphere refracts the radar beam, bending it slightly toward the Earth's surface under standard conditions.
The shader logic driving the 3D WebGL visualizations must elevate the data points based on the Standard Atmosphere Refraction Model. The height of the radar beam () above the ground at a given slant range () is calculated using the following equation:

Where  represents the Earth's radius,  is the effective earth radius multiplier (typically estimated at 4/3 to account for standard atmospheric refraction),  is the elevation angle of the radar tilt, and  is the physical height of the radar tower above sea level. Implementing this mathematical transformation transforms a flat 2D image into an accurate 3D spatial representation, allowing users to accurately gauge the altitude of overhanging supercell anvils or the vertical depth of intense hail cores.8
Furthermore, advanced analysis relies on calculating the Radar Cross Section (RCS), denoted as . The RCS is a measure of the electromagnetic energy that a target intercepts and scatters back toward the receiver.21 By isolating the  value from the standard radar equation, the software can determine the exact physical profile and scattering characteristics of the storm cell, a calculation that is foundational for precise hail size estimation algorithms.21
4. Advanced Algorithmic Interpretation and Data Derivation
Raw radar data achieves its highest utility when processed through sophisticated meteorological algorithms. The backend infrastructure must be designed to execute complex mathematical derivations in real-time, translating floating-point matrices into actionable severe weather intelligence.
4.1 The Hydrometeor Classification Algorithm (HCA)
The Hydrometeor Classification Algorithm (HCA) is a sophisticated computational tool that ingests the full suite of dual-polarization variables (Z, ZDR, CC, KDP) alongside environmental data, such as the height of the melting layer derived from numerical models, to automatically classify radar echoes into distinct precipitation types.19
The HCA employs fuzzy logic, a computational approach that abandons rigid binary thresholds in favor of assigning probabilistic "membership values" to various categories based on the input matrices.19 For example, a pixel exhibiting high reflectivity ( dBZ), combined with low differential reflectivity ( dB) and slightly reduced correlation coefficient (), would receive a high membership value for the "Hail" or "Rain/Hail Mixture" category.19 The algorithm evaluates these parameters globally, classifying echoes into numerous categories, including Heavy Rain (HR), Graupel (GR), Wet Snow (WS), Dry Snow (DS), Ice Crystals (IC), Biological Scatterers (BS), and Ground Clutter (GC).19 By executing this fuzzy logic matrix across every pixel in the radial sweep, the system acts as an expert system, inferring the most likely hydrometeor type and presenting it as a visually distinct overlay.19
4.2 Hail Detection and Maximum Estimated Hail Size (MESH)
Earlier iterations of severe weather software relied on the single-polarization Hail Detection Algorithm (HDA), which calculated the probability of severe hail by analyzing vertical profiles of reflectivity and noting how high the maximum reflectivity core extended above the environmental freezing level.19 While foundational, the HDA lacked the direct microphysical insight provided by modern dual-polarization variables.19
The target architecture must implement the Maximum Estimated Hail Size (MESH) algorithm. MESH is calculated by integrating the reflectivity properties of a storm cell above the environmental 0°C level, utilizing the Severe Hail Index (SHI) as a primary mathematical input.19 Advanced implementations of MESH utilize a Multi-Radar, Multi-Sensor (MRMS) approach.19 By synthesizing radial data from multiple overlapping radar sites, the algorithm mitigates the physical limitations inherent in single-site observations, such as the cone-of-silence directly above the radome, beam broadening at extreme ranges, and terrain blockage.19 Furthermore, the algorithm integrates mesoscale model analysis data to apply dynamic temperature-altitude proxies across the spatial domain, drastically improving the accuracy of the final hail size estimation.19
4.3 Storm Cell Identification and Kinematic Tracking
The existing codebase demonstrates a solid foundation with its storm_tracking_service.py module, which implements Storm Cell Identification and Tracking (SCIT) methodology.1 The system utilizes a high-to-low thresholding approach, analyzing reflectivity data in descending steps (from 60 dBZ down to 30 dBZ) to identify intense convective cores before mapping their surrounding precipitation halos, strictly filtering out noise components smaller than 5 square kilometers.1
To achieve parity with applications like GRLevel3, the kinematic engine must continually execute azimuthal shear calculations () on the lowest polar velocity sweeps (tilts ).1 The engine actively scans for Mesocyclones by requiring a minimum rotational velocity of 15 m/s located strictly between 2 kilometers and 8 kilometers Above Ground Level (AGL), preventing the misclassification of upper-level linear wind shear.1 Tornadic Vortex Signatures (TVS) are flagged when the gate-to-gate shear exceeds 25 m/s.1
Furthermore, the engine calculates a composite severity score on a 0-100 scale, categorizing threats from Minimal to Extreme.1 This algorithm utilizes a weighted matrix: Rotation accounts for 16% of the score, Core Reflectivity 15%, Tornado Debris Signatures 14%, Dual-Pol Hail confirmation 13%, and Low-Level Shear Detection 10%.1 Additional parameters include growth trends over a 25-minute temporal window, the presence of Mid-Altitude Radial Convergence (MARC) indicating strong updraft inflow, and Rear-Inflow Jets (RIJ) indicative of damaging straight-line winds behind bow echoes.1
4.4 Integrating the Severe Weather Parameter Spaces Engine
To provide a comprehensive operational picture, the radar analysis must be cross-referenced with the broader atmospheric environment. The system should integrate a Severe Weather Parameter Spaces Engine, which monitors critical thermodynamic and kinematic thresholds to determine the likely convective mode of developing storms.25
By continuously evaluating these parameter spaces from ingested model data (such as the HRRR), the application can dynamically alert users not just to the presence of storms, but to the specific hazards they are pre-conditioned to produce.25 The critical parameters driving this engine are detailed below.
Convective Event / Mode
Primary Parameter
Secondary Parameter
Critical "Red Flag" Value
Discrete Supercell
0-6 km Bulk Shear
Supercell Composite Parameter (SCP)
> 2
Significant Tornado
0-1 km Storm Relative Helicity (SRH)
Mean Layer LCL Height
> 12
QLCS Tornado
Line-Normal 0-3 km Shear
0-3 km MLCAPE
> 21
HSLC Severe
MOSH / SHERB
0-500 m SRH
> 5
Significant Hail
Significant Hail Parameter (SHIP)
700-500 mb Lapse Rate
> 16
Derecho / Bow Echo
0-3 km Shear
Downdraft CAPE (DCAPE)
> 6
Elevated Convection
Most Unstable CAPE (MUCAPE)
Most Unstable CIN (MUCIN)
> 11

5. Raw Data Acquisition and Backend Infrastructure
Achieving the ultra-low latency necessary for a professional alert dashboard requires abandoning legacy HTTP polling mechanisms and fully embracing an event-driven, cloud-native ingestion architecture.
5.1 The AWS Event-Driven Chunked Pipeline
As previously established, waiting for the completion of a full Volume Coverage Pattern introduces unacceptable delays. The NOAA Big Data Project resolved this by transmitting partial volume scans, or "chunks," via Amazon Web Services as a real-time feed.26 The NEXRAD network generates approximately 1,200 chunks per hour, writing them to the public Amazon S3 bucket designated as unidata-nexrad-level2-chunks.26
The target architecture utilizes the AWS Simple Notification Service (SNS) and Simple Queue Service (SQS) to create an event-driven ingestion pipeline.27 The system subscribes a backend worker—such as an AWS Lambda function or a dedicated asynchronous Python microservice—to the specific SNS topic arn:aws:sns:us-east-1:684042711724:NewNEXRADLevel2Archive.27 The moment a new radar chunk is written to the S3 bucket by the NWS, the SNS topic broadcasts a notification. The SQS queue triggers the backend worker, which utilizes the boto3 library to immediately download the chunk into memory.27 This architecture reduces the latency from the physical radar dome to the application's processing backend to mere seconds, matching the performance of WeatherWise's FastScan technology.10
5.2 Decoding and Packing Radial Data
The chunks arrive as highly compressed binary files utilizing a specialized BZip2 block compression format. The real-time Level II transmission blocks contain headers and data sections that are linked and arranged based on scanning order, meaning standard BZip2 decompression tools will fail to process them.30
To overcome this, the backend must utilize specialized meteorological libraries. The Python ARM Radar Toolkit (Py-ART), developed by the Department of Energy, or the nexradaws module, are explicitly designed to parse these unique NetCDF and CF/Radial formats.27 The pyart.io.read_nexrad_archive module ingests the raw file, decompresses the BZip2 blocks, and converts the polar data arrays into manipulatable Python numerical matrices.29
Once decoded, the data must be prepared for the client. The legacy approach of utilizing the Pillow library to draw PNG images on the server is a critical anti-pattern.1 Instead, the backend extracts the radial arrays, normalizes the floating-point values into 8-bit or 16-bit integers to conserve bandwidth, and packs them into a flat binary buffer.32 This binary payload consists of a structured header containing the Radar Site ID, the precise epoch timestamp, the elevation angle, the number of radials, and the number of range gates per radial. Following the header is the continuous block of azimuth angles and the subsequent array of gate values.32
This highly optimized binary payload is then streamed to the React frontend via WebSockets.32 WebSockets natively support binary framing, allowing the connection to be configured with ws.binaryType = 'arraybuffer'.32 This mechanism permits megabytes of raw, uncompressed radial data to reach the client with near-zero serialization overhead, vastly outperforming text-based JSON encoding for dense numerical matrices.32
5.3 Deterministic Alert Ingestion via CAP XML
Visualizing radar is only half the equation; an alert dashboard must precisely map the warnings issued by the NWS. These products originate from the Advanced Weather Interactive Processing System (AWIPS) at local Weather Forecast Offices and are distributed via the NOAA Weather Wire Service (NWWS).34
Legacy systems rely on parsing the plain-text format of these warnings, utilizing fragile Regular Expressions to extract coordinates from the appended LAT...LON block.34 This whitespace-sensitive format is highly susceptible to parser failure caused by manual typographical errors from forecasters or unexpected line breaks.34 Furthermore, legacy systems often route alerts based solely on the Universal Geographic Code (UGC), alerting an entire county even if a storm cell only clips its extreme corner, leading to severe "over-warning" fatigue.34
The target architecture must strictly ingest the Common Alerting Protocol (CAP) XML feed.34 CAP parsing is deterministic and robust, utilizing standard Document Object Model (DOM) traversal validated against an XSD schema.34 In the CAP format, the <polygon> element is an explicitly tagged, first-class citizen.34 This structure supports complex geometries that are immediately ingestible by Geographic Information Systems (GIS) rendering engines, enabling precise, location-based alerting without the need for error-prone text scraping.34 Furthermore, CAP supports <resource> blocks for linking digital assets and allows multiple <info> blocks to package multilingual translations within a single cohesive envelope.34
6. High-Performance WebGL Visualization Architecture
The defining characteristic of an elite radar application is its rendering engine. Transitioning away from standard DOM-based mapping overlays, the dashboard must implement a custom WebGL pipeline to render the binary arrays natively on the client's GPU, executing millions of calculations per frame to maintain a fluid 60 frames-per-second experience.2
6.1 The 2D Radial Fragment Shader Pipeline
When the React frontend receives the ArrayBuffer via the WebSocket, it loads this binary data into a WebGL Texture (gl.TEXTURE_2D).37 In this context, the texture does not represent an image; it acts as a massive data matrix where the X-axis represents the range gates and the Y-axis represents the azimuth radials.37
The transformation from the Cartesian screen space of the user's monitor back to the radar's polar coordinate space occurs entirely within the Fragment Shader.39 For every pixel rendered on the screen, the shader mathematically calculates its distance and angle relative to the physical radar tower's coordinates.39
The GLSL shader executes the following core logic across all GPU cores simultaneously:
It determines the current pixel's position relative to the radar center (vec2 uv = fragCoord.xy - radarCenter.xy).
It calculates the distance, or Rho, which corresponds to the range gate (float distance = length(uv)).
It calculates the angle, or Theta, which corresponds to the azimuth (float angle = atan(uv.y, uv.x)).
It normalizes the angle into the 0.0 to 1.0 texture coordinate space.39
With the precise polar coordinates calculated, the shader samples the TEXTURE_2D radar data matrix.40 Once the raw physical value is retrieved, a secondary one-dimensional texture—acting as a Color Lookup Table (LUT)—is queried to apply the exact meteorological color scale (e.g., the standard NWS reflectivity scale or the dual-pol correlation coefficient scale).41 Because this mathematical transformation occurs at the hardware level, the rendering is instantaneous, allowing the user to pan, zoom, and manipulate the map fluidly without the pixelation or blocking inherent in raster tiles.2
6.2 Implementing Volumetric 3D Ray-Marching
To achieve parity with WeatherWise's 3D capabilities, the WebGL architecture must implement volumetric rendering.8 Instead of rendering a single 0.5-degree tilt, the application receives multiple elevation slices from the backend, stacking them into a 3D volumetric texture.35
Rendering this volume requires a sophisticated Ray-Marching algorithm within the shader.44 The shader initializes a virtual 3D ray for each pixel originating from the user's camera perspective.44 This ray mathematically steps, or "marches," through the 3D volume.44 At each step, it samples the stacked radar texture, evaluating the density and reflectivity of the voxel.46 By applying the Standard Atmosphere Refraction Model equation detailed earlier, the shader correctly elevates the data points, curving the radar beam upward to match reality.18
This ray-marching technique enables the visualization of complex atmospheric structures. Users can view the overhanging anvil of a supercell, calculate the exact physical depth of a hail core, and identify the bounded weak echo region (BWER) characteristic of intense updrafts, transforming the dashboard from a 2D map into a comprehensive meteorological workstation.8
6.3 Temporal Data Integration: Instanced Lightning Rendering
The existing application architecture handles lightning data by fetching Geostationary Lightning Mapper (GLM) files from an open AWS S3 bucket (noaa-goes18), tracking flashes within a 15-minute rolling window.1 Currently, rendering hundreds of lightning strikes as individual DOM elements creates massive performance bottlenecks during severe convective outbreaks.
In the upgraded WebGL architecture, the WebSocket streams the precise lightning coordinates (flash_lat, flash_lon) and their optical radiant energy (flash_energy) directly to the frontend.1 The React application pushes these coordinates into an Instanced Buffer within WebGL. The GPU renders the flashes using a custom particle system shader.1 The shader applies a procedural Gaussian blur bloom effect, scaling the intensity and size of the bloom dynamically based on the specific flash_energy parameter.47 As the flashes age within the 15-minute window, the shader smoothly fades them out by manipulating their alpha channel based on their epoch timestamp, ensuring the visualization remains performant regardless of the strike density.1
7. Refactoring the Existing Codebase
An analysis of the provided system architecture reveals a solid structural foundation that requires targeted refactoring to support this new high-performance paradigm.
7.1 State Management and the React Frontend
The current App.tsx implementation correctly utilizes a WebSocket-to-React state pattern.1 Managing a global radarFrame and maintaining a Record<string, RadarFrame> for tracking multi-site overlays simultaneously is a robust architectural choice.1
However, the RadarFrame type definition must be heavily refactored. Currently, it is designed to handle string-based image URLs.1 It must be updated to accept and manage the raw ArrayBuffer payloads streamed from the new WebSocket architecture.32 Furthermore, the reflectivityFrameRef mechanism remains critical for ensuring the application has immediate access to the latest data.1 Instead of referencing a static image for the off-screen AlertMapGraphic renderer, it will now hold the binary state. A headless WebGL context will execute the shader math and render the data into a high-resolution dataURL, which is then exported to the backend for social media dissemination.1
7.2 Backend Python Services Integration
The existing radar_service.py requires complete deprecation.1 While its Singleton pattern and asynchronous I/O management are structurally sound, its reliance on polling the radmap.php endpoint and rendering visual crosshairs via the Pillow library is fundamentally incompatible with a native radial rendering pipeline.1
Conversely, the analytical logic within storm_tracking_service.py and glm_service.py is highly sophisticated and must be preserved.1 The SCIT algorithms, the kinematic signature detection, and the 0-100 severity scoring matrix are operating at a professional tier.1
The critical refactoring step involves repositioning these analytical services. They must sit directly downstream of the new Py-ART/AWS decoding layer. The backend will pull the raw NetCDF chunk data, instantly pass the matrices through the kinematics engine to calculate the tracking metadata (TVS, MESO vectors, Severe Hail probabilities), and then tightly package both the raw radial binary data and the derived JSON metadata into a single, cohesive WebSocket payload.1 This ensures that the frontend receives the raw visualization data perfectly synchronized with the algorithmic intelligence.

---

## ML Rotation Classifier — Operational Workflow & Lessons

The `storm_tracking_service` ships with an optional ML rotation classifier wired
to nudge the per-cell severity score when its prediction disagrees with the
physics detectors. The complete data pipeline:

### Data flow

1. **Collect** — `live_qa_service.py` (in-process when
   `LIVE_QA_LOG_TRAINING_DATA=true`) or `live_qa.py --log` (standalone CLI)
   appends one row per cell per scan to `data/training_data.jsonl`. Each row
   has 25 features + `label: null` and the per-flag context (rotation
   detected, TDS, BWER, MESH band, etc.).
2. **Label** — `scripts/label_from_warnings.py` pulls NWS warning polygons
   from the IEM SBW archive and assigns labels via point-in-polygon plus a
   time window match. Use `--strict-tornado` to count only TO.W as positive
   (SVR-only matches stay unlabeled / ambiguous). The inner loop is
   vectorized with numpy — 50k rows × 26k warnings runs in seconds.
3. **Train** — `scripts/train_rotation_model.py` builds a class-balanced
   GradientBoosting classifier, then wraps it in
   `CalibratedClassifierCV(method='isotonic')` against a held-out 20% set.
   Saves to `data/rotation_model.joblib`.
4. **Deploy** — backend restart auto-loads the model
   (`storm_tracking_service.load_rotation_model`). Per-cell predictions
   populate the `p_rotation_model` field; a conservative ±2 score nudge
   fires only at `p ≥ 0.80` (boost) or `p < 0.10` (demote).

### Required CLI environment on Windows

The labeler prints arrows (`→`) which crash the default `cp1252` console.
Always invoke with UTF-8 forced:

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python -u scripts/label_from_warnings.py ...
```

### Lessons from training iterations

| Iteration | Approach | Result | Diagnosis |
|---|---|---|---|
| v1 | TOR + SVR both → label=True | AUC 0.814, recall 32% on positives | Model learned "severe storm" via `area`, `vil`, `max_dbz`. Rotation features absent from top-10 importances. SVR labels diluted the signal. |
| v2 | `--strict-tornado` (TOR only), no class weights | AUC 0.825, recall 99.5% on positives but 25% on negatives | 79% positive class → model over-predicts positive. Confusion matrix lopsided. Feature importances now rotation-led (`llsd_max_shear` #1). |
| v3 | Strict TOR + inverse-class-frequency `sample_weight` + isotonic calibration on held-out 20% | AUC 0.817, recall 95% pos / 53% neg, **Brier 0.169 → 0.123** | Calibration recovered the probability semantics so `p=0.5` actually means 50%. Class balance restored true negative recall. Operationally usable. |

**Headline lesson:** the class balance and probability calibration matter more
than absolute AUC. AUC measures ranking; the score-nudge wiring uses fixed
probability thresholds (0.10 / 0.80), so calibrated outputs are required for
the thresholds to mean anything. Validate every retrain via the Brier score
on a held-out set, not just CV-AUC.

### Backup / rollback

The trainer overwrites `data/rotation_model.joblib` unconditionally. Before
each retrain, snapshot the existing model:

```bash
cp data/rotation_model.joblib data/rotation_model.previous.joblib
cp data/training_data.jsonl    data/training_data.jsonl.bak
```

If a new model regresses in the field, restore the snapshot and restart the
backend.

### Future improvement roadmap

Ordered by effort-to-payoff ratio. Researched against current operational
practice + recent literature (see references below).

**Tier 1 — quick wins (hours)**
- **Temporal train/test split** instead of random 5-fold. The current
  `StratifiedKFold` leaks because the same cell often appears in both
  splits. A chronological split gives an honest generalization estimate.
- **Cell-level deduplication.** One supercell scanned for 90 min contributes
  ~25 highly-correlated rows. Subsample to one row per cell per VCP.
- **`p_rotation_model` time smoothing** (exponential moving average over
  recent scans) to reduce scan-to-scan flicker on the dashboard.

**Tier 2 — medium effort, real payoff (~½–1 day each)**
- **MRMS rotation tracks as a feature.** NSSL publishes multi-radar
  azimuthal shear (0–2 km AGL) and rotation tracks (30-min running max) on
  AWS `s3://noaa-mrms-pds/`. Pulling the value at each cell's lat/lon at
  each scan would add the single strongest available radar-derived signal
  we don't already have. Critical caveat: also using it as a label source
  is mildly circular; pick one role.
- **LSR-based positive labels** to mix with TOR-warning labels. Confirmed
  tornado touchdowns are the gold standard but sparse. Mix via
  `sample_weight = 2.0` for LSR matches, `1.0` for warnings.
- **HRRR environmental features.** SPC composite parameters (SCP, STP, SRH,
  MLLCL) at cell location/time are complementary to our radar features.
  Build an HRRR ingester (also unlocks dynamic MESH freezing level).
- **Hard negative mining** on clear-air days. Schedule a few `live_qa --log`
  sessions during quiet weather to harvest negatives and rebalance the
  collected class distribution.

**Tier 3 — bigger investments (multi-day)**
- **Switch to LightGBM/XGBoost** with hyperparameter tuning. Typically
  picks up 0.02–0.05 AUC on tabular data + supports class weighting +
  monotone constraints natively.
- **Sequence model** (small LSTM or transformer) over the 5-scan trend
  window. Currently we collapse temporal history to linear slope features;
  a sequence model captures non-linear patterns (e.g., "steady then sharp
  jump" vs "linear ramp" both give the same slope but mean different
  things meteorologically).
- **TorNet integration.** MIT Lincoln Lab's open dataset has 200k full-
  polarimetric radar images (13,587 confirmed tornadoes). Either use their
  pretrained CNN's embeddings as features in our model, or replace our
  classifier entirely with their CNN architecture fine-tuned on our data.
  Heavy compute (Raspberry Pi will struggle); the embeddings path is more
  practical.

**Tier 4 — operational polish**
- **Adaptive thresholds by environmental setup.** Lower the `p_rotation_model`
  threshold when SCP > 4 (environment primed for supercells); raise it when
  SCP < 0. Requires HRRR ingest first.
- **Multi-radar voting** for cells in Voronoi-overlap regions. Average
  `p_rotation_model` from each radar's processing instead of single-radar
  pick.

### External references

ML / SOTA radar classifiers:
- [TorNet: Benchmark dataset for tornado detection (arxiv 2401.16437)](https://arxiv.org/abs/2401.16437)
- [TorNet: AMS AIES journal version](https://journals.ametsoc.org/view/journals/aies/4/1/AIES-D-24-0006.1.xml)
- [TorNet: MIT Lincoln Lab announcement](https://www.ll.mit.edu/news/ai-dataset-carves-new-paths-tornado-detection)

NEXRAD / MRMS operational algorithms:
- [NSSL Mesocyclone Detection Algorithm (Weather & Forecasting 1998)](https://journals.ametsoc.org/view/journals/wefo/13/2/1520-0434_1998_013_0304_tnsslm_2_0_co_2.xml)
- [MRMS overview & severe-weather products (BAMS 2016)](https://journals.ametsoc.org/view/journals/bams/97/9/bams-d-14-00173.1.xml)
- [MRMS Rotation Tracks (WDTD training)](https://vlab.noaa.gov/web/wdtd/-/rotation-trac-3)
- [MRMS Azimuthal Shear (WDTD training)](https://vlab.noaa.gov/web/wdtd/-/azimuthal-shear)

Environmental ingredients:
- [Supercell Composite Parameter (Wikipedia)](https://en.wikipedia.org/wiki/Supercell_composite_parameter)
- [Storm-Relative Helicity & tornado forecasting (Weather & Forecasting 2019)](https://journals.ametsoc.org/view/journals/wefo/34/5/waf-d-19-0115_1.xml)
- [Supercell environments using GridRad-Severe + HRRR (arxiv 2503.15466)](https://arxiv.org/abs/2503.15466)

Class imbalance / calibration:
- [Machine-learning classifiers for imbalanced tornado data (Springer)](https://link.springer.com/article/10.1007/s10287-013-0174-6)
- [Focal Loss overview (Ultralytics)](https://www.ultralytics.com/glossary/focal-loss)

Labeling sources:
- [IEM Storm Based Warning archive](https://mesonet.agron.iastate.edu/request/gis/watchwarn.phtml) — used by `label_from_warnings.py`
- [NWS Storm Reports (LSRs)](https://www.weather.gov/lsr/) — used by `label_from_lsr.py`

### MRMS Rotation Tracks feature

`backend/services/mrms_rotation_service.py` polls the `noaa-mrms-pds` bucket
every 2 minutes for two GRIB2 products and caches the latest CONUS grid for
each:

| GRIB2 product | What it measures | Used as feature |
|---|---|---|
| `RotationTrack30min_00.50` (with `RotationTrackML1440min_00.50` fallback) | 30-min running max of 0–2 km azimuthal shear, per grid cell | `mrms_rotation_track_30min` |
| `MergedAzShear_0-2kmAGL_00.50` | Instantaneous multi-radar fused low-level azimuthal shear | `mrms_azshear_0_2km` |

Both default to 0.0 when MRMS is unavailable (no eccodes, S3 issue, lat/lon
outside CONUS) so the model degrades gracefully rather than crashing.

**Sampling.** The service maintains 2D numpy grids per product. Per-cell
lookup uses nearest-neighbour indexing against the standard MRMS CONUS bounds
(20–55°N × −130 to −60°W, 0.01° step). The same code path serves both live
training-data collection (`live_qa_service.extract_features`) and live ML
inference (`storm_tracking_service._cell_to_feature_vector`).

**Backfilling existing training data.** Existing rows in
`data/training_data.jsonl` were collected before this feature was wired in
and have no MRMS values — they'd default to 0.0 during training, so the
model wouldn't learn to use the feature. To enrich them retroactively:

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
  python -u scripts/backfill_mrms_features.py
```

The script groups rows by 2-minute time bin so each MRMS file is downloaded
exactly once and used to sample every row in that bin. With 8 parallel S3
workers (the default) and ~5000–15000 unique bins for a 60-day dataset,
expect total runtime in the hours and bandwidth in the 30–50 GB range.
Resumable: re-running over the enriched file is a no-op (rows that already
have the features keep them, missing rows get refilled).

After backfilling, retrain with `scripts/train_rotation_model.py` — the
trainer auto-picks up the two new features from the `FEATURE_NAMES` list.

### Wind signatures — two-tier detection

`_detect_straight_line_winds` flags a cell's broad outflow at two thresholds
so developing wind threats are visible *before* they cross the severe
warning floor:

| Tier | Flag | Velocity threshold | Min swath area | Score band | Use case |
|---|---|---|---|---|---|
| Severe | `straight_line_wind_detected` | `SLW_SEVERE_MS = 25.7 m/s` (50 kt — NWS severe) | `SLW_MIN_SWATH_KM2 = 30` km² | 40–100 (RIJ floor 70) | NWS-warning-class damage |
| Strong | `strong_wind_detected` | `SLW_STRONG_MS = 18.0 m/s` (35 kt — sub-severe but damaging) | `SLW_STRONG_MIN_SWATH_KM2 = 15` km² | 10–35 | Developing squall lines, QLCS outflow intensification, gust front pushes |

Both tiers store the peak outbound velocity in `max_wind_velocity_ms` so the
cell card can display "X m/s (Y kt)" regardless of tier.  The analyst service
fires notifications on the first scan a cell crosses either threshold.

Without the strong tier, a developing squall line in the 35-50 kt regime
gets `0` on the `straight_line` score factor — which was the symptom that
prompted adding this two-tier scheme.

**Why this feature matters.** Our single-radar LLSD (`llsd_max_shear`) is
already the model's top feature, but it suffers from beam blockage, the
cone-of-silence directly above each radar, and beam broadening at long range.
MRMS fuses 0.5° azimuthal shear from every NEXRAD site that can see the
gate, producing a denser and less noisy field. The 30-min running max
adds short-term temporal context — "this place was rotating recently" — that
a single-scan model can't capture without the trend window. Combined, these
two features close the operational gap to RadarScope / GR2Analyst's
multi-radar rotation overlays.

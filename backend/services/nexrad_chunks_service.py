"""
NEXRAD Level 2 Chunks-bucket service (near-real-time).

Polls `s3://unidata-nexrad-level2-chunks/<SITE>/` directly via boto3 for
in-flight volume scan chunks, assembles them, and feeds the assembled file
into `NexradService` for parse + render + broadcast — the same downstream
path the archive bucket uses.

Architecture:
  Chunks bucket layout:
      <SITE>/<VolumeNumber>/<YYYYMMDD-HHMMSS-CHUNKNUM-CHUNKTYPE>
  Chunk types: S (Start, contains the volume header), I (Intermediate),
                E (End of volume).

  Per active site, the service tracks open volume folders.  Each new chunk
  is downloaded and appended to an in-memory accumulator.  When the chunk
  count crosses `min_chunks_for_partial` the accumulator is concatenated
  (S first, then I in chunk-number order, then E if present), written to a
  temp file, and passed to `NexradService._process_local_file_sync`.  Py-ART
  will parse the partial volume (it tolerates a missing end-of-volume marker)
  and produce whatever sweeps the chunks have so far.

  Each volume can be rendered up to two times: once when the partial-render
  threshold is crossed, and again at end-of-volume (E chunk) if
  `render_on_complete` is set.  After E, the volume folder is finalized and
  the archive-bucket poller is told (via `mark_chunks_processed`) to skip
  this scan when it eventually appears in the archive.

Runs alongside the existing archive bucket pipeline.  Gated behind
`nexrad_chunks_enabled` (default False).
"""

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore import UNSIGNED
from botocore.config import Config

logger = logging.getLogger(__name__)


CHUNKS_BUCKET = "unidata-nexrad-level2-chunks"

# How recent a chunk's volume-start timestamp must be to be considered for
# processing.  The bucket retains 24+ hours of data; we only care about the
# current minute.  900 s = 15 min is generous enough to handle clock skew
# and slightly delayed VCPs without picking up day-old volumes.
CHUNK_RECENCY_S = 900

# Safety cap on chunks processed for the working volume in a single poll.
# Per-volume now (not per-poll-across-volumes), so this needs to fit a full
# severe-weather VCP with SAILS/MRLE — ~60 chunks worst case.  100 is a
# generous safety net; in normal operation we process the few chunks that
# arrived in the last poll cycle.
MAX_CHUNKS_PER_POLL = 100


# ---------------------------------------------------------------------------
# Per-volume accumulator
# ---------------------------------------------------------------------------

@dataclass
class _VolumeBuffer:
    """In-memory accumulator for one volume scan's chunks (one site)."""
    site: str
    volume_number: int
    first_seen_at: datetime
    chunks: dict[int, tuple[str, bytes]] = field(default_factory=dict)
    # chunks[chunk_num] = (chunk_type, bytes).  chunk_type ∈ {"S", "I", "E"}
    partial_broadcast_ts: Optional[str] = None  # scan_iso of last partial render
    partial_broadcast_chunk_count: int = 0       # chunk count at that render
    partial_broadcast_at: Optional[datetime] = None  # wall-clock of last partial
    complete_broadcast_ts: Optional[str] = None
    last_chunk_added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # SAILS-aware: per-volume set of scan_iso strings we've broadcast.  Used to
    # avoid re-emitting SAILS revisits on every refresh cycle once they've
    # already been delivered as their own timeline entry.
    broadcasted_scan_isos: set = field(default_factory=set)

    def add(self, chunk_num: int, chunk_type: str, data: bytes) -> bool:
        """Add a chunk's bytes.  Returns True if new (False if already had it)."""
        if chunk_num in self.chunks:
            return False
        self.chunks[chunk_num] = (chunk_type, data)
        self.last_chunk_added_at = datetime.now(timezone.utc)
        return True

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def has_start(self) -> bool:
        return any(t == "S" for t, _ in self.chunks.values())

    @property
    def has_end(self) -> bool:
        return any(t == "E" for t, _ in self.chunks.values())

    def assembled_bytes(self) -> Optional[bytes]:
        """Concat S header + ordered I chunks + E.  None if no S chunk yet."""
        if not self.has_start:
            return None
        ordered = sorted(self.chunks.items(), key=lambda kv: kv[0])
        # Sanity: enforce S → I → E order is implicit by chunk_num because
        # NEXRAD numbers chunks 1..N within a volume.  S is chunk 1.
        return b"".join(data for _, (_, data) in ordered)


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------

def _parse_chunk_key(key: str) -> Optional[tuple[str, int, datetime, int, str]]:
    """Parse a chunks-bucket S3 key.

    Returns (site, volume_number, ts, chunk_number, chunk_type) or None.
    Key pattern:  <SITE>/<VOLUME_NUMBER>/<YYYYMMDD-HHMMSS-CHUNKNUM-CHUNKTYPE>
    Example:      KDFX/602/20190510-143508-028-I
    """
    try:
        parts = key.split("/")
        if len(parts) != 3:
            return None
        site = parts[0].upper()
        volume_number = int(parts[1])
        fname = parts[2]
        seg = fname.split("-")
        if len(seg) != 4:
            return None
        ymd, hms, num_str, ctype = seg
        if ctype not in ("S", "I", "E"):
            return None
        ts = datetime.strptime(ymd + hms, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return site, volume_number, ts, int(num_str), ctype
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class NexradChunksService:
    """Polls the chunks bucket and feeds assembled volumes into NexradService.

    The archive-bucket NexradService remains the source of truth for the
    `_frames` cache, `_last_volume`, status broadcasts, etc.  This service
    only produces new data faster.
    """

    def __init__(self, nexrad_svc, settings):
        self._nexrad = nexrad_svc
        self._poll_interval = max(5, int(settings.nexrad_chunks_poll_interval))
        self._min_partial   = max(2, int(settings.nexrad_chunks_min_chunks_for_partial))
        self._render_on_complete = bool(settings.nexrad_chunks_render_on_complete)
        self._partial_refresh_chunks = max(0, int(getattr(settings, "nexrad_chunks_partial_refresh_chunks", 0)))
        self._partial_refresh_min_interval_s = max(0, int(getattr(settings, "nexrad_chunks_partial_refresh_min_interval_s", 60)))

        # Per-site state — we only care about volumes for currently active sites
        self._volumes: dict[tuple[str, int], _VolumeBuffer] = {}
        # Per-site last-seen chunk key (for dedup on the next LIST)
        self._seen_keys: dict[str, set[str]] = {}

        # boto3 client — anonymous because the bucket is public
        self._s3 = boto3.client(
            "s3",
            config=Config(signature_version=UNSIGNED),
            region_name="us-east-1",
        )

        # Per-site diagnostic state
        self._diagnostics: dict[str, dict] = {}

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    # ─── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        logger.info(
            f"NEXRAD chunks service starting (poll={self._poll_interval}s, "
            f"partial_threshold={self._min_partial} chunks)"
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("NEXRAD chunks service stopped")

    # ─── public ───────────────────────────────────────────────────────────
    @property
    def diagnostics(self) -> dict:
        return {s: dict(d) for s, d in self._diagnostics.items()}

    def _diag(self, site: str) -> dict:
        return self._diagnostics.setdefault(site, {})

    # ─── polling loop ─────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                active = [s.upper() for s in self._nexrad.active_sites]
                self._evict_inactive_sites(active)
                for site in active:
                    try:
                        await self._poll_site(site)
                    except Exception as e:
                        logger.error(f"Chunks poll failed for {site}: {e}", exc_info=True)
                # Reap stale volumes (no chunks in 15 min = abandoned)
                self._reap_stale_volumes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Chunks poll loop error: {e}", exc_info=True)
                await asyncio.sleep(self._poll_interval)

    def _evict_inactive_sites(self, active: list[str]) -> None:
        """Drop in-flight state for sites no longer in active_sites.

        Without this, switching the UI from KILN to KUEX leaves a stale
        KILN entry in `_diagnostics` and a 36k-key `_seen_keys[KILN]`.
        """
        active_set = set(active)
        for site in list(self._seen_keys.keys()):
            if site.upper() not in active_set:
                self._seen_keys.pop(site, None)
                self._diagnostics.pop(site, None)
                logger.info(f"Chunks: evicted state for inactive site {site}")
        # Drop volume buffers for inactive sites too
        for buf_key in list(self._volumes.keys()):
            site, _ = buf_key
            if site.upper() not in active_set:
                self._volumes.pop(buf_key, None)

    async def _poll_site(self, site: str) -> None:
        loop = asyncio.get_event_loop()
        diag = self._diag(site)

        # LIST is blocking I/O — run in executor.
        t_list = time.perf_counter()
        try:
            keys = await loop.run_in_executor(None, self._list_site_keys, site)
        except Exception as e:
            diag["last_error"] = f"list: {e}"
            logger.warning(f"Chunks LIST failed for {site}: {e}")
            return
        diag["last_list_duration_s"] = round(time.perf_counter() - t_list, 2)
        diag["last_list_count"]      = len(keys)
        diag["last_poll_at"]         = datetime.now(timezone.utc).isoformat()

        seen = self._seen_keys.setdefault(site, set())
        now = datetime.now(timezone.utc)

        # ── First poll: bootstrap, but keep in-flight volume processable ──
        # The chunks bucket retains 24h+ of data.  We only want to skip
        # *historical* chunks — anything within CHUNK_RECENCY_S of now is
        # part of the currently in-flight volume (whose S chunk may have
        # arrived before we started watching) and should be processed
        # immediately so we can assemble the live scan.
        if not seen:
            old_count = 0
            recent_count = 0
            for k in keys:
                p = _parse_chunk_key(k)
                if p is None or (now - p[2]).total_seconds() > CHUNK_RECENCY_S:
                    seen.add(k)
                    old_count += 1
                else:
                    recent_count += 1
            diag["bootstrapped_at"]        = now.isoformat()
            diag["bootstrap_old_count"]    = old_count
            diag["bootstrap_recent_count"] = recent_count
            logger.info(
                f"Chunks: bootstrap {site} — {old_count} historical keys ignored, "
                f"{recent_count} recent keys queued for this poll"
            )
            # Fall through and process the recent keys in the same poll.

        new_keys = [k for k in keys if k not in seen]
        if not new_keys:
            return

        # ── Recency + parse filter ────────────────────────────────────────
        parsed_keys: list[tuple[str, tuple]] = []
        for k in new_keys:
            p = _parse_chunk_key(k)
            if p is None:
                seen.add(k)
                continue
            ksite, _, ts, _, _ = p
            if ksite.upper() != site.upper():
                seen.add(k)
                continue
            if (now - ts).total_seconds() > CHUNK_RECENCY_S:
                seen.add(k)  # ancient, ignore forever
                continue
            parsed_keys.append((k, p))

        if not parsed_keys:
            return

        # ── Focus on the most recent volume per site ──────────────────────
        # A volume's chunks all share the same start-time embedded in their
        # keys, so identifying "the latest volume" by timestamp picks all of
        # its chunks at once (including the S chunk, which is chunk 1).
        # Dropping older volumes' chunks here means a long VCP can't fill
        # our budget with stragglers from a volume we already missed.
        #
        # BUT: we must still finish a volume we are *actively building*.  At a
        # volume boundary the previous volume's final chunks (notably its E
        # chunk) carry the older timestamp, while the next volume's S chunk
        # carries the newer one.  If both land in the same poll, filtering to
        # `latest_ts` alone would orphan the previous volume one chunk short of
        # completion — and since those dropped keys are never marked `seen`,
        # they lose the race forever.  That volume then never sets `has_end`,
        # so the completion-only consumers (storm tracking via `_render_volume`
        # mark_complete) never fire for it.  Keep chunks for any in-progress
        # buffer so it can receive its E chunk and complete.  Volumes we never
        # started (no buffer) are still ignored, and stale buffers are reaped.
        latest_ts = max(p[2] for _, p in parsed_keys)
        in_progress = {
            vnum for (s, vnum), b in self._volumes.items()
            if s == site and b.complete_broadcast_ts is None
        }
        parsed_keys = [
            (k, p) for k, p in parsed_keys
            if p[2] == latest_ts or p[1] in in_progress
        ]

        # Sort within the volume by chunk number so we apply S → I... → E
        parsed_keys.sort(key=lambda item: item[1][3])

        # Safety cap — should never bite in normal operation (a VCP has
        # ~50-60 chunks max), but bounds memory & download time if something
        # weird happens.
        if len(parsed_keys) > MAX_CHUNKS_PER_POLL:
            parsed_keys = parsed_keys[:MAX_CHUNKS_PER_POLL]
            logger.warning(
                f"Chunks: {site} V{parsed_keys[0][1][1]} clipped to "
                f"{MAX_CHUNKS_PER_POLL} chunks for this poll"
            )

        latest_vol_num = next(
            (p[1] for _, p in parsed_keys if p[2] == latest_ts),
            parsed_keys[0][1][1],
        )
        diag["new_keys_this_poll"]      = len(parsed_keys)
        diag["working_volume_number"]   = latest_vol_num
        diag["working_volume_start_ts"] = latest_ts.isoformat()

        # ── Parallel download ─────────────────────────────────────────────
        # Each chunk is small (tens of KB); parallel GETs amortize round-trip.
        async def _fetch(k: str) -> tuple[str, Optional[bytes]]:
            try:
                data = await loop.run_in_executor(None, self._download_chunk, k)
                return k, data
            except Exception as e:
                logger.warning(f"Chunks GET failed for {k}: {e}")
                return k, None

        download_results = await asyncio.gather(
            *(_fetch(k) for k, _ in parsed_keys),
            return_exceptions=False,
        )
        downloads = {k: d for k, d in download_results if d is not None}

        # ── Apply chunks in timestamp order; broadcast at thresholds ──────
        touched_buffers: set[tuple[str, int]] = set()
        for key, (_, vol_num, _, chunk_num, ctype) in parsed_keys:
            data = downloads.get(key)
            if data is None:
                continue

            buf_key = (site, vol_num)
            existing = self._volumes.get(buf_key)
            if existing and existing.complete_broadcast_ts is not None:
                seen.add(key)
                continue

            buf = self._volumes.setdefault(
                buf_key,
                _VolumeBuffer(site=site, volume_number=vol_num,
                              first_seen_at=datetime.now(timezone.utc)),
            )
            if buf.add(chunk_num, ctype, data):
                seen.add(key)
                touched_buffers.add(buf_key)
                diag["last_chunk_received_at"] = buf.last_chunk_added_at.isoformat()
                diag["last_chunk_key"]         = key

        # Broadcast once per touched buffer at the end — avoids rendering
        # the same partial volume N times if multiple chunks arrived
        # together this poll.
        for buf_key in touched_buffers:
            buf = self._volumes.get(buf_key)
            if buf is not None:
                await self._maybe_broadcast(site, buf)

    # ─── S3 I/O (blocking, run in executor) ───────────────────────────────
    def _list_site_keys(self, site: str) -> list[str]:
        """List all chunk keys under <SITE>/ in the chunks bucket."""
        keys: list[str] = []
        token = None
        prefix = f"{site.upper()}/"
        while True:
            kwargs = {"Bucket": CHUNKS_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                keys.append(obj["Key"])
            token = resp.get("NextContinuationToken")
            if not resp.get("IsTruncated") or not token:
                break
        return keys

    def _download_chunk(self, key: str) -> Optional[bytes]:
        obj = self._s3.get_object(Bucket=CHUNKS_BUCKET, Key=key)
        return obj["Body"].read()

    # ─── broadcast decision ───────────────────────────────────────────────
    async def _maybe_broadcast(self, site: str, buf: _VolumeBuffer) -> None:
        if not buf.has_start:
            return  # cannot parse without the volume header

        # Decide whether to render.  We support three trigger paths:
        #   1. First partial — fires once min_partial chunks have arrived.
        #   2. Refresh partial — fires again as additional chunks accumulate,
        #      throttled by chunk-delta + wall-clock to keep CPU sane.
        #   3. Complete   — fires once at the volume's E chunk.
        # Without (2), a single partial broadcast freezes the displayed scan
        # for the rest of the VCP (up to ~10 min on clear-air modes), which
        # leaves viewers seeing a "10 minutes behind" timestamp.
        now = datetime.now(timezone.utc)
        first_partial = (
            buf.partial_broadcast_ts is None
            and buf.chunk_count >= self._min_partial
        )
        chunks_since_last = buf.chunk_count - buf.partial_broadcast_chunk_count
        seconds_since_last = (
            (now - buf.partial_broadcast_at).total_seconds()
            if buf.partial_broadcast_at is not None else float("inf")
        )
        refresh_partial = (
            buf.partial_broadcast_ts is not None
            and not buf.has_end
            and self._partial_refresh_chunks > 0
            and chunks_since_last >= self._partial_refresh_chunks
            and seconds_since_last >= self._partial_refresh_min_interval_s
        )
        should_partial  = first_partial or refresh_partial
        should_complete = (
            buf.has_end
            and self._render_on_complete
            and buf.complete_broadcast_ts is None
        )
        if not (should_partial or should_complete):
            return

        # Render the most-complete-so-far view.  When E arrived we render the
        # full volume; otherwise we render the partial.
        source_label = "chunks-complete" if buf.has_end else "chunks-partial"
        await self._render_volume(site, buf, source_label, mark_complete=buf.has_end)

    async def _render_volume(
        self,
        site: str,
        buf: _VolumeBuffer,
        source: str,
        mark_complete: bool,
    ) -> None:
        loop = asyncio.get_event_loop()
        diag = self._diag(site)
        assembled = buf.assembled_bytes()
        if assembled is None:
            return

        # Write to a temp file; Py-ART expects a path.
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"{site}_V{buf.volume_number}_",
            suffix=".raw",
            delete=False,
            dir=tempfile.gettempdir(),
        )
        try:
            tmp.write(assembled)
            tmp.flush()
            tmp.close()
            t_proc = time.perf_counter()

            # ── Parse once ────────────────────────────────────────────────
            try:
                radar = await loop.run_in_executor(
                    None, self._nexrad._parse_local_file_sync, tmp.name, site,
                )
            except Exception as e:
                logger.warning(
                    f"Chunks parse failed for {site} V{buf.volume_number} "
                    f"({buf.chunk_count} chunks): {e}"
                )
                diag["last_error"] = f"parse: {e}"
                return
            if radar is None:
                logger.debug(
                    f"Chunks parse returned None for {site} V{buf.volume_number} "
                    f"({buf.chunk_count} chunks) — likely too few sweeps; "
                    f"will retry on next chunk"
                )
                return

            # Dealias velocity ONCE on the full radar.  Sub-radars extracted
            # for SAILS revisits inherit `velocity_dealiased` and skip
            # re-dealiasing in `_process_radar_object`.
            await loop.run_in_executor(
                None, self._nexrad.dealias_radar_in_place, radar,
            )

            # ── Identify low-tilt scans (original + SAILS revisits) ──────
            # Each is a separate timeline event with its own scan_iso.
            scans = self._nexrad._identify_low_tilt_scans(radar)
            if not scans:
                logger.debug(
                    f"Chunks: no low-tilt scans found in {site} V{buf.volume_number}"
                )
                return

            # ── Broadcast each new scan ───────────────────────────────────
            # Dedup policy:
            #   - The "original" scan (sweep 0) may be broadcast multiple
            #     times — its non-refl products (velocity, CC, SRV) only
            #     become available once their sweep finishes filling in,
            #     so refreshes pick up additional coverage.
            #   - SAILS revisits are emitted ONCE when first detected
            #     (refl+vel sweep pair both present); their content doesn't
            #     change after that point.
            last_scan_dt: Optional[datetime] = None
            for refl_idx, vel_idx, scan_time, label in scans:
                scan_iso = scan_time.isoformat()
                if label != "original" and scan_iso in buf.broadcasted_scan_isos:
                    continue

                # Build a sub-radar with just this scan's sweeps
                sweep_list = [refl_idx]
                if vel_idx is not None:
                    sweep_list.append(vel_idx)
                try:
                    sub_radar = radar.extract_sweeps(sweep_list)
                except Exception as e:
                    logger.warning(
                        f"extract_sweeps({sweep_list}) failed for {site} "
                        f"V{buf.volume_number} {label}: {e}"
                    )
                    continue

                try:
                    sub_result = await loop.run_in_executor(
                        None,
                        self._nexrad._process_radar_object,
                        sub_radar, site, scan_time, True,  # skip_dealias
                    )
                except Exception as e:
                    logger.warning(
                        f"Chunks {label} render failed for {site} "
                        f"V{buf.volume_number}: {e}"
                    )
                    continue
                if sub_result is None:
                    continue

                # Each sub-scan is for display only (single low-tilt cone,
                # not enough vertical structure for storm tracking).
                await self._nexrad.finalize_phase1_async(
                    site, sub_result, source=f"{source}/{label}", skip_grid=True,
                )
                buf.broadcasted_scan_isos.add(scan_iso)
                last_scan_dt = scan_time
                logger.info(
                    f"Chunks {label} broadcast: {site} V{buf.volume_number} "
                    f"@ {scan_iso} (sweeps {sweep_list})"
                )

            # ── Storm tracking on the FULL volume (chunks-complete only) ──
            # SAILS revisits don't have higher tilts; only the complete
            # volume has the vertical structure needed for VIL, MESH, BWER,
            # mid-level rotation, and the storm tracking pipeline.
            if mark_complete:
                try:
                    full_result = await loop.run_in_executor(
                        None,
                        self._nexrad._process_radar_object,
                        radar, site, None, True,  # skip_dealias (already done)
                    )
                except Exception as e:
                    logger.warning(
                        f"Chunks-complete full render failed for {site} "
                        f"V{buf.volume_number}: {e}"
                    )
                    full_result = None
                if full_result is not None:
                    await self._nexrad.finalize_phase1_async(
                        site, full_result, source=f"{source}/full", skip_grid=False,
                    )
                    nsweeps = getattr(radar, "nsweeps", "?")
                    diag["last_complete_at"]     = datetime.now(timezone.utc).isoformat()
                    diag["last_complete_sweeps"] = nsweeps
                    logger.info(
                        f"Chunks-complete full render → storm tracking: {site} "
                        f"V{buf.volume_number} ({nsweeps} sweeps, "
                        f"{buf.chunk_count} chunks)"
                    )

            diag["last_render_duration_s"] = round(time.perf_counter() - t_proc, 2)

            # ── Bookkeeping ───────────────────────────────────────────────
            if last_scan_dt is not None:
                self._nexrad.mark_chunks_processed(site, last_scan_dt)
                last_scan_iso = last_scan_dt.isoformat()
            else:
                last_scan_iso = scans[-1][2].isoformat()

            if mark_complete:
                buf.complete_broadcast_ts = last_scan_iso
            else:
                # Mark the original-scan timestamp as the "partial broadcast"
                # so refresh-partial gating works as before.
                buf.partial_broadcast_ts = scans[0][2].isoformat()
                buf.partial_broadcast_chunk_count = buf.chunk_count
                buf.partial_broadcast_at = datetime.now(timezone.utc)

            diag.update({
                "volume_number":          buf.volume_number,
                "chunks_at_broadcast":    buf.chunk_count,
                "low_tilt_scans_in_volume": len(scans),
                "last_broadcast_source":  source,
                "last_broadcast_ts":      last_scan_iso,
            })
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ─── housekeeping ─────────────────────────────────────────────────────
    def _reap_stale_volumes(self) -> None:
        """Drop volume buffers that have been idle for >15 min."""
        now = datetime.now(timezone.utc)
        stale = [
            k for k, buf in self._volumes.items()
            if (now - buf.last_chunk_added_at).total_seconds() > 900
        ]
        for k in stale:
            buf = self._volumes.pop(k, None)
            if buf is not None:
                logger.debug(
                    f"Reaping stale chunks buffer: {buf.site} V{buf.volume_number} "
                    f"({buf.chunk_count} chunks, last activity "
                    f"{buf.last_chunk_added_at.isoformat()})"
                )
        # Trim the seen-keys cache so it doesn't grow unbounded.  Keep last
        # ~5000 keys per site — chunks bucket has 24h retention so this
        # comfortably exceeds the live window.
        for site, seen in list(self._seen_keys.items()):
            if len(seen) > 8000:
                self._seen_keys[site] = set(list(seen)[-5000:])


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[NexradChunksService] = None


def get_nexrad_chunks_service() -> Optional[NexradChunksService]:
    return _service


async def start_nexrad_chunks_service(nexrad_svc, settings) -> Optional[NexradChunksService]:
    global _service
    if not settings.nexrad_chunks_enabled:
        logger.info("NEXRAD chunks service disabled by setting")
        return None
    try:
        _service = NexradChunksService(nexrad_svc, settings)
        await _service.start()
        return _service
    except Exception as e:
        logger.error(f"NEXRAD chunks service failed to start: {e}", exc_info=True)
        _service = None
        return None


async def stop_nexrad_chunks_service() -> None:
    global _service
    if _service is not None:
        await _service.stop()
        _service = None

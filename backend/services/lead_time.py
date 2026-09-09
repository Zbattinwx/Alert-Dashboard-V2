"""Lead time: how far ahead of the warning did we see it?

WHY THIS IS THE METRIC
----------------------
AUC and average precision are computed over every row, and measured on this
archive 64.4% of the positive rows are scans of storms that were ALREADY under
a warning. Recognising an already-warned supercell is easy, so those rows
dominate the headline numbers while saying nothing about the only question the
system was built to answer: did we flag the storm BEFORE the forecaster did,
and by how long?

A model can improve its AP substantially while its lead time gets worse. That
has probably already happened and nobody could have seen it, because nothing
measured this.

WHAT IS MEASURED
----------------
Rows carry `minutes_before_warning` (see label_from_warnings): minutes the scan
precedes issuance, positive when we are ahead. Group the rows by warning, walk
each storm's scans in time order, and find the FIRST scan whose probability
crosses the operating threshold. Its lead time is that storm's score.

Reported:
  * detected      -- share of warned storms flagged at any point before issuance
  * median / p25 / p75 lead minutes over the detected ones
  * missed        -- warned storms never flagged before issuance
  * late          -- flagged, but only after the warning was out

`detected` and `median_lead_min` together are the product. One without the
other is misleading: firing on everything gives perfect detection and a
worthless false-alarm rate, so read this beside precision, never instead of it.
"""
from __future__ import annotations

from typing import Iterable, Optional

LEAD_FIELD = "minutes_before_warning"


def _percentile(sorted_vals: list[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def lead_time_report(rows: Iterable[dict], threshold: float) -> dict:
    """Lead-time skill over warned storms.

    `rows` are dicts with at least:
        warning_id  -- groups scans of one storm/warning together
        p           -- model probability for that scan
        lead_min    -- minutes the scan precedes issuance (>0 = ahead)

    Storms with no pre-warning scan at all are EXCLUDED rather than counted as
    misses: we never had the chance to see them early, so scoring them would
    measure the archive's coverage rather than the model.
    """
    by_warning: dict[str, list[dict]] = {}
    for r in rows:
        wid = r.get("warning_id")
        if wid is None:
            continue
        by_warning.setdefault(str(wid), []).append(r)

    leads: list[float] = []
    detected = missed = late = no_chance = 0

    for wid, scans in by_warning.items():
        pre = [s for s in scans if (s.get("lead_min") or 0) > 0]
        if not pre:
            no_chance += 1
            continue
        # Earliest crossing wins: a detector that fires 40 minutes out and holds
        # is worth more than one that only agrees at 2 minutes, and taking the
        # max probability instead would score them identically.
        pre.sort(key=lambda s: -(s.get("lead_min") or 0.0))
        first = next((s for s in pre if (s.get("p") or 0.0) >= threshold), None)
        if first is not None:
            detected += 1
            leads.append(float(first["lead_min"]))
        elif any((s.get("p") or 0.0) >= threshold for s in scans):
            late += 1
        else:
            missed += 1

    leads.sort()
    scored = detected + missed + late
    return {
        "threshold": threshold,
        "warnings_scored": scored,
        "warnings_without_pre_warning_scans": no_chance,
        "detected_before_issuance": detected,
        "detected_fraction": (detected / scored) if scored else None,
        "late_only": late,
        "missed": missed,
        "median_lead_min": _percentile(leads, 0.5),
        "p25_lead_min": _percentile(leads, 0.25),
        "p75_lead_min": _percentile(leads, 0.75),
        "max_lead_min": leads[-1] if leads else None,
    }


def format_lead_report(rep: dict) -> str:
    """One block for the training log / scorecard."""
    if not rep.get("warnings_scored"):
        return ("Lead time: no warned storms with pre-warning scans in this "
                "holdout -- nothing to measure.")
    frac = rep.get("detected_fraction")
    med = rep.get("median_lead_min")
    lines = [
        f"Lead time @ p>={rep['threshold']:.2f} over {rep['warnings_scored']} warned storms:",
        f"  flagged BEFORE the warning : {rep['detected_before_issuance']}"
        f" ({frac:.0%})" if frac is not None else "",
        f"  median lead                : {med:.0f} min" if med is not None else
        "  median lead                : n/a",
    ]
    if rep.get("p25_lead_min") is not None:
        lines.append(f"  p25 / p75                  : {rep['p25_lead_min']:.0f}"
                     f" / {rep['p75_lead_min']:.0f} min")
    lines.append(f"  flagged only after issuance: {rep['late_only']}")
    lines.append(f"  never flagged              : {rep['missed']}")
    return "\n".join(x for x in lines if x)

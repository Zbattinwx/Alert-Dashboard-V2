# Re-derive the training archive with the current feature set, overnight.
#
# WHY RE-DERIVE RATHER THAN START OVER
# ------------------------------------
# The existing archive is not wrong, it is missing COLUMNS. Deleting it would
# throw away 86 days of warning labels and storm reports -- the expensive part --
# to fix features that NaN already handles. Re-deriving keeps the labels and
# recomputes the features.
#
# backfill_training_data.py replays archived Level 2 through the SAME live
# pipeline (`_process_sync` + `build_training_record`) rather than
# reimplementing anything, so running it on today's code automatically produces:
#
#   * azimuthal shear from the fixed 2500 m physical kernel, instead of the
#     fixed-ray-count version whose values were very nearly a measurement of
#     range (median fell 50x from <10 km to >=150 km);
#   * the seven kinematic wind signatures, which the tracker was computing every
#     scan for its severity score and never writing into a training row;
#   * the near-storm environment AT THE RIGHT HOUR -- storm_environment resolves
#     each scan's own archived RAP cycle. Before that guard existed this replay
#     would have stapled tonight's atmosphere onto a storm from 2024.
#
# Lightning is the one thing it cannot recover: GLM is archived on AWS but the
# tracker has no historical flash feed, so flash_rate stays NaN for these rows.
#
# OUTPUT GOES TO A SEPARATE FILE. The current archive stays untouched until the
# re-derived one demonstrably trains better; that comparison is the whole point
# and it cannot be made if the old data has already been overwritten.
#
# Restartable: --resume skips (site, day) pairs already checkpointed, so an
# interrupted night picks up where it stopped.

$ErrorActionPreference = 'Continue'
$repo = 'F:\Apps\tbf\AlertDashboard'
Set-Location $repo

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log   = Join-Path $repo "logs\rederive-$stamp.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$out = Join-Path $repo 'data\training_data.rederived.jsonl'

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Log "=== re-derivation start ==="
Log "output: $out"

# 2019 is outside the RAP archive window, so those two days would come back with
# no environment. Everything from 2024-02-27 on is covered.
$sites = @('KILN', 'KIND', 'KCLE', 'KIWX', 'KPBZ')

Log "phase 1/3: replaying Level 2 (this is the long one)"
python scripts\backfill_training_data.py `
    --start 2024-02-27 --end 2026-09-07 `
    --sites $sites `
    --out $out --resume 2>&1 | Tee-Object -FilePath $log -Append
Log "phase 1 exit code: $LASTEXITCODE"

if (-not (Test-Path $out)) {
    Log "no output produced - stopping before the labelling phases"
    exit 1
}

Log "phase 2/3: labelling from warning polygons"
python scripts\label_from_warnings.py --data $out --all --overwrite 2>&1 |
    Tee-Object -FilePath $log -Append
Log "phase 2 exit code: $LASTEXITCODE"

Log "phase 3/3: labelling hail and wind from storm reports"
python scripts\label_from_lsr_hazards.py --from-archive --data $out 2>&1 |
    Tee-Object -FilePath $log -Append
Log "phase 3 exit code: $LASTEXITCODE"

$size = (Get-Item $out).Length / 1MB
Log ("done. {0:N0} MB at {1}" -f $size, $out)
Log "NOT promoted: train against it and compare before replacing training_merged.jsonl"
Log "=== re-derivation end ==="

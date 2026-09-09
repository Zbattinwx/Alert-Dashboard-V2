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
$logDir = Join-Path $repo 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "rederive-$stamp.log"

$out = Join-Path $repo 'data\training_data.rederived.jsonl'

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Output $line
    # Explicit UTF-8 without BOM. Add-Content and Tee-Object default to UTF-16
    # here, which makes the log unreadable with every ordinary text tool.
    [IO.File]::AppendAllText($log, $line + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding $false))
}

# PowerShell 5.1 wraps EVERY stderr line from a native exe in an ErrorRecord
# ("NativeCommandError") when piped through 2>&1, and sets $? to false even on a
# clean exit 0. Python's harmless "Using slower stringprep" notice was enough to
# fill the first run's log with fake failures. Start-Process with explicit
# redirection keeps the streams as plain text and yields a real exit code.
function Invoke-Phase {
    param([string]$Label, [string[]]$PyArgs)
    Log "=== $Label ==="
    $o = Join-Path $logDir "rederive-$stamp.$Label.out"
    $e = Join-Path $logDir "rederive-$stamp.$Label.err"
    $p = Start-Process -FilePath 'python' -ArgumentList $PyArgs `
        -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $o -RedirectStandardError $e
    Log "$Label exit code: $($p.ExitCode)"
    if (Test-Path $o) { Get-Content $o -Tail 12 | ForEach-Object { Log "  $_" } }
    return $p.ExitCode
}

Log '=== re-derivation start ==='
Log "output: $out"

# backfill_training_data.py keeps its checkpoint at a FIXED path shared by every
# run, and --resume means "skip any pair ever recorded as done" -- not "resume
# this run". On the first attempt that silently skipped 76 of 130 in-range
# pairs, all of them completed by the OLD code, i.e. exactly the work a
# re-derivation exists to redo. The output looked healthy the whole time.
#
# So: the checkpoint must agree with THIS run's output file. Pairs already
# present in $out stay done (a genuine resume); everything else is redone.
Invoke-Phase 'phase0-reseed' @(
    'scripts\reseed_backfill_state.py', $out) | Out-Null

# 2019 is outside the RAP archive window, so those two days would come back with
# no environment. Everything from 2024-02-27 on is covered.
$sites = @('KILN', 'KIND', 'KCLE', 'KIWX', 'KPBZ')

$rc = Invoke-Phase 'phase1-backfill' (
    @('scripts\backfill_training_data.py',
      '--start', '2024-02-27', '--end', '2026-09-07', '--sites') +
    $sites + @('--out', $out, '--resume'))

if (-not (Test-Path $out)) {
    Log 'no output produced - stopping before the labelling phases'
    exit 1
}

Invoke-Phase 'phase2-warnings' @(
    'scripts\label_from_warnings.py', '--data', $out, '--all', '--overwrite') | Out-Null

Invoke-Phase 'phase3-hazards' @(
    'scripts\label_from_lsr_hazards.py', '--from-archive', '--data', $out) | Out-Null

$size = (Get-Item $out).Length / 1MB
Log ('done. {0:N0} MB at {1}' -f $size, $out)
Log 'NOT promoted: train against it and compare before replacing training_merged.jsonl'
Log '=== re-derivation end ==='

# Assemble the merged training archive and train BOTH prediction targets.
#
# Run after the MRMS feature passes have finished (data\mrms_run2.done exists).
#
# The two targets come from ONE non-strict labelling pass -- see
# train_rotation_model.target_label. Labelling here runs WITHOUT --overwrite on
# purpose: that only touches rows which are currently unlabelled, which is
# exactly the SVR-ambiguous set left behind by the earlier --strict-tornado
# passes, so every existing tornado label stays byte-identical and the rotation
# numbers remain comparable to what was measured before.
#
#   .\scripts\build_training_set.ps1              label, merge, train both
#   .\scripts\build_training_set.ps1 -SkipLabel   merge + train only

param([switch]$SkipLabel)

$ErrorActionPreference = 'Stop'
Set-Location 'F:\Apps\tbf\AlertDashboard'

$py = 'C:\Python313\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

$archive = 'data\training_data.jsonl'
$part1   = 'data\training_data.backfill.part1.mrms.jsonl'
$run2    = 'data\training_data.backfill.mrms.jsonl'
$merged  = 'data\training_merged.jsonl'

function Run-Py {
    param([string[]]$Arguments, [string]$OutLog, [string]$ErrLog)
    # Start-Process, not `&` with `*>`: PowerShell 5.1 wraps a native command's
    # stderr in NativeCommandError records, and under $ErrorActionPreference =
    # 'Stop' a harmless startup notice on stderr aborts the run.
    $p = Start-Process -FilePath $py -ArgumentList $Arguments `
        -WorkingDirectory (Get-Location).Path `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
        -NoNewWindow -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Get-Content $ErrLog -Tail 20
        throw "python exited $($p.ExitCode): $($Arguments -join ' ')"
    }
}

# ── 1. non-strict labelling ────────────────────────────────────────────────
if (-not $SkipLabel) {
    foreach ($f in @($part1, $run2)) {
        if (-not (Test-Path $f)) { Write-Host "  skip (missing): $f" -ForegroundColor Yellow; continue }
        Write-Host "labelling $f (non-strict, adds SV.W positives)" -ForegroundColor Cyan
        Run-Py @('scripts\label_from_warnings.py', '--data', $f, '--all',
                 '--phenomena', 'TO', 'SV') 'data\lbl.out.log' 'data\lbl.err.log'
    }
}

# ── 2. merge ───────────────────────────────────────────────────────────────
# Append-only concatenation. The three inputs cover disjoint (site, ts, cell_id)
# keys -- verified before the first merge -- so this cannot duplicate rows.
Write-Host "merging -> $merged" -ForegroundColor Cyan
$inputs = @($archive, $part1, $run2) | Where-Object { Test-Path $_ }
Remove-Item $merged -ErrorAction SilentlyContinue
foreach ($f in $inputs) {
    Write-Host ("  + {0}  ({1:N0} MB)" -f $f, ((Get-Item $f).Length / 1MB))
    cmd /c "type `"$f`" >> `"$merged`""
}
Write-Host ("  = {0:N0} MB" -f ((Get-Item $merged).Length / 1MB))

# ── 3. train both targets ──────────────────────────────────────────────────
foreach ($t in @('rotation', 'severe')) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
    Write-Host " training target: $t" -ForegroundColor Cyan
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
    $out = "data\$($t)_model.candidate.joblib"
    Run-Py @('scripts\train_rotation_model.py', '--data', $merged,
             '--target', $t, '--out', $out) "data\train_$t.out.log" "data\train_$t.err.log"
    Get-Content "data\train_$t.out.log"
}

Write-Host ""
Write-Host "Candidates written. Nothing promoted - review the metrics first." -ForegroundColor Green

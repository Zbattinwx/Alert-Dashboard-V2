# MRMS rotation-feature pass over the run-2 radar replay output.
#
# Launched as a Windows Scheduled Task, NOT as a child of whatever shell asked
# for it. Two earlier attempts died when the calling agent session tore down and
# took its whole process tree with it; this job has no --resume and only writes
# its output at the very end, so an interruption costs the entire run.
#
# Writes a marker file on completion so the caller can tell "finished" from
# "still going" from "died" without watching the process.

$ErrorActionPreference = 'Stop'
Set-Location 'F:\Apps\tbf\AlertDashboard'

$log    = 'data\mrms_run2.log'
$marker = 'data\mrms_run2.done'
$in     = 'data\training_data.backfill.jsonl'
$out    = 'data\training_data.backfill.mrms.jsonl'

Remove-Item $marker -ErrorAction SilentlyContinue

$py = 'C:\Python313\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

"[$(Get-Date -Format o)] starting MRMS pass on $in" | Out-File $log -Encoding utf8

# Redirect at the PROCESS level, not with PowerShell's `*>>`. In 5.1 any
# redirection of a native command's stderr wraps each line in a NativeCommandError
# record, so with $ErrorActionPreference='Stop' the harmless "Using slower
# stringprep" notice that eccodes/idna prints on stderr aborts the whole run
# before python has done any work. That is exactly what killed the first attempt.
$outLog = 'data\mrms_run2.out.log'
$errLog = 'data\mrms_run2.err.log'
try {
    $p = Start-Process -FilePath $py `
        -ArgumentList @('scripts\backfill_mrms_features.py',
                        '--input',  $in,
                        '--output', $out,
                        '--workers', '10') `
        -WorkingDirectory 'F:\Apps\tbf\AlertDashboard' `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -NoNewWindow -Wait -PassThru
    $code = $p.ExitCode
} catch {
    "[$(Get-Date -Format o)] EXCEPTION $_" | Out-File $log -Append -Encoding utf8
    $code = 1
}

$rows = 0
if (Test-Path $out) { $rows = (Get-Item $out).Length }
"exit=$code bytes=$rows at $(Get-Date -Format o)" | Out-File $marker -Encoding utf8
"[$(Get-Date -Format o)] done, exit $code" | Out-File $log -Append -Encoding utf8

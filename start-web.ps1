[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$frontendRoot = Join-Path $projectRoot "frontend"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found. Run: python -m venv .venv"
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules"))) {
    throw "Frontend dependencies not found. Run: npm --prefix frontend install"
}

$occupied = foreach ($port in 4200, 5173) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($null -ne $owner) { $owner.ProcessName } else { "unknown" }
        "http://127.0.0.1:$port is already in use by PID $($listener.OwningProcess) ($name)"
    }
}
if ($occupied) {
    throw "Alexandria is already running or its ports are occupied:`n$($occupied -join "`n")`nStop the existing process before starting again."
}

$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$backend = $null
$frontend = $null

try {
    $backend = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "backend.alexandria.main" `
        -WorkingDirectory $projectRoot `
        -NoNewWindow `
        -PassThru

    $frontend = Start-Process `
        -FilePath $npm `
        -ArgumentList "run", "dev" `
        -WorkingDirectory $frontendRoot `
        -NoNewWindow `
        -PassThru

    Write-Host "Backend: http://127.0.0.1:4200"
    Write-Host "Frontend: http://127.0.0.1:5173"
    Write-Host "Press Ctrl+C to stop both services."

    while (-not $backend.HasExited -and -not $frontend.HasExited) {
        Start-Sleep -Seconds 1
        $backend.Refresh()
        $frontend.Refresh()
    }

    if ($backend.HasExited) {
        throw "Backend exited with code $($backend.ExitCode)."
    }

    throw "Frontend exited with code $($frontend.ExitCode)."
}
finally {
    foreach ($process in @($backend, $frontend)) {
        if ($null -eq $process) {
            continue
        }

        $process.Refresh()
        if (-not $process.HasExited) {
            $process.Kill($true)
            $process.WaitForExit()
        }
    }
}

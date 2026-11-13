param(
    [switch]$Install,
    [switch]$Classic,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SkaldArguments
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Find-SkaldPython {
    $candidates = @()
    if (Test-Path -LiteralPath $venvPython) { $candidates += $venvPython }
    # Python's Windows install manager and traditional per-user installer.
    $localRoots = @($env:LOCALAPPDATA, (Join-Path $env:USERPROFILE 'AppData\Local')) |
        Select-Object -Unique
    foreach ($base in @($localRoots | ForEach-Object {
        Join-Path $_ 'Python'
        Join-Path $_ 'Programs\Python'
    })) {
        if (Test-Path -LiteralPath $base) {
            $candidates += Get-ChildItem -LiteralPath $base -Directory |
                Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' }
        }
    }
    foreach ($name in @('python.exe', 'py.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { continue }
    }
    throw 'Python 3.11 or newer was not found. Install Python, then run install.bat again.'
}

try {
    Set-Location -LiteralPath $projectRoot
    $runtimePython = Find-SkaldPython
    if ($Install) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            & $runtimePython -m venv (Join-Path $projectRoot '.venv')
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
        }
        & $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Read the error above before launching.' }
        Write-Host 'Installed. Run run.bat to start Skald. The first run may download a speech model.'
    } else {
        $arguments = @((Join-Path $projectRoot 'skald.py'))
        if ($Classic) { $arguments += '--classic' }
        $arguments += $SkaldArguments
        & $runtimePython @arguments
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} catch {
    Write-Host ('Skald: ' + $_.Exception.Message) -ForegroundColor Yellow
    exit 1
}

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Project virtual environment not found at $python"
}

Push-Location $repoRoot
try {
    & $python -m uvicorn provenance.api.app:app --host 127.0.0.1 --port 8000 @args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
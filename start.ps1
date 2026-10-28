# ReviewPilot one-click launcher.
# Usage:
#   .\start.ps1            run a live agent review on the sample diff
#   .\start.ps1 -Test      run the agent unit test suite
#   .\start.ps1 -CheckOnly verify the environment only
param(
    [switch]$Test,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

function Info([string]$m) { Write-Host "[reviewpilot] $m" -ForegroundColor Cyan }
function Ok([string]$m)   { Write-Host "[reviewpilot] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[reviewpilot] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[reviewpilot] $m" -ForegroundColor Red; exit 1 }

Info "repo: $repo"

# 1) Python environment: use the project venv, create it via uv when missing.
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Warn ".venv not found; running 'uv sync' (requires uv >= 0.12.10)"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv is not installed. Install it first: pip install uv  (then rerun this script)"
    }
    & uv sync
    if ($LASTEXITCODE -ne 0) { Fail "uv sync failed" }
}
Ok "python venv ready"

$env:PYTHONPATH = "."

# 2) Secrets: create from template when missing.
$secrets = "pr_agent/settings/.secrets.toml"
if (-not (Test-Path $secrets)) {
    Copy-Item "pr_agent/settings/.secrets_template.toml" $secrets
    Warn ".secrets.toml missing; copied from template - fill your API key in $secrets"
}

# 3) Model/key sanity check (warnings only, never fatal).
#    The check code is passed base64-encoded: PowerShell 5.1 strips embedded
#    double quotes when passing a multiline -c argument to a native process.
$checkCode = @'
import tomllib
with open("pr_agent/settings/.secrets.toml", "rb") as f:
    data = tomllib.load(f)
model = (data.get("config") or {}).get("model", "")
provider = model.split("/", 1)[0] if "/" in model else "openai"
key = ((data.get(provider) or {}).get("key") or "")
print("MODEL=" + (model or "<unset>"))
print("KEY=" + ("set" if key else "EMPTY"))
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($checkCode))
$check = & $py -c "import base64;exec(base64.b64decode('$encoded').decode('utf-8'))"
$check | ForEach-Object { Info $_ }
if ($check -match "KEY=EMPTY") {
    Warn "provider key is empty - a live run will fail until you fill it in $secrets"
}

if ($CheckOnly) { Ok "environment check done"; exit 0 }

if ($Test) {
    Info "running unit tests (tests/unittest/agent_exec)"
    & $py -m pytest tests/unittest/agent_exec -q
    if ($LASTEXITCODE -ne 0) { Fail "tests failed" }
    Ok "all tests passed"
    exit 0
}

# 4) Live run: one real agent loop over the sample diff.
Info "starting a live agent review (sample diff, real model)"
& $py run_agent.py
if ($LASTEXITCODE -ne 0) { Fail "agent run failed - see output above" }
Ok "done"

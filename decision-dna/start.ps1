#Requires -Version 5.1
<#
.SYNOPSIS
    One-command launcher for the DecisionDNA Docker stack on Windows.

.DESCRIPTION
    Brings the full DecisionDNA stack up in Docker and verifies it end-to-end. The script:

      1. PREFLIGHT   - checks Docker is installed, Compose v2 is present, and the Docker daemon is running.
      2. CORP CA     - ensures 'corp-ca.crt' exists in every build context (needed to get past the
                       corporate Cloudflare Gateway TLS inspection). Exports it from the Windows trust
                       store; falls back to an empty file if you're not behind the proxy.
      3. ENV         - ensures a '.env' exists (creates a template if missing) and warns if API keys
                       are still placeholders.
      4. PORTS       - warns about host-port conflicts before building.
      5. BUILD & UP  - runs 'docker compose up --build -d'.
      6. VERIFY      - waits for the API gateway, then polls its aggregated /health endpoint and
                       reports the status of every downstream service. Also checks the Streamlit UI.

    The gateway's published host port is discovered automatically via 'docker compose port', so the
    script works whether the gateway is on the default 8000 or a custom GATEWAY_HOST_PORT from .env.

    Run it from anywhere - it operates on its own folder (the 'decision-dna' directory).

.PARAMETER SkipBuild
    Start containers without rebuilding images (faster if images already exist).

.PARAMETER Down
    Stop and remove the stack's containers, then exit. Data volumes are preserved.

.PARAMETER TimeoutSeconds
    How long to wait for services to become healthy after startup. Default: 180.

.EXAMPLE
    .\start.ps1
    Full run: checks, build, start, and verify.

.EXAMPLE
    .\start.ps1 -SkipBuild
    Start already-built images and verify (no rebuild).

.EXAMPLE
    .\start.ps1 -Down
    Tear the stack down.

.NOTES
    If PowerShell blocks the script ("running scripts is disabled"), unblock it for this session:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    Companion doc: WINDOWS_DOCKER_SETUP.md
#>

[CmdletBinding()]
param(
    [switch] $SkipBuild,
    [switch] $Down,
    [int]    $TimeoutSeconds = 180
)

# Stop on unhandled errors; we handle expected failures explicitly.
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ helpers
function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red }
function Fail($msg)       { Write-Err2 $msg; Write-Host "`nAborted." -ForegroundColor Red; exit 1 }

# The seven Docker build contexts (relative to this script's folder).
$BuildContexts = @(
    "api-gateway", "frontend",
    "services\ingestion-service", "services\embedding-service",
    "services\graph-service", "services\query-service", "services\timeline-service"
)

# Always operate on the folder this script lives in (the decision-dna dir).
Set-Location -Path $PSScriptRoot

Write-Host "==================================================================" -ForegroundColor Magenta
Write-Host "  DecisionDNA - Docker launcher (Windows)" -ForegroundColor Magenta
Write-Host "==================================================================" -ForegroundColor Magenta

# ------------------------------------------------------------------ teardown shortcut
if ($Down) {
    Write-Step "Stopping the stack (docker compose down)"
    docker compose down
    if ($LASTEXITCODE -eq 0) { Write-Ok "Stack stopped. Data volumes preserved (use 'docker compose down -v' to wipe)." }
    else { Fail "docker compose down failed." }
    exit 0
}

# ------------------------------------------------------------------ 1. PREFLIGHT
Write-Step "1/6  Preflight checks"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed or not on PATH. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
}
Write-Ok "docker found: $((docker --version))"

# Compose v2 is a docker subcommand ('docker compose', not the legacy 'docker-compose').
docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Compose v2 not available. Update Docker Desktop (it bundles 'docker compose')."
}
Write-Ok "compose found: $((docker compose version | Select-Object -First 1))"

# Is the daemon actually running? 'docker info' errors if Docker Desktop is stopped.
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "The Docker daemon isn't responding. Start Docker Desktop and wait until it says 'running', then re-run."
}
Write-Ok "Docker daemon is running"

if (-not (Test-Path ".\docker-compose.yml")) {
    Fail "docker-compose.yml not found in $PSScriptRoot. Run this script from the 'decision-dna' folder."
}
Write-Ok "docker-compose.yml found"

# ------------------------------------------------------------------ 2. CORP CA
Write-Step "2/6  Corporate TLS CA (corp-ca.crt in each build context)"

$missing = $BuildContexts | Where-Object { -not (Test-Path (Join-Path $_ "corp-ca.crt")) }

if ($missing.Count -eq 0) {
    Write-Ok "corp-ca.crt already present in all $($BuildContexts.Count) build contexts"
} else {
    Write-Warn2 "corp-ca.crt missing in $($missing.Count) context(s); generating..."

    # Look for the corporate root CA already trusted by Windows (Cloudflare Gateway / Zero Trust).
    $ca = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
          Where-Object { $_.Subject -like "*Cloudflare*" -and $_.Subject -like "*Gateway*" } |
          Select-Object -First 1

    if ($ca) {
        $b64 = [Convert]::ToBase64String($ca.RawData, 'InsertLineBreaks')
        $pem = "-----BEGIN CERTIFICATE-----`r`n$b64`r`n-----END CERTIFICATE-----`r`n"
        foreach ($c in $BuildContexts) { Set-Content -Path (Join-Path $c "corp-ca.crt") -Value $pem -Encoding ascii }
        Write-Ok "Exported corporate CA ($($ca.Subject.Split(',')[-1].Trim())) to all build contexts"
    } else {
        # Not behind TLS inspection (or a different proxy). Empty files satisfy the Dockerfile COPY and are harmless.
        foreach ($c in $BuildContexts) { New-Item -ItemType File -Path (Join-Path $c "corp-ca.crt") -Force | Out-Null }
        Write-Warn2 "No Cloudflare Gateway CA found in your trust store. Created empty corp-ca.crt files."
        Write-Warn2 "If the build later fails with CERTIFICATE_VERIFY_FAILED, your proxy uses a different CA -"
        Write-Warn2 "see WINDOWS_DOCKER_SETUP.md section 3 to supply it manually."
    }
}

# ------------------------------------------------------------------ 3. ENV
Write-Step "3/6  Environment file (.env)"

if (-not (Test-Path ".\.env")) {
    Write-Warn2 ".env not found; writing a template with placeholder keys."
    @'
# --- SECRETS (fill these in) ---
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
PINECONE_API_KEY=your-pinecone-key

# --- Pinecone (an index with this exact name + dimension must already exist) ---
PINECONE_ENVIRONMENT=us-east-1
PINECONE_INDEX_NAME=ai-asset
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_DIMENSIONS=1024

# --- Neo4j (local container; matches docker-compose NEO4J_AUTH) ---
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123

# --- Redis (local container) ---
REDIS_URL=redis://redis:6379

# --- Inter-service URLs (docker network names - do not change) ---
INGESTION_SERVICE_URL=http://ingestion-service:8001
EMBEDDING_SERVICE_URL=http://embedding-service:8002
GRAPH_SERVICE_URL=http://graph-service:8003
QUERY_SERVICE_URL=http://query-service:8004
TIMELINE_SERVICE_URL=http://timeline-service:8005
GATEWAY_URL=http://api-gateway:8000

# --- App ---
ENVIRONMENT=development
LOG_LEVEL=INFO
'@ | Set-Content -Path ".\.env" -Encoding ascii
    Write-Warn2 "Created .env. EDIT IT NOW and add your ANTHROPIC / OPENAI / PINECONE keys, then re-run this script."
    Write-Host  "    (The stack will start, but embedding/query/timeline will fail until real keys are set.)" -ForegroundColor Yellow
} else {
    Write-Ok ".env found"
    $envText = Get-Content ".\.env" -Raw
    if ($envText -match 'your-(anthropic|openai|pinecone)-key' -or $envText -match 'REPLACE_WITH') {
        Write-Warn2 "It looks like .env still contains PLACEHOLDER keys."
        Write-Warn2 "embedding/query/timeline services will report 'unreachable' (Pinecone 401) until you set real keys."
    } else {
        Write-Ok "API keys appear to be filled in"
    }
}

# ------------------------------------------------------------------ 4. PORTS (informational)
Write-Step "4/6  Host port check"

# host-port -> what it's for (the default LEFT side of the mappings in docker-compose.yml).
# Gateway/Redis host ports are overridable via GATEWAY_HOST_PORT / REDIS_HOST_PORT in .env.
$portsToCheck = @{ 3000 = "Streamlit UI"; 8000 = "API gateway"; 7474 = "Neo4j browser"; 6379 = "Redis" }
foreach ($p in ($portsToCheck.Keys | Sort-Object)) {
    $inUse = $false
    try { $inUse = (Test-NetConnection -ComputerName localhost -Port $p -WarningAction SilentlyContinue).TcpTestSucceeded } catch {}
    if ($inUse) {
        Write-Warn2 ("Port {0} ({1}) is already in use." -f $p, $portsToCheck[$p])
        if ($p -eq 8000) { Write-Warn2 "  -> set GATEWAY_HOST_PORT=<free port> in .env, then re-run." }
        elseif ($p -eq 6379) { Write-Warn2 "  -> set REDIS_HOST_PORT=<free port> in .env, then re-run." }
        else { Write-Warn2 "  -> free it, or change the host port in docker-compose.yml." }
    } else {
        Write-Ok ("Port {0} ({1}) is free" -f $p, $portsToCheck[$p])
    }
}

# ------------------------------------------------------------------ 5. BUILD & UP
if ($SkipBuild) {
    Write-Step "5/6  Starting containers (no rebuild)"
    docker compose up -d
} else {
    Write-Step "5/6  Building images and starting containers (first build can take 5-10 min)"
    docker compose up --build -d
}
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose failed to start the stack. Scroll up for the error (common causes: port conflict, or CERTIFICATE_VERIFY_FAILED -> see WINDOWS_DOCKER_SETUP.md)."
}
Write-Ok "docker compose reported the stack started"

# ------------------------------------------------------------------ 6. VERIFY
Write-Step "6/6  Verifying the system is up"

# Discover the gateway's published host port (robust to 8080 vs 8000 vs anything).
$gatewayPort = $null
try {
    $portLine = (docker compose port api-gateway 8000 2>$null | Select-Object -First 1)
    if ($portLine) { $gatewayPort = $portLine.Split(':')[-1].Trim() }
} catch {}
if (-not $gatewayPort) { $gatewayPort = "8000" }  # sensible fallback (matches compose default)
$healthUrl = "http://localhost:$gatewayPort/health"
Write-Host "    Gateway published on host port $gatewayPort -> polling $healthUrl" -ForegroundColor Gray

# Poll /health until the gateway answers or we time out.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$health = $null
$attempt = 0
while ((Get-Date) -lt $deadline) {
    $attempt++
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5 -ErrorAction Stop
        break
    } catch {
        Write-Host ("    ...waiting for gateway (attempt {0})" -f $attempt) -ForegroundColor DarkGray
        Start-Sleep -Seconds 4
    }
}

if (-not $health) {
    Write-Err2 "Gateway did not respond on $healthUrl within $TimeoutSeconds seconds."
    Write-Host  "    Check its logs:  docker compose logs api-gateway" -ForegroundColor Yellow
    Write-Host  "    All containers:  docker compose ps" -ForegroundColor Yellow
    exit 2
}

Write-Ok "API gateway is responding"

# Per-service report from the aggregated health payload.
Write-Host "`n    Service health:" -ForegroundColor White
$allHealthy = $true
$svc = $health.services
if ($svc) {
    foreach ($name in ($svc.PSObject.Properties.Name | Sort-Object)) {
        $state = $svc.$name
        if ($state -eq "healthy") {
            Write-Host ("      - {0,-12} {1}" -f $name, $state) -ForegroundColor Green
        } else {
            Write-Host ("      - {0,-12} {1}" -f $name, $state) -ForegroundColor Red
            $allHealthy = $false
        }
    }
}

# Streamlit UI reachability (published on host 3000).
$uiOk = $false
try {
    $ui = Invoke-WebRequest -Uri "http://localhost:3000" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    $uiOk = ($ui.StatusCode -eq 200)
} catch {}
if ($uiOk) { Write-Host "      - frontend     reachable (HTTP 200)" -ForegroundColor Green }
else       { Write-Host "      - frontend     not reachable yet" -ForegroundColor Yellow }

# ------------------------------------------------------------------ SUMMARY
Write-Host "`n==================================================================" -ForegroundColor Magenta
if ($allHealthy -and $uiOk) {
    Write-Host "  SUCCESS - DecisionDNA is up and all services are healthy." -ForegroundColor Green
    Write-Host "==================================================================" -ForegroundColor Magenta
    Write-Host "  Open the UI:        http://localhost:3000"
    Write-Host "  API gateway:        http://localhost:$gatewayPort"
    Write-Host "  API docs (Scalar):  http://localhost:$gatewayPort/scalar"
    Write-Host "  Neo4j browser:      http://localhost:7474   (neo4j / password123)"
    Write-Host "`n  Next: load data ->"
    Write-Host "    Invoke-RestMethod -Method Post -Uri http://localhost:$gatewayPort/api/v1/ingest ``"
    Write-Host "      -ContentType 'application/json' ``"
    Write-Host "      -Body '{`"data_dir`":`"/app/data/synthetic`",`"trigger_embedding`":true,`"trigger_graph`":true}'"
    exit 0
} else {
    Write-Host "  PARTIAL - the stack is running but not everything is healthy." -ForegroundColor Yellow
    Write-Host "==================================================================" -ForegroundColor Magenta
    Write-Host "  Most common cause: placeholder API keys in .env (Pinecone 401) ->" -ForegroundColor Yellow
    Write-Host "    1. Put real keys in .env"
    Write-Host "    2. Ensure a Pinecone index named 'ai-asset' (1024 dims, cosine) exists"
    Write-Host "    3. docker compose up -d --force-recreate embedding-service query-service timeline-service"
    Write-Host "`n  Inspect a failing service:  docker compose logs <service-name>" -ForegroundColor Yellow
    Write-Host "  The UI at http://localhost:3000 works regardless for browsing."
    exit 3
}

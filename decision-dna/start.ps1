#Requires -Version 5.1
<#
.SYNOPSIS
    One-command launcher + health verifier for the DecisionDNA Docker stack on Windows.

.DESCRIPTION
    Brings the full DecisionDNA stack up (or down) in Docker and verifies it end-to-end.
    Designed to be run repeatedly and idempotently - every 'up' recreates containers from
    the latest images and .env, so stale containers never linger. The script:

      1. PREFLIGHT   - Docker installed, Compose v2 present, daemon running, right folder.
      2. CORP CA     - ensures 'corp-ca.crt' exists in every build context (needed to get past
                       the corporate Cloudflare Gateway TLS inspection).
      3. ENV         - ensures a '.env' exists (creates a template if missing); warns on placeholder keys.
      4. PORTS       - warns about host-port conflicts before building.
      5. BUILD & UP  - 'docker compose up -d --build --force-recreate' so code AND .env changes
                       always take effect (this is the fix for "I changed .env but nothing happened").
      6. VERIFY      - waits for the gateway, then checks EACH service two ways:
                         (a) reachability via the gateway's aggregated /health, and
                         (b) readiness  via each service's own /health 'ready' flag
                             (true only once it has actually connected to Pinecone/OpenAI).
                       Any service that is unreachable or up-but-not-ready has its container logs
                       captured to a timestamped file under .\logs\ and echoed to the console.

    Everything printed is also written to .\logs\run-<timestamp>.log via a transcript.

    The gateway's published host port is discovered automatically via 'docker compose port'.
    Run from anywhere - it operates on its own folder (the 'decision-dna' directory).

.PARAMETER Down
    Stop and remove the stack's containers, then exit. Data volumes are preserved
    (add -Volumes to also wipe Neo4j/Redis data).

.PARAMETER Volumes
    Only with -Down: also remove data volumes (Neo4j graph + Redis cache are wiped).

.PARAMETER SkipBuild
    Start containers without rebuilding images (still force-recreates to pick up .env).

.PARAMETER TimeoutSeconds
    How long to wait for services to become healthy after startup. Default: 240.

.EXAMPLE
    .\start.ps1
    Full run: checks, build, (re)create, and verify.

.EXAMPLE
    .\start.ps1 -Down
    Tear the stack down (keep data).

.EXAMPLE
    .\start.ps1 -Down -Volumes
    Tear the stack down and wipe Neo4j/Redis data.

.NOTES
    If PowerShell blocks the script ("running scripts is disabled"), unblock it for this session:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    Companion doc: WINDOWS_DOCKER_SETUP.md
#>

[CmdletBinding()]
param(
    [switch] $Down,
    [switch] $Volumes,
    [switch] $SkipBuild,
    [int]    $TimeoutSeconds = 240
)

# Stop on unhandled errors; expected failures (native commands, probes) are handled explicitly.
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ helpers
function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red }

# Clean exit that always flushes the transcript log first.
function Finish($code) {
    try { Stop-Transcript | Out-Null } catch {}
    if ($script:LogFile) { Write-Host "`n  Full log written to: $script:LogFile" -ForegroundColor DarkGray }
    exit $code
}
function Fail($msg) { Write-Err2 $msg; Write-Host "`nAborted." -ForegroundColor Red; Finish 1 }

# The seven Docker build contexts (relative to this script's folder).
$BuildContexts = @(
    "api-gateway", "frontend",
    "services\ingestion-service", "services\embedding-service",
    "services\graph-service", "services\query-service", "services\timeline-service"
)

# Downstream services: gateway-health short name, compose service name, published host port,
# and whether it depends on Pinecone/OpenAI (so we know to check the 'ready' flag).
$ServiceMap = @(
    [pscustomobject]@{ Short="ingestion"; Compose="ingestion-service"; Port=8001; NeedsPinecone=$false },
    [pscustomobject]@{ Short="embedding"; Compose="embedding-service"; Port=8002; NeedsPinecone=$true  },
    [pscustomobject]@{ Short="graph";     Compose="graph-service";     Port=8003; NeedsPinecone=$false },
    [pscustomobject]@{ Short="query";     Compose="query-service";     Port=8004; NeedsPinecone=$true  },
    [pscustomobject]@{ Short="timeline";  Compose="timeline-service";  Port=8005; NeedsPinecone=$true  }
)

# Always operate on the folder this script lives in (the decision-dna dir).
Set-Location -Path $PSScriptRoot

# Start logging everything to a timestamped file under .\logs\
$script:LogFile = $null
try {
    $logDir = Join-Path $PSScriptRoot "logs"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $script:LogFile = Join-Path $logDir ("run-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    Start-Transcript -Path $script:LogFile -Append | Out-Null
} catch {
    Write-Host "    [WARN] Could not start log transcript: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "==================================================================" -ForegroundColor Magenta
Write-Host "  DecisionDNA - Docker launcher (Windows)" -ForegroundColor Magenta
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Magenta
Write-Host "==================================================================" -ForegroundColor Magenta

# ------------------------------------------------------------------ teardown shortcut
if ($Down) {
    if ($Volumes) {
        Write-Step "Stopping the stack and WIPING data volumes (docker compose down -v)"
        docker compose down -v
    } else {
        Write-Step "Stopping the stack (docker compose down)"
        docker compose down
    }
    if ($LASTEXITCODE -eq 0) {
        if ($Volumes) { Write-Ok "Stack stopped and data volumes removed." }
        else          { Write-Ok "Stack stopped. Data preserved (use -Down -Volumes to wipe)." }
        Finish 0
    } else {
        Fail "docker compose down failed (see output above)."
    }
}

# ------------------------------------------------------------------ 1. PREFLIGHT
Write-Step "1/6  Preflight checks"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed or not on PATH. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
}
Write-Ok "docker found: $((docker --version))"

docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 not available. Update Docker Desktop (it bundles 'docker compose')." }
Write-Ok "compose found: $((docker compose version | Select-Object -First 1))"

docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "The Docker daemon isn't responding. Start Docker Desktop, wait until it says 'running', then re-run." }
Write-Ok "Docker daemon is running"

if (-not (Test-Path ".\docker-compose.yml")) { Fail "docker-compose.yml not found in $PSScriptRoot. Run this from the 'decision-dna' folder." }
Write-Ok "docker-compose.yml found"

# ------------------------------------------------------------------ 2. CORP CA
Write-Step "2/6  Corporate TLS CA (corp-ca.crt in each build context)"

$missing = $BuildContexts | Where-Object { -not (Test-Path (Join-Path $_ "corp-ca.crt")) }
if (-not $missing -or $missing.Count -eq 0) {
    Write-Ok "corp-ca.crt already present in all $($BuildContexts.Count) build contexts"
} else {
    Write-Warn2 "corp-ca.crt missing in $($missing.Count) context(s); generating..."
    $ca = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
          Where-Object { $_.Subject -like "*Cloudflare*" -and $_.Subject -like "*Gateway*" } |
          Select-Object -First 1
    if ($ca) {
        $b64 = [Convert]::ToBase64String($ca.RawData, 'InsertLineBreaks')
        $pem = "-----BEGIN CERTIFICATE-----`r`n$b64`r`n-----END CERTIFICATE-----`r`n"
        foreach ($c in $BuildContexts) { Set-Content -Path (Join-Path $c "corp-ca.crt") -Value $pem -Encoding ascii }
        Write-Ok "Exported corporate CA to all build contexts"
    } else {
        foreach ($c in $BuildContexts) { New-Item -ItemType File -Path (Join-Path $c "corp-ca.crt") -Force | Out-Null }
        Write-Warn2 "No Cloudflare Gateway CA found; created empty corp-ca.crt files (fine if you're not behind TLS inspection)."
    }
}

# ------------------------------------------------------------------ 3. ENV
Write-Step "3/6  Environment file (.env)"

$placeholderKeys = $false
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
    $placeholderKeys = $true
    Write-Warn2 "Created .env - EDIT IT and add real ANTHROPIC / OPENAI / PINECONE keys, then re-run."
} else {
    Write-Ok ".env found"
    $envText = Get-Content ".\.env" -Raw
    if ($envText -match 'your-(anthropic|openai|pinecone)-key' -or $envText -match 'REPLACE_WITH') {
        $placeholderKeys = $true
        Write-Warn2 "It looks like .env still contains PLACEHOLDER keys - Pinecone-backed services will start but not be READY."
    } else {
        Write-Ok "API keys appear to be filled in"
    }
}

# ------------------------------------------------------------------ 4. PORTS
Write-Step "4/6  Host port check"
$portsToCheck = @{ 8501 = "Streamlit UI"; 8000 = "API gateway"; 7474 = "Neo4j browser"; 6379 = "Redis" }
foreach ($p in ($portsToCheck.Keys | Sort-Object)) {
    $inUse = $false
    try { $inUse = (Test-NetConnection -ComputerName localhost -Port $p -WarningAction SilentlyContinue).TcpTestSucceeded } catch {}
    if ($inUse) {
        Write-Warn2 ("Port {0} ({1}) is already in use." -f $p, $portsToCheck[$p])
        if     ($p -eq 8000) { Write-Warn2 "  -> set GATEWAY_HOST_PORT=<free port> in .env, then re-run." }
        elseif ($p -eq 6379) { Write-Warn2 "  -> set REDIS_HOST_PORT=<free port> in .env, then re-run." }
        else                 { Write-Warn2 "  -> free it, or change the host port in docker-compose.yml." }
    } else {
        Write-Ok ("Port {0} ({1}) is free" -f $p, $portsToCheck[$p])
    }
}

# ------------------------------------------------------------------ 5. BUILD & UP
# --force-recreate guarantees fresh containers so rebuilt code AND edited .env always take
# effect - this is what makes repeated runs behave identically instead of reusing stale state.
if ($SkipBuild) {
    Write-Step "5/6  (Re)creating containers without rebuild"
    docker compose up -d --force-recreate
} else {
    Write-Step "5/6  Building images and (re)creating containers (first build can take 5-10 min)"
    docker compose up -d --build --force-recreate
}
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose failed to start the stack (see output above). Common causes: port conflict, or CERTIFICATE_VERIFY_FAILED -> WINDOWS_DOCKER_SETUP.md."
}
Write-Ok "docker compose reported the stack (re)created"

# ------------------------------------------------------------------ 6. VERIFY
Write-Step "6/6  Verifying the system is up"

# Discover the gateway's published host port (robust to a GATEWAY_HOST_PORT override).
$gatewayPort = $null
try {
    $portLine = (docker compose port api-gateway 8000 2>$null | Select-Object -First 1)
    if ($portLine) { $gatewayPort = $portLine.Split(':')[-1].Trim() }
} catch {}
if (-not $gatewayPort) { $gatewayPort = "8000" }
$healthUrl = "http://localhost:$gatewayPort/health"
Write-Host "    Gateway published on host port $gatewayPort -> polling $healthUrl" -ForegroundColor Gray

# Poll the gateway until it answers or we time out (covers Neo4j's ~60s warm-up).
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$health = $null
$attempt = 0
while ((Get-Date) -lt $deadline) {
    $attempt++
    try { $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5 -ErrorAction Stop; break }
    catch { Write-Host ("    ...waiting for gateway (attempt {0})" -f $attempt) -ForegroundColor DarkGray; Start-Sleep -Seconds 4 }
}
if (-not $health) {
    Write-Err2 "Gateway did not respond on $healthUrl within $TimeoutSeconds seconds."
    Write-Host "`n    --- docker compose ps ---" -ForegroundColor Yellow
    docker compose ps
    Write-Host "`n    --- api-gateway logs (last 40) ---" -ForegroundColor Yellow
    docker compose logs --tail 40 api-gateway
    Finish 2
}
Write-Ok "API gateway is responding"

# Per-service check: reachability (from gateway) + readiness (from each service's own /health).
Write-Host "`n    Service health (reachable = process up; ready = connected to Pinecone/OpenAI):" -ForegroundColor White
$gwServices = $health.services
$problems = @()   # services that are unreachable OR up-but-not-ready

foreach ($m in $ServiceMap) {
    $reachable = ($gwServices -and ($gwServices.$($m.Short) -eq "healthy"))

    # Read the service's own /health to learn readiness (only meaningful once reachable).
    $ready = $null
    try {
        $h = Invoke-RestMethod -Uri "http://localhost:$($m.Port)/health" -TimeoutSec 5 -ErrorAction Stop
        if ($h.PSObject.Properties.Name -contains 'ready') { $ready = [bool]$h.ready } else { $ready = $true }
    } catch { $ready = $null }

    if (-not $reachable) {
        Write-Host ("      - {0,-11} DOWN (unreachable)" -f $m.Short) -ForegroundColor Red
        $problems += $m
    } elseif ($m.NeedsPinecone -and ($ready -ne $true)) {
        Write-Host ("      - {0,-11} UP but NOT READY (can't reach Pinecone/OpenAI)" -f $m.Short) -ForegroundColor Yellow
        $problems += $m
    } else {
        Write-Host ("      - {0,-11} healthy + ready" -f $m.Short) -ForegroundColor Green
    }
}

# Streamlit UI reachability (published on host 8501).
$uiOk = $false
try { $uiOk = ((Invoke-WebRequest -Uri "http://localhost:8501" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop).StatusCode -eq 200) } catch {}
if ($uiOk) { Write-Host "      - frontend    reachable (HTTP 200)" -ForegroundColor Green }
else       { Write-Host "      - frontend    NOT reachable" -ForegroundColor Red }

# Capture logs for anything unhealthy so the developer has the error in hand (and in the log file).
if ($problems.Count -gt 0) {
    Write-Step "Collecting logs for services that aren't fully healthy"
    foreach ($m in $problems) {
        Write-Host "`n    ----- docker compose logs --tail 40 $($m.Compose) -----" -ForegroundColor Yellow
        docker compose logs --tail 40 $($m.Compose) 2>&1 | ForEach-Object { Write-Host "      $_" }
    }
}

# ------------------------------------------------------------------ SUMMARY
Write-Host "`n==================================================================" -ForegroundColor Magenta
if ($problems.Count -eq 0 -and $uiOk) {
    Write-Host "  SUCCESS - DecisionDNA is up and every service is healthy + ready." -ForegroundColor Green
    Write-Host "==================================================================" -ForegroundColor Magenta
    Write-Host "  Open the UI:        http://localhost:8501"
    Write-Host "  API gateway:        http://localhost:$gatewayPort"
    Write-Host "  API docs (Scalar):  http://localhost:$gatewayPort/scalar"
    Write-Host "  Neo4j browser:      http://localhost:7474   (neo4j / password123)"
    Write-Host "`n  Next: load data ->"
    Write-Host "    Invoke-RestMethod -Method Post -Uri http://localhost:$gatewayPort/api/v1/ingest ``"
    Write-Host "      -ContentType 'application/json' ``"
    Write-Host "      -Body '{`"data_dir`":`"/app/data/synthetic`",`"trigger_embedding`":true,`"trigger_graph`":true}'"
    Finish 0
} else {
    Write-Host "  PARTIAL - the stack is running but not everything is ready." -ForegroundColor Yellow
    Write-Host "==================================================================" -ForegroundColor Magenta
    if ($placeholderKeys) {
        Write-Host "  .env still has PLACEHOLDER keys. That's the most likely cause." -ForegroundColor Yellow
    }
    Write-Host "  If a Pinecone-backed service is UP but NOT READY:" -ForegroundColor Yellow
    Write-Host "    1. Put real ANTHROPIC / OPENAI / PINECONE keys in .env"
    Write-Host "    2. Ensure a Pinecone index named 'ai-asset' (1024 dims, cosine) exists"
    Write-Host "    3. Re-run this script (it force-recreates, so the new keys take effect)"
    Write-Host "`n  The per-service logs above (and $script:LogFile) show the exact error."
    Write-Host "  The UI at http://localhost:8501 works regardless for browsing."
    Finish 3
}

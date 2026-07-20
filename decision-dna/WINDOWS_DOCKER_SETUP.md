# Running DecisionDNA on Windows with Docker

A step-by-step runbook for setting up and running the DecisionDNA stack on a **Windows 10/11 desktop** using Docker Desktop. All commands are **PowerShell** unless noted.

> Estimated time: ~20 min (plus first build ~5–10 min).

---

## ⚡ Fast path: use the launcher script

After installing the prerequisites (Section 1) and getting the code (Section 2), you can let the bundled script do Sections 3–6 for you. From the `decision-dna\` folder in **PowerShell**:

```powershell
# one-time, if PowerShell blocks scripts:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\start.ps1
```

`start.ps1` runs preflight checks, generates `corp-ca.crt` and a `.env` template if missing, builds, starts the stack, and then polls the gateway's health endpoint and prints a per-service PASS/FAIL summary with the URLs. If it reports placeholder keys, fill in `.env` (Section 4) and re-run. Other handy modes: `.\start.ps1 -SkipBuild` and `.\start.ps1 -Down`.

The manual sections below explain exactly what the script does, and are the fallback if anything needs adjusting.

---

## 0. What you're setting up

Nine containers orchestrated by `docker-compose.yml`: Redis, Neo4j, six FastAPI services, and a Streamlit UI. When it's running you'll open the UI at **http://localhost:8501**.

Two files are intentionally **not** in git and must be created on your machine:
- **`.env`** — your API keys and config (Section 4).
- **`corp-ca.crt`** — a copy of the corporate TLS root CA, one per build context (Section 3). Required only if you're behind the corporate network's TLS inspection (most of us are); harmless otherwise.

---

## 1. Prerequisites

1. **Docker Desktop for Windows** — https://www.docker.com/products/docker-desktop/
   - During install, keep **"Use WSL 2 based engine"** checked (Settings → General).
   - In **Settings → Resources**, give Docker at least **8 GB RAM** (Neo4j + 6 Python services + Redis are memory-hungry). 4 GB will thrash.
   - Start Docker Desktop and wait for the whale icon to say "Docker Desktop is running."
2. **Git for Windows** — https://git-scm.com/download/win (gives you Git Bash + `openssl`, used as a fallback in Section 3).
3. Verify in PowerShell:
   ```powershell
   docker --version
   docker compose version
   docker info      # should NOT error; if it does, Docker Desktop isn't running
   ```

> **Tip (performance):** clone the repo *inside* your WSL2 filesystem (e.g. `\\wsl$\Ubuntu\home\you\`) or on a local drive Docker shares. Building on a network drive is slow.

---

## 2. Get the code

```powershell
git clone <your-repo-url> AI-asset
cd AI-asset\decision-dna
```

Make sure the repo you cloned **includes the following committed fixes** (ask the person who shared it if unsure — see the note at the very bottom of this doc):
- CA-injection lines in all 7 `Dockerfile`s,
- the resilient `scalar_fastapi` import in `api-gateway/app/main.py`,
- the host-port settings in `docker-compose.yml`.

If any are missing, the build or startup will fail exactly as described in **Troubleshooting**.

---

## 3. Create `corp-ca.crt` in every build context (corporate TLS fix)

Our corporate network (Cloudflare Gateway / Zero Trust) intercepts TLS. Fresh Docker containers don't trust that corporate root CA, so `pip install` **and** runtime calls to Pinecone/OpenAI fail with `CERTIFICATE_VERIFY_FAILED`. The Dockerfiles are already wired to trust a `corp-ca.crt` file — you just need to produce it.

### Option A — PowerShell (exports the CA from your Windows trust store)

Run from `decision-dna\`:

```powershell
# Find the corporate root CA already trusted by Windows
$ca = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
      Where-Object { $_.Subject -like "*Cloudflare*" -and $_.Subject -like "*Gateway*" } |
      Select-Object -First 1

$ctxs = @("api-gateway","frontend",
          "services\ingestion-service","services\embedding-service",
          "services\graph-service","services\query-service","services\timeline-service")

if ($ca) {
    $b64 = [Convert]::ToBase64String($ca.RawData, 'InsertLineBreaks')
    $pem = "-----BEGIN CERTIFICATE-----`r`n$b64`r`n-----END CERTIFICATE-----`r`n"
    foreach ($c in $ctxs) { Set-Content -Path "$c\corp-ca.crt" -Value $pem -Encoding ascii }
    Write-Host "Wrote corp-ca.crt to all build contexts."
} else {
    # Not behind TLS inspection (or a different proxy): create empty files so the Dockerfile COPY succeeds.
    foreach ($c in $ctxs) { New-Item -ItemType File -Path "$c\corp-ca.crt" -Force | Out-Null }
    Write-Host "No Cloudflare Gateway CA found; created empty corp-ca.crt files (harmless)."
}
```

> If your company uses a different inspection proxy (e.g. Zscaler), change the `Where-Object` filter to match its CA name, or use Option B which grabs whatever CA is actually presented.

### Option B — Git Bash (extract from a live TLS connection)

Open **Git Bash** in `decision-dna/` and run:

```bash
echo | openssl s_client -connect pypi.org:443 -showcerts 2>/dev/null \
  | awk '/-----BEGIN CERTIFICATE-----/{c++} c>=2{print}' > corp-ca.crt

for d in api-gateway frontend services/ingestion-service services/embedding-service \
         services/graph-service services/query-service services/timeline-service; do
  cp corp-ca.crt "$d/corp-ca.crt"
done
```

### Verify

```powershell
Get-ChildItem .\api-gateway\corp-ca.crt, .\services\query-service\corp-ca.crt
```
Both should exist. (These files are gitignored — never commit them.)

---

## 4. Create `.env`

From `decision-dna\`, create the file:

```powershell
notepad .env
```

Paste this, fill in the three real keys, then **save**:

```env
# --- SECRETS (fill these in) ---
OPENAI_API_KEY=your-openai-key
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
```

> ⚠️ **Windows line-endings gotcha:** if you paste keys with a trailing space or Notepad adds a stray character, you can get spurious `401 Unauthorized` errors. Keep each `KEY=value` on one clean line. VS Code (bottom-right "CRLF"→"LF") avoids the issue entirely.

**Pinecone prerequisite:** in the Pinecone console, confirm a serverless index named **`ai-asset`** exists with **1024 dimensions** and **cosine** metric. Without it, the embedding/query/timeline services will exit at startup.

---

## 5. Check host ports

By default the stack uses standard host ports: API gateway **8000**, Redis **6379**, UI **8501**, Neo4j **7474/7687**, services **8001–8005**. On a typical machine these are free and you can skip straight to Section 6.

Check the two most likely to clash:
```powershell
Test-NetConnection -ComputerName localhost -Port 8000   # TcpTestSucceeded = True means it's taken
Test-NetConnection -ComputerName localhost -Port 6379
```

If either is already in use, don't edit `docker-compose.yml` — just add an override to your `.env` and re-run:
```env
GATEWAY_HOST_PORT=8080
REDIS_HOST_PORT=6380
```
The gateway's internal port stays 8000, so the **UI keeps working** (it uses Docker's internal network). But `scripts\ingest_all.py` hits `localhost:8000` directly — if you set `GATEWAY_HOST_PORT` to something else, use that port in the `curl`/`Invoke-RestMethod` examples below instead (run `docker compose port api-gateway 8000` to confirm it). `start.ps1` detects the gateway port automatically either way.

---

## 6. Build and start everything

From `decision-dna\`:

```powershell
docker compose up --build -d
```

First build downloads images and installs Python deps for six services — **5–10 minutes**. Subsequent starts are seconds.

Watch it come up:
```powershell
docker compose ps
```

Wait ~60 seconds for Neo4j to report `(healthy)`, then check the gateway:
```powershell
curl.exe http://localhost:8000/health
```
A healthy response looks like:
```json
{"gateway":"healthy","services":{"ingestion":"healthy","embedding":"healthy","graph":"healthy","query":"healthy","timeline":"healthy"}}
```

If `embedding`/`query`/`timeline` show `unreachable`, that's almost always a Pinecone key/index problem — see Troubleshooting.

---

## 7. Load the data

The repo ships small sample datasets in `data\synthetic\`, so you can ingest immediately.

### 7a. (Optional) generate a larger synthetic dataset
Requires Python on Windows:
```powershell
pip install faker
python scripts\generate_data.py
```

### 7b. Trigger ingestion (no Python needed)

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/ingest `
  -ContentType 'application/json' `
  -Body '{"data_dir":"/app/data/synthetic","trigger_embedding":true,"trigger_graph":true}'
```

This returns a `job_id`. Poll status:
```powershell
Invoke-RestMethod http://localhost:8000/api/v1/ingest/status/<job_id>
```

> With the default port 8000, you can instead just run `python scripts\ingest_all.py` (it targets `localhost:8000`).

---

## 8. Use the app

| What | URL |
|---|---|
| **Streamlit UI** | http://localhost:8501 |
| API Gateway | http://localhost:8000 |
| Scalar API docs | http://localhost:8000/scalar |
| Neo4j Browser | http://localhost:7474  (user `neo4j`, password `password123`) |

The UI reaches the gateway over Docker's internal network, so it works regardless of which host port the gateway is published on.

---

## 9. Everyday commands

```powershell
docker compose ps                       # status
docker compose logs -f query-service    # tail one service's logs
docker compose restart query-service    # restart one service
docker compose down                      # stop & remove containers (keeps data volumes)
docker compose down -v                   # also wipe Neo4j/Redis data volumes
docker compose up -d --force-recreate embedding-service query-service timeline-service  # after editing .env
```

> **Note:** services run `uvicorn --reload`, so a container can show **`running`** in `docker compose ps` even though the app crashed at startup. Always confirm with `docker compose logs <service>`.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails: `COPY corp-ca.crt ... not found` | Section 3 skipped | Create `corp-ca.crt` in every build context (Section 3), then rebuild. |
| Build fails: `CERTIFICATE_VERIFY_FAILED ... self-signed certificate` during `pip install` | Corporate TLS interception; CA not trusted in build | Ensure `corp-ca.crt` holds your real corporate root CA (Option A found a cert, not an empty file). Rebuild with `--no-cache`. |
| Startup log: `pinecone ... UnauthorizedException: (401)` | Placeholder/invalid `PINECONE_API_KEY`, or index missing | Put a real key in `.env`; create the `ai-asset` (1024-dim, cosine) index; `docker compose up -d --force-recreate embedding-service query-service timeline-service`. |
| Startup log: `api.pinecone.io ... CERTIFICATE_VERIFY_FAILED` at **runtime** | Corporate CA not in certifi's bundle | Confirm the Dockerfiles include the `certifi.where()` append line; rebuild. |
| `Bind for 0.0.0.0:XXXX failed: port is already allocated` | Another app/container holds that host port | Find it: `docker ps --filter publish=XXXX`. Either stop it, or change the left-hand host port in `docker-compose.yml` (e.g. `"8081:8000"`). |
| `api-gateway` exits: `cannot import name 'get_scalar_api_reference'` | Old `main.py` without the import fix | Pull a repo that includes the resilient import in `api-gateway/app/main.py`. |
| Gateway `unreachable` for some services in `/health` | Those services crashed at startup | `docker compose logs <service>` and match the error above. |
| UI shows **"Connection error"**; console spams `localhost:8501/_stcore/health ... ERR_CONNECTION_REFUSED` (typically when the UI is on port 3000) | Streamlit's client bundle has a hardcoded dev-mode heuristic (`window.location.port === 3000` ⇒ assume backend on 8501). Serving the UI on **3000** always breaks it; `--browser.serverPort` does NOT override it. | Serve the UI on **8501** (this repo now does: `frontend/Dockerfile` uses `--server.port=8501`, compose maps `8501:8501`). `git pull`, rebuild the frontend, hard-refresh (Ctrl+Shift+R), and open **http://localhost:8501**. |
| Everything slow / builds hang | Docker has too little RAM, or repo on a slow share | Raise Docker RAM to 8 GB+; move the repo to WSL2/local disk. |
| Volume mount errors for `./data` | Drive not shared with Docker | Docker Desktop → Settings → Resources → File Sharing, add the drive. |

---

### Note for the person sharing this repo
`.env` and `corp-ca.crt` are gitignored (correct — don't commit them). But make sure these **are** committed and pushed so your colleague's clone works:
`decision-dna/docker-compose.yml`, all 7 `Dockerfile`s, `decision-dna/api-gateway/app/main.py`, and this file. Your colleague generates their own `.env` and `corp-ca.crt` per Sections 3–4.

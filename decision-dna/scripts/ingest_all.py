"""
scripts/ingest_all.py
Triggers the ingestion pipeline after docker-compose is up.
Run: python scripts/ingest_all.py

Runs a credential preflight first (see preflight()) so an invalid Pinecone or
OpenAI key fails loudly BEFORE a full ingest — instead of silently degrading
into a throwaway in-memory index that query-service can never read.
"""

import os
import sys
import time

import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")
# embedding-service is published on 8002 by docker-compose; override if remapped
EMBEDDING = os.getenv("EMBEDDING_URL", "http://localhost:8002")


def preflight():
    """Verify OpenAI + Pinecone credentials via embedding-service /selftest.

    The check runs inside the container (correct network + corp CA + the real
    keys), so it catches the OpenAI 429 quota error and the Pinecone 401 that
    /health can't see at startup. Exits non-zero and loudly on any failure.
    """
    print("🔐 Preflight: verifying OpenAI + Pinecone credentials...")
    try:
        resp = httpx.get(f"{EMBEDDING}/selftest", timeout=60)
    except Exception as e:
        print(f"❌ Could not reach embedding-service at {EMBEDDING}: {e}")
        print("   Is the stack up? Run `docker-compose up --build` first.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"❌ /selftest returned HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)

    data = resp.json()
    openai_res = data.get("openai", {})
    pinecone_res = data.get("pinecone", {})
    ok = True

    if openai_res.get("ok"):
        print("  ✅ OpenAI embeddings: reachable")
    else:
        ok = False
        print(f"  ❌ OpenAI embeddings: {openai_res.get('error')}")

    if pinecone_res.get("ok"):
        print(f"  ✅ Pinecone: index '{pinecone_res.get('index')}' "
              f"reachable (dim={pinecone_res.get('dimension')})")
    else:
        ok = False
        print(f"  ❌ Pinecone: {pinecone_res.get('error')}")

    if not ok:
        print()
        print("🛑 Aborting BEFORE ingest.")
        print("   With an invalid key, embedding falls back to a per-process in-memory index")
        print("   that query-service cannot read — ingest would 'succeed' but every query")
        print("   returns 0 chunks / 0% confidence.")
        print("   Fix the failing key in decision-dna/.env, rebuild that service")
        print("   (`docker-compose up --build embedding-service query-service`), then re-run.")
        sys.exit(1)

    print("  ✅ All credentials valid — proceeding.\n")


def check_health():
    print("🔍 Checking service health...")
    resp = httpx.get(f"{GATEWAY}/health", timeout=10)
    data = resp.json()
    for service, status in data.get("services", {}).items():
        icon = "✅" if status == "healthy" else "❌"
        print(f"  {icon} {service}: {status}")
    print()


def ingest():
    print("🚀 Triggering ingestion pipeline...")

    response = httpx.post(
        f"{GATEWAY}/api/v1/ingest",
        json={
            "data_dir": "/app/data/synthetic",
            "trigger_embedding": True,
            "trigger_graph": True,
        },
        timeout=10,
    )
    response.raise_for_status()
    job = response.json()
    job_id = job["job_id"]
    print(f"📋 Job ID: {job_id}")
    print("⏳ Polling for completion...")

    while True:
        time.sleep(3)
        status_resp = httpx.get(f"{GATEWAY}/api/v1/ingest/status/{job_id}", timeout=10)
        if status_resp.status_code == 200:
            data = status_resp.json()
            status = data.get("status", "unknown")
            print(f"   Status: {status} | {data.get('progress', '')}")
            if status == "completed":
                print(f"✅ Ingestion complete! Total docs: {data.get('total_docs', '?')}")
                break
            elif status == "failed":
                print(f"❌ Ingestion failed: {data.get('error', 'unknown')}")
                break
        else:
            print(f"   Could not get status: {status_resp.status_code}")


if __name__ == "__main__":
    preflight()
    check_health()
    ingest()

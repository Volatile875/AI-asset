"""
scripts/ingest_all.py
Triggers the ingestion pipeline after docker-compose is up.
Run: python scripts/ingest_all.py
"""

import httpx
import time

GATEWAY = "http://localhost:8000"


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


def check_health():
    print("🔍 Checking service health...")
    resp = httpx.get(f"{GATEWAY}/health", timeout=10)
    data = resp.json()
    for service, status in data.get("services", {}).items():
        icon = "✅" if status == "healthy" else "❌"
        print(f"  {icon} {service}: {status}")
    print()


if __name__ == "__main__":
    check_health()
    ingest()

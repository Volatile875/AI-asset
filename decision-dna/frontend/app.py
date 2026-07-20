"""
frontend/app.py
DecisionDNA — Streamlit UI
The demo-ready frontend for the hackathon.
"""

import logging
import os
import time

import httpx
import streamlit as st

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [frontend] %(message)s",
)
log = logging.getLogger("frontend")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
log.info("frontend starting; GATEWAY_URL=%s", GATEWAY_URL)

# ── Page Config ────────────────────────────────────────────────

st.set_page_config(
    page_title="DecisionDNA — Organizational Memory",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────

st.markdown("""
<style>
    /* Main background */
    .stApp { background-color: #0d1117; color: #e6edf3; }

    /* Cards */
    .dna-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 20px;
        margin: 10px 0;
    }

    /* Timeline */
    .timeline-event {
        border-left: 3px solid #238636;
        padding-left: 16px;
        margin: 12px 0;
        position: relative;
    }
    .timeline-event.critical { border-left-color: #da3633; }
    .timeline-event.dissent  { border-left-color: #f85149; }
    .timeline-event.concern  { border-left-color: #d29922; }

    /* Confidence bar */
    .confidence-bar {
        height: 8px;
        background: #238636;
        border-radius: 4px;
    }

    /* Source chips */
    .source-chip {
        display: inline-block;
        background: #1f6feb33;
        border: 1px solid #1f6feb;
        border-radius: 20px;
        padding: 2px 10px;
        font-size: 12px;
        margin: 2px;
        color: #58a6ff;
    }

    /* Answer box */
    .answer-box {
        background: #161b22;
        border: 1px solid #238636;
        border-radius: 12px;
        padding: 24px;
        line-height: 1.7;
    }

    /* Step badge */
    .step-badge {
        background: #21262d;
        border-radius: 6px;
        padding: 4px 10px;
        font-size: 12px;
        color: #8b949e;
        margin: 2px 0;
        display: block;
    }

    h1 { color: #58a6ff !important; }
    h2, h3 { color: #e6edf3 !important; }
</style>
""", unsafe_allow_html=True)


# ── Sidebar ────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🧬 DecisionDNA")
    st.markdown("*AI Organizational Memory Engine*")
    st.divider()

    page = st.radio(
        "Navigate",
        ["🔍 Ask DecisionDNA", "📅 Decision Timeline", "🕸️ Knowledge Graph", "⚙️ Ingest Data", "❤️ Health"],
        label_visibility="collapsed",
    )

    st.divider()
    project_filter = st.selectbox(
        "Filter by Project",
        ["All Projects", "CloudMigration", "DataPlatform", "AuthRefactor", "MobileApp"],
    )
    project = None if project_filter == "All Projects" else project_filter

    st.divider()
    st.markdown("**Example Questions:**")
    examples = [
        "Why did we reject Vendor X?",
        "Why did we migrate to Azure Functions?",
        "What security risks were flagged in the auth refactor?",
        "Who raised concerns about the database migration?",
        "What decisions were made about JWT tokens?",
    ]
    for q in examples:
        if st.button(q, key=q, use_container_width=True):
            st.session_state["prefill_question"] = q


# ── Helper Functions ───────────────────────────────────────────

def call_gateway(path: str, method: str = "GET", payload: dict = None) -> dict:
    url = f"{GATEWAY_URL}{path}"
    start = time.perf_counter()
    log.info("→ %s %s", method, url)
    try:
        with httpx.Client(timeout=90) as client:
            if method == "POST":
                r = client.post(url, json=payload)
            else:
                r = client.get(url)
            dur = (time.perf_counter() - start) * 1000
            log.info("← %s %s %s %.0fms", method, url, r.status_code, dur)
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError as e:
        log.error("✗ %s %s connection refused: %r", method, url, e)
        st.error("❌ Cannot connect to API Gateway. Is docker-compose running?")
        return {}
    except httpx.TimeoutException as e:
        log.error("✗ %s %s timed out after %.0fms: %r", method, url, (time.perf_counter() - start) * 1000, e)
        st.error("⏱️ Request timed out. The pipeline may still be running — check the query-service logs.")
        return {}
    except httpx.HTTPStatusError as e:
        # Surface the gateway's error detail (now populated by the hardened proxy) to the UI.
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            detail = e.response.text[:300]
        log.error("✗ %s %s → HTTP %s: %s", method, url, e.response.status_code, detail)
        st.error(f"Error {e.response.status_code}: {detail or e}")
        return {}
    except Exception as e:
        log.exception("✗ %s %s unexpected error", method, url)
        st.error(f"Error: {e}")
        return {}


def render_confidence(score: float):
    pct = int(score * 100)
    color = "#238636" if pct > 70 else "#d29922" if pct > 40 else "#da3633"
    st.markdown(f"""
    <div style='margin: 8px 0;'>
        <div style='font-size: 12px; color: #8b949e; margin-bottom: 4px;'>
            Confidence Score: <b style='color:{color}'>{pct}%</b>
        </div>
        <div style='background: #21262d; border-radius: 4px; height: 8px;'>
            <div style='background: {color}; width: {pct}%; height: 8px; border-radius: 4px;'></div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_timeline(timeline: dict):
    events = timeline.get("events", [])
    if not events:
        st.info("No timeline events found.")
        return

    st.markdown(f"**{len(events)} events found for:** *{timeline.get('topic', '')}*")

    for event in events:
        sentiment = event.get("sentiment", "neutral")
        css_class = (
            "critical" if event.get("is_critical") else
            "dissent"  if sentiment == "dissent" else
            "concern"  if sentiment == "concern" else
            ""
        )
        icon = event.get("icon", "📅")
        participants = ", ".join(event.get("participants", []))

        st.markdown(f"""
        <div class='timeline-event {css_class}'>
            <div style='font-size: 12px; color: #8b949e;'>{event.get('date', '?')} · {event.get('event_type', '').upper()}</div>
            <div style='font-weight: bold; margin: 4px 0;'>{icon} {event.get('title', '')}</div>
            <div style='color: #8b949e; font-size: 14px;'>{event.get('description', '')}</div>
            {'<div style="font-size: 12px; color: #58a6ff; margin-top: 4px;">👥 ' + participants + '</div>' if participants else ''}
        </div>
        """, unsafe_allow_html=True)

    if timeline.get("outcome_assessment"):
        st.markdown(f"""
        <div class='dna-card' style='border-color: #238636; margin-top: 16px;'>
            <b>🎯 Outcome Assessment</b><br>
            <span style='color: #8b949e;'>{timeline['outcome_assessment']}</span>
        </div>
        """, unsafe_allow_html=True)

    render_confidence(timeline.get("confidence_score", 0.0))


# ── Pages ──────────────────────────────────────────────────────

# ── Page 1: Ask ────────────────────────────────────────────────

if page == "🔍 Ask DecisionDNA":
    st.title("🧬 Ask DecisionDNA")
    st.markdown("*Ask any question about past decisions, rejected vendors, architecture choices, or forgotten context.*")

    prefill = st.session_state.pop("prefill_question", "")
    question = st.text_input(
        "Your Question",
        value=prefill,
        placeholder="Why did we reject Vendor X in Q1 2024?",
        label_visibility="collapsed",
    )

    col1, col2 = st.columns([1, 5])
    with col1:
        ask_clicked = st.button("🔍 Ask", type="primary", use_container_width=True)

    if ask_clicked and question:
        with st.spinner("🤖 Running 5-agent pipeline..."):
            result = call_gateway(
                "/api/v1/query",
                "POST",
                {"question": question, "project_filter": project},
            )

        if result:
            # Answer
            st.markdown("### 💡 Answer")
            st.markdown(f"""
            <div class='answer-box'>
                {result.get('answer', 'No answer generated.').replace(chr(10), '<br>')}
            </div>
            """, unsafe_allow_html=True)

            render_confidence(result.get("confidence_score", 0.0))

            col_left, col_right = st.columns([3, 2])

            with col_left:
                # Timeline
                timeline = result.get("timeline")
                if timeline and timeline.get("events"):
                    st.markdown("### 📅 Decision Timeline")
                    render_timeline(timeline)

            with col_right:
                # Sources
                sources = result.get("sources", [])
                if sources:
                    st.markdown("### 📎 Sources")
                    for src in sources:
                        score_pct = int(src.get("relevance_score", 0) * 100)
                        st.markdown(f"""
                        <div class='dna-card' style='padding: 12px;'>
                            <div style='font-size: 11px; color: #8b949e;'>
                                {src.get('doc_type','').upper()} · {src.get('date','')[:10]} · Match: {score_pct}%
                            </div>
                            <div style='font-weight: bold; font-size: 14px; margin: 4px 0;'>{src.get('title','')}</div>
                            <div style='font-size: 12px; color: #8b949e;'>{src.get('excerpt','')[:150]}...</div>
                        </div>
                        """, unsafe_allow_html=True)

                # Agent Steps
                steps = result.get("processing_steps", [])
                if steps:
                    st.markdown("### 🤖 Agent Pipeline")
                    for step in steps:
                        st.markdown(f"<span class='step-badge'>✓ {step}</span>", unsafe_allow_html=True)

    elif ask_clicked:
        st.warning("Please enter a question.")


# ── Page 2: Timeline ───────────────────────────────────────────

elif page == "📅 Decision Timeline":
    st.title("📅 Decision Timeline")
    st.markdown("*Visualize the full chronological trail of any topic.*")

    topic = st.text_input("Topic to explore", placeholder="Azure migration, Vendor X, Auth refactor...")
    if st.button("Build Timeline", type="primary") and topic:
        with st.spinner("Building timeline..."):
            params = f"?topic={topic}" + (f"&project={project}" if project else "")
            result = call_gateway(f"/api/v1/timeline/{topic}{params}")

        if result:
            render_timeline(result)


# ── Page 3: Knowledge Graph ────────────────────────────────────

elif page == "🕸️ Knowledge Graph":
    st.title("🕸️ Knowledge Graph")
    st.markdown("*Explore relationships between people, decisions, and projects.*")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📋 Recent Decisions")
        result = call_gateway(f"/api/v1/graph/decisions" + (f"?project={project}" if project else ""))
        decisions = result.get("decisions", [])
        if decisions:
            for d in decisions[:10]:
                st.markdown(f"""
                <div class='dna-card' style='padding: 12px;'>
                    <div style='font-size: 11px; color: #8b949e;'>{d.get('date','')[:10]} · {d.get('project','')}</div>
                    <div style='font-size: 14px;'>{d.get('description','')[:200]}</div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.info("No decisions in graph yet. Ingest data first.")

    with col2:
        st.markdown("#### 🔎 Explore Entity")
        entity = st.text_input("Person or Project name", placeholder="Ravi Sharma")
        if st.button("Explore") and entity:
            result = call_gateway(f"/api/v1/graph/entities/{entity}")
            connections = result.get("connections", [])
            if connections:
                for conn in connections[:10]:
                    st.markdown(f"""
                    <div class='dna-card' style='padding: 10px;'>
                        <span style='color: #58a6ff;'>{conn.get('rel','?')}</span> →
                        {conn.get('labels',['?'])[0]}: {str(conn.get('m',''))[:100]}
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("No connections found.")


# ── Page 4: Ingest ─────────────────────────────────────────────

elif page == "⚙️ Ingest Data":
    st.title("⚙️ Ingest Data")
    st.markdown("*Load your synthetic or real corporate data into DecisionDNA.*")

    st.markdown("""
    <div class='dna-card'>
        <b>Data Sources:</b><br>
        📧 Emails &nbsp;|&nbsp; 📋 Meeting Notes &nbsp;|&nbsp; 🎫 Jira Tickets<br><br>
        Place JSON files in:<br>
        <code>data/synthetic/emails/</code><br>
        <code>data/synthetic/meetings/</code><br>
        <code>data/synthetic/jira/</code>
    </div>
    """, unsafe_allow_html=True)

    if st.button("🚀 Start Ingestion", type="primary"):
        with st.spinner("Ingesting..."):
            result = call_gateway(
                "/api/v1/ingest",
                "POST",
                {"data_dir": "/app/data/synthetic", "trigger_embedding": True, "trigger_graph": True},
            )

        if result:
            job_id = result.get("job_id", "?")
            st.success(f"✅ Job started: `{job_id}`")
            st.info("Poll `/api/v1/ingest/status/{job_id}` for progress or refresh this page.")


# ── Page 5: Health ─────────────────────────────────────────────

elif page == "❤️ Health":
    st.title("❤️ System Health")

    result = call_gateway("/health")
    if result:
        gateway_status = result.get("gateway", "unknown")
        st.metric("API Gateway", gateway_status.upper())

        services = result.get("services", {})
        cols = st.columns(len(services))
        for col, (name, status) in zip(cols, services.items()):
            with col:
                icon = "✅" if status == "healthy" else "❌"
                st.metric(name, f"{icon} {status}")

        st.caption(f"Last checked: {result.get('timestamp', '')}")

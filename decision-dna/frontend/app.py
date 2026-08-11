"""
frontend/app.py
DecisionDNA - Streamlit UI
Visual language rebuilt from scratch to match kiro.dev: flat black canvas,
a plain top nav bar, a big bold rounded headline, an eyebrow badge, pill CTA
buttons (incl. a split "code snippet" pill), a glowing preview mockup,
and a two-column spec/checklist section.
"""

import html
import logging
import os
import time
from typing import Optional

import httpx
import streamlit as st


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [frontend] %(message)s",
)
log = logging.getLogger("frontend")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
log.info("frontend starting; GATEWAY_URL=%s", GATEWAY_URL)

st.set_page_config(
    page_title="DecisionDNA - Organizational Memory",
    page_icon="DD",
    layout="wide",
    initial_sidebar_state="collapsed",
)


st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

    :root {
        --bg: #000000;
        --card: #0a0a0b;
        --card-hover: #101012;
        --border: rgba(255,255,255,0.11);
        --border-strong: rgba(255,255,255,0.22);
        --text: #ffffff;
        --muted: #9a9aa2;
        --muted-2: #6c6c74;
        --accent: #6d5ef7;
        --accent-2: #8f7bff;
        --accent-soft: rgba(109,94,247,0.14);
        --accent-border: rgba(109,94,247,0.45);
        --green: #35d281;
        --amber: #f5a524;
        --red: #ff5470;
        --mono: 'JetBrains Mono', Consolas, monospace;
        --display: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    html, body, [class*="css"] { font-family: var(--display); }
    #MainMenu, header, footer { visibility: hidden; }
    section[data-testid="stSidebar"] { display: none; }

    .stApp { background: #000000; color: var(--text); }
    .block-container { padding-top: 0.6rem; padding-bottom: 3rem; max-width: 1180px; }

    .topnav {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 1rem 0 1.1rem 0;
        border-bottom: 1px solid var(--border);
        margin-bottom: 0.4rem;
    }
    .topnav-left { display: flex; align-items: center; gap: 0.6rem; }
    .topnav-logo-mark {
        width: 30px; height: 30px; border-radius: 8px;
        background: var(--accent);
        display: grid; place-items: center;
        font-family: var(--mono); font-weight: 700; font-size: 0.72rem; color: #fff;
    }
    .topnav-logo-word {
        font-family: var(--mono); font-weight: 700; font-size: 1rem;
        letter-spacing: 0.06em; color: #fff;
    }
    .topnav-right { display: flex; align-items: center; gap: 0.7rem; }
    .navpill {
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 0.42rem 0.9rem;
        font-family: var(--mono);
        font-size: 0.72rem;
        letter-spacing: 0.03em;
        color: var(--muted);
        text-transform: uppercase;
        white-space: nowrap;
    }
    .navpill-solid {
        background: #ffffff; color: #000000; border: none; font-weight: 700;
    }

    div[data-testid="stHorizontalBlock"] .stRadio [role="radiogroup"] {
        gap: 1.6rem;
        flex-wrap: wrap;
    }
    .stRadio [role="radiogroup"] label {
        background: transparent !important;
    }
    .stRadio [role="radiogroup"] label p {
        font-family: var(--mono) !important;
        font-size: 0.76rem !important;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--muted) !important;
    }
    .stRadio [role="radiogroup"] label:has(input:checked) p {
        color: #ffffff !important;
    }
    .stRadio [role="radiogroup"] input { accent-color: var(--accent); }

    .eyebrow {
        display: inline-flex; align-items: center; gap: 0.5rem;
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 0.4rem 0.9rem 0.4rem 0.6rem;
        font-family: var(--mono);
        font-size: 0.72rem;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #c9c9d2;
    }
    .eyebrow::before {
        content: "";
        width: 6px; height: 6px; border-radius: 999px;
        background: var(--accent);
        box-shadow: 0 0 10px 1px rgba(109,94,247,0.75);
    }

    .hero { padding: 2.6rem 0 1.6rem 0; }
    .hero h1 {
        font-family: var(--display);
        font-weight: 700;
        font-size: clamp(2.3rem, 4.4vw, 4rem);
        line-height: 1.03;
        letter-spacing: -0.025em;
        color: #ffffff;
        margin: 1.1rem 0 1rem 0;
        max-width: 18ch;
    }
    .hero p {
        color: var(--muted);
        font-size: 1.08rem;
        line-height: 1.68;
        max-width: 62ch;
        margin: 0;
    }
    .hero-notes {
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 1rem 1.1rem;
        background: var(--card);
    }
    .hero-notes .notes-label {
        display: flex; align-items: center; gap: 0.45rem;
        font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.06em;
        text-transform: uppercase; color: var(--muted-2); margin-bottom: 0.7rem;
    }
    .hero-notes .notes-label::before {
        content: ""; width: 6px; height: 6px; border-radius: 999px; background: var(--accent);
    }
    .notes-item {
        font-size: 0.85rem; color: #d6d6dc; line-height: 1.5;
        padding: 0.55rem 0; border-top: 1px solid var(--border);
    }
    .notes-item:first-of-type { border-top: none; padding-top: 0; }

    .cta-row { display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 1.6rem; }
    .btn-solid, .btn-outline {
        display: inline-flex; align-items: center; gap: 0.5rem;
        font-family: var(--display); font-weight: 600; font-size: 0.92rem;
        border-radius: 999px; padding: 0.85rem 1.4rem; cursor: pointer;
        border: none; text-decoration: none;
    }
    .btn-solid { background: var(--accent); color: #ffffff; }
    .btn-solid:hover { background: var(--accent-2); }
    .btn-outline { background: transparent; border: 1px solid var(--border-strong); color: #fff; }
    .btn-split { display: inline-flex; align-items: stretch; border-radius: 999px; overflow: hidden; }
    .btn-split .part-label {
        background: var(--accent); color: #fff; font-family: var(--display); font-weight: 600;
        font-size: 0.92rem; padding: 0.85rem 1.1rem; display: flex; align-items: center;
    }
    .btn-split .part-code {
        background: #000000; border: 1px solid var(--border); border-left: none;
        color: #d6d6dc; font-family: var(--mono); font-size: 0.82rem;
        padding: 0.85rem 1.1rem; display: flex; align-items: center; gap: 0.6rem;
    }

    .stButton > button {
        border: none !important;
        background: var(--accent) !important;
        color: #ffffff !important;
        border-radius: 999px !important;
        font-family: var(--display) !important;
        font-weight: 600 !important;
        padding: 0.8rem 1.3rem !important;
    }
    .stButton > button:hover { background: var(--accent-2) !important; }
    .btn-secondary-row .stButton > button {
        background: transparent !important;
        border: 1px solid var(--border-strong) !important;
        color: #fff !important;
    }

    .preview-wrap { position: relative; margin-top: 2.2rem; padding: 3px; border-radius: 26px;
        background: radial-gradient(120% 160% at 20% 0%, rgba(143,123,255,0.55), rgba(109,94,247,0.08) 45%, transparent 70%); }
    .preview-window {
        background: #050506; border-radius: 24px; border: 1px solid var(--border);
        overflow: hidden;
    }
    .preview-titlebar {
        display: flex; align-items: center; gap: 0.5rem;
        padding: 0.7rem 1rem; border-bottom: 1px solid var(--border);
    }
    .tl-dot { width: 10px; height: 10px; border-radius: 999px; }
    .preview-search {
        margin-left: 0.8rem; flex: 1;
        border: 1px solid var(--border); border-radius: 8px;
        padding: 0.35rem 0.7rem; color: var(--muted-2);
        font-family: var(--mono); font-size: 0.76rem;
    }
    .preview-body { display: grid; grid-template-columns: 1.1fr 1.6fr 1.2fr; min-height: 260px; }
    .preview-col { padding: 1rem; border-right: 1px solid var(--border); }
    .preview-col:last-child { border-right: none; }
    .preview-col-title {
        font-family: var(--mono); font-size: 0.68rem; letter-spacing: 0.06em;
        text-transform: uppercase; color: var(--muted-2); margin-bottom: 0.7rem;
    }
    .preview-row {
        padding: 0.55rem 0.6rem; border-radius: 8px; margin-bottom: 0.4rem;
        font-size: 0.82rem; color: #d6d6dc; background: rgba(255,255,255,0.03);
    }
    .preview-row.active { background: var(--accent-soft); color: #fff; border: 1px solid var(--accent-border); }
    .preview-chat-line { font-size: 0.85rem; color: #c9c9d2; line-height: 1.6; margin-bottom: 0.7rem; }
    .preview-code {
        font-family: var(--mono); font-size: 0.76rem; color: var(--accent-2);
        background: rgba(109,94,247,0.1); border-radius: 6px; padding: 0.1rem 0.4rem;
    }
    .preview-badge-row { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.7rem; }
    .preview-badge {
        font-family: var(--mono); font-size: 0.68rem; padding: 0.25rem 0.55rem;
        border-radius: 6px; border: 1px solid var(--border); color: var(--muted);
    }
    .preview-badge.merged { background: rgba(53,210,129,0.14); color: #58e6a0; border-color: rgba(53,210,129,0.3); }

    .spec-section { padding: 4rem 0 2rem 0; }
    .spec-heading {
        font-family: var(--display); font-weight: 700; font-size: clamp(1.7rem, 2.6vw, 2.5rem);
        line-height: 1.12; letter-spacing: -0.02em; color: #fff; margin-bottom: 1rem;
    }
    .spec-copy { color: var(--muted); font-size: 1rem; line-height: 1.75; }
    .spec-copy u { color: #cfcaff; text-decoration-color: rgba(143,123,255,0.6); }
    .spec-card { border: 1px solid var(--border); border-radius: 20px; background: var(--card); overflow: hidden; }
    .spec-card-head {
        display: flex; align-items: center; justify-content: space-between;
        padding: 0.9rem 1.1rem; border-bottom: 1px solid var(--border);
    }
    .task-tree { padding: 1.1rem 1.2rem; font-family: var(--display); font-size: 0.88rem; }
    .task-l1 { display: flex; align-items: center; gap: 0.5rem; color: #fff; font-weight: 600; margin: 0.7rem 0 0.35rem 0; }
    .task-l2 { display: flex; align-items: center; gap: 0.5rem; color: #e2e2e8; margin: 0.5rem 0 0.3rem 1.3rem; }
    .task-leaf { color: var(--muted); font-size: 0.83rem; margin: 0.3rem 0 0.3rem 2.4rem; display: flex; gap: 0.4rem; align-items: flex-start; }
    .task-leaf::before { content: "•"; color: var(--muted-2); }
    .task-circle { width: 13px; height: 13px; border-radius: 999px; border: 1.5px solid var(--muted-2); flex-shrink: 0; }
    .task-code { font-family: var(--mono); color: var(--accent-2); background: rgba(109,94,247,0.1); border-radius: 4px; padding: 0 0.35rem; }

    .chip-strip { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1.4rem; }
    .feature-chip {
        border: 1px solid var(--border); border-radius: 999px; background: #000;
        color: #c9c9d2; font-family: var(--mono); font-size: 0.72rem;
        padding: 0.4rem 0.85rem;
    }

    .panel, .stat-card, .dna-card {
        background: var(--card); border: 1px solid var(--border); border-radius: 18px;
    }
    .panel, .stat-card { padding: 1.15rem 1.2rem; }
    .panel:hover, .stat-card:hover, .dna-card:hover { border-color: var(--border-strong); background: var(--card-hover); }
    .stat-label { font-family: var(--mono); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
    .stat-value { font-family: var(--display); font-size: 1.4rem; font-weight: 700; color: #fff; margin-top: 0.3rem; }
    .stat-note { font-size: 0.83rem; color: var(--muted-2); margin-top: 0.3rem; line-height: 1.5; }
    .section-title { font-family: var(--display); font-weight: 700; font-size: 1.2rem; color: #fff; margin: 0 0 0.3rem 0; }
    .section-kicker { color: var(--muted); font-size: 0.9rem; margin-bottom: 0.85rem; line-height: 1.5; }

    .timeline-tag, .step-badge {
        display: inline-flex; align-items: center; gap: 0.3rem;
        border: 1px solid var(--border); border-radius: 999px; background: #000;
        color: #d6d6dc; font-family: var(--mono); font-size: 0.71rem;
        padding: 0.32rem 0.7rem; margin: 0.12rem 0.18rem 0.12rem 0;
    }

    .input-shell { border: 1px solid var(--border); border-radius: 18px; background: var(--card); padding: 1.05rem; margin-top: 0.8rem; }
    .input-shell [data-testid="stTextInput"] input, .panel [data-testid="stTextInput"] input {
        background: #000 !important; color: #fff !important; font-family: var(--mono) !important;
        border-radius: 10px !important; border: 1px solid var(--border) !important; padding: 0.95rem 1rem !important;
    }
    .input-shell [data-testid="stTextInput"] input:focus {
        border-color: var(--accent-border) !important; box-shadow: 0 0 0 3px var(--accent-soft) !important;
    }

    .answer-box { border: 1px solid var(--border); border-radius: 16px; background: #000; padding: 1.25rem 1.3rem; color: #f0f0f2; line-height: 1.75; }
    .answer-box p { margin-top: 0; }

    .timeline-event { position: relative; border: 1px solid var(--border); border-radius: 16px; background: var(--card); padding: 1rem 1.05rem 1rem 1.15rem; margin-left: 0.5rem; }
    .timeline-event::before { content: ""; position: absolute; left: -0.55rem; top: 1.2rem; width: 9px; height: 9px; border-radius: 999px; background: var(--accent); box-shadow: 0 0 0 5px var(--accent-soft); }
    .timeline-event.critical { border-left: 3px solid var(--red); }
    .timeline-event.dissent { border-left: 3px solid #ff8fa8; }
    .timeline-event.concern { border-left: 3px solid var(--amber); }
    .timeline-event.agreement { border-left: 3px solid var(--green); }
    .timeline-track { display: grid; gap: 0.75rem; }
    .timeline-meta { font-family: var(--mono); font-size: 0.71rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .timeline-title { font-family: var(--display); font-weight: 700; font-size: 1.02rem; color: #fff; margin: 0.35rem 0; }
    .timeline-description { color: #c4c4ca; font-size: 0.94rem; line-height: 1.6; }
    .timeline-foot { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.6rem; }

    .progress-bar { height: 0.42rem; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; }
    .progress-bar > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
    .progress-label { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 0.78rem; color: var(--muted); margin-bottom: 0.35rem; }
    .pulse { display: inline-flex; align-items: center; gap: 0.4rem; font-family: var(--mono); }
    .pulse::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: var(--green); box-shadow: 0 0 0 0 rgba(53,210,129,0.4); animation: pulse 1.8s infinite; }
    @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(53,210,129,0.4); } 70% { box-shadow: 0 0 0 11px rgba(53,210,129,0); } 100% { box-shadow: 0 0 0 0 rgba(53,210,129,0); } }

    .metric-row { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 0.7rem; }
    .metric-item { border: 1px solid var(--border); border-radius: 12px; background: #000; padding: 0.95rem; }
    .metric-item .label { font-family: var(--mono); font-size: 0.7rem; text-transform: uppercase; color: var(--muted); }
    .metric-item .value { font-family: var(--mono); font-weight: 600; color: #fff; margin-top: 0.3rem; }
    .metric-item .sub { color: var(--muted-2); font-size: 0.81rem; margin-top: 0.25rem; line-height: 1.5; }

    .empty-state { border: 1px dashed var(--border); border-radius: 16px; background: #000; padding: 1.3rem; color: var(--muted); }
    .empty-state strong { color: #fff; }

    div[data-baseweb="select"] > div { background: #000 !important; border-color: var(--border) !important; border-radius: 10px !important; }
    .stTextInput label, .stSelectbox label { color: var(--muted) !important; font-family: var(--mono) !important; font-size: 0.78rem !important; }

    @media (max-width: 900px) {
        .preview-body { grid-template-columns: 1fr; }
        .preview-col { border-right: none; border-bottom: 1px solid var(--border); }
        .metric-row { grid-template-columns: 1fr; }
    }
</style>
""",
    unsafe_allow_html=True,
)


def escape_text(value: object) -> str:
    return html.escape("" if value is None else str(value))


def to_sentence(value: object) -> str:
    return escape_text(value).replace("\n", "<br>")


def call_gateway(path: str, method: str = "GET", payload: Optional[dict] = None) -> dict:
    url = f"{GATEWAY_URL}{path}"
    start = time.perf_counter()
    log.info("→ %s %s", method, url)
    try:
        with httpx.Client(timeout=90) as client:
            if method == "POST":
                response = client.post(url, json=payload)
            else:
                response = client.get(url)
            duration_ms = (time.perf_counter() - start) * 1000
            log.info("← %s %s %s %.0fms", method, url, response.status_code, duration_ms)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError as exc:
        log.error("✗ %s %s connection refused: %r", method, url, exc)
        st.error("Cannot connect to the API gateway. Check that the backend stack is running.")
        return {}
    except httpx.TimeoutException as exc:
        log.error("✗ %s %s timed out after %.0fms: %r", method, url, (time.perf_counter() - start) * 1000, exc)
        st.error("Request timed out. The pipeline may still be running, so check the service logs.")
        return {}
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = exc.response.text[:300]
        log.error("✗ %s %s → HTTP %s: %s", method, url, exc.response.status_code, detail)
        st.error(f"Error {exc.response.status_code}: {detail or exc}")
        return {}
    except Exception as exc:
        log.exception("✗ %s %s unexpected error", method, url)
        st.error(f"Error: {exc}")
        return {}


def render_confidence(score: float):
    pct = int(score * 100)
    color = "#238636" if pct > 70 else "#d29922" if pct > 40 else "#da3633"
    st.markdown(
        f"""
        <div style='margin: 8px 0;'>
            <div style='font-size: 12px; color: #8b949e; margin-bottom: 4px;'>
                Confidence Score: <b style='color:{color}'>{pct}%</b>
            </div>
            <div style='background: #21262d; border-radius: 4px; height: 8px;'>
                <div style='background: {color}; width: {pct}%; height: 8px; border-radius: 4px;'></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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

        st.markdown(
            f"""
            <div class='timeline-event {css_class}'>
                <div style='font-size: 12px; color: #8b949e;'>{event.get('date', '?')} · {event.get('event_type', '').upper()}</div>
                <div style='font-weight: bold; margin: 4px 0;'>{icon} {event.get('title', '')}</div>
                <div style='color: #8b949e; font-size: 14px;'>{event.get('description', '')}</div>
                {'<div style="font-size: 12px; color: #58a6ff; margin-top: 4px;">👥 ' + participants + '</div>' if participants else ''}
            </div>
            """,
            unsafe_allow_html=True,
        )

    if timeline.get("outcome_assessment"):
        st.markdown(
            f"""
            <div class='dna-card' style='border-color: #238636; margin-top: 16px;'>
                <b>🎯 Outcome Assessment</b><br>
                <span style='color: #8b949e;'>{timeline['outcome_assessment']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    render_confidence(timeline.get("confidence_score", 0.0))


def page_header() -> None:
    nav_left, nav_right = st.columns([3, 1])
    with nav_left:
        st.markdown(
            "<div class='topnav-left'><div class='topnav-logo-mark'>DD</div><div class='topnav-logo-word'>DECISIONDNA</div></div>",
            unsafe_allow_html=True,
        )
        page_choice = st.radio(
            "Navigate",
            ["Ask", "Timeline", "Graph", "Ingest", "Health"],
            horizontal=True,
            label_visibility="collapsed",
        )
    with nav_right:
        st.markdown(
            "<div class='topnav-right'><span class='navpill'>Spec-driven</span><span class='navpill navpill-solid'>DecisionDNA</span></div>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<div class='topnav'><div class='topnav-left'></div><div class='topnav-right'><span class='navpill'>Query</span><span class='navpill'>Timeline</span><span class='navpill'>Graph</span></div></div>",
        unsafe_allow_html=True,
    )
    return page_choice


page = page_header()

project_filter = st.selectbox(
    "Filter by Project",
    ["All Projects", "CloudMigration", "DataPlatform", "AuthRefactor", "MobileApp"],
)
project = None if project_filter == "All Projects" else project_filter


if page == "Ask":
    hero_left, hero_right = st.columns([2.1, 1])
    with hero_left:
        st.markdown(
            """
            <div class="hero">
                <div class="eyebrow">Decision intelligence</div>
                <h1>Move beyond search to organizational memory</h1>
                <p>DecisionDNA turns emails, meetings, and tickets into a queryable
                record of why decisions were made. A five-agent pipeline plans the
                question, retrieves evidence, reconstructs the timeline, and writes
                an answer you can trust — with the sources attached.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='chip-strip'><span class='feature-chip'>5-agent pipeline</span><span class='feature-chip'>Project-aware retrieval</span><span class='feature-chip'>Neo4j knowledge graph</span><span class='feature-chip'>Timeline reconstruction</span></div>",
            unsafe_allow_html=True,
        )

    with hero_right:
        st.markdown(
            """
            <div class="hero-notes">
                <div class="notes-label">System notes</div>
                <div class="notes-item">Planner agent routes each question to the right retrieval strategy.</div>
                <div class="notes-item">Timeline agent orders events and flags dissent automatically.</div>
                <div class="notes-item">Confidence score reflects source coverage, not just similarity.</div>
                <div class="notes-item">Degraded mode kicks in if the embeddings provider drops.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    prefill = st.session_state.pop("prefill_question", "")
    st.markdown("<div class='input-shell'>", unsafe_allow_html=True)
    question = st.text_input(
        "Your question",
        value=prefill,
        placeholder="Why did we reject Vendor X in Q1 2024?",
        label_visibility="collapsed",
    )
    b1, b2, b3, b4, b5 = st.columns(5)
    quick_prompts = [
        "Why did we reject Vendor X?",
        "Why did we migrate to Azure Functions?",
        "Security risks in the auth refactor?",
        "Who raised concerns on the DB migration?",
        "Decisions made about JWT tokens?",
    ]
    for col, prompt_text in zip([b1, b2, b3, b4, b5], quick_prompts):
        with col:
            st.markdown("<div class='btn-secondary-row'>", unsafe_allow_html=True)
            if st.button(prompt_text, key=f"quick_{prompt_text}", use_container_width=True):
                st.session_state["prefill_question"] = prompt_text
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
    ask_clicked = st.button("Ask DecisionDNA", type="primary", use_container_width=False)
    st.markdown("</div>", unsafe_allow_html=True)

    if ask_clicked and question:
        with st.spinner("Running the agent pipeline..."):
            result = call_gateway("/api/v1/query", "POST", {"question": question, "project_filter": project})

        if result:
            st.markdown(
                f"""
                <div class='panel' style='margin-top:1rem;'>
                    <div class='section-title'>Answer</div>
                    <div class='section-kicker'>Concise answer produced by the agent chain, with the evidence trail immediately below.</div>
                    <div class='answer-box'>{to_sentence(result.get('answer','No answer generated.'))}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if result.get("degraded"):
                st.warning("Degraded mode: fallback embeddings were used because the primary embeddings provider was unavailable. Retrieval quality and confidence are lower than normal.")
            render_confidence(result.get("confidence_score", 0.0))

            left, right = st.columns([3, 2])
            with left:
                timeline = result.get("timeline")
                if timeline and timeline.get("events"):
                    st.markdown("<div class='section-title' style='margin-top:0.9rem;'>Decision Timeline</div>", unsafe_allow_html=True)
                    st.markdown("<div class='section-kicker'>Chronology of the events the answer depends on.</div>", unsafe_allow_html=True)
                    render_timeline(timeline)
                else:
                    st.markdown("<div class='empty-state'><strong>No timeline returned.</strong> This usually means the topic was too narrow or the timeline service had no supporting evidence.</div>", unsafe_allow_html=True)
            with right:
                render_sources_and_steps(result)
    elif ask_clicked:
        st.warning("Enter a question before running the query.")

    st.markdown(
        """
        <div class="preview-wrap">
            <div class="preview-window">
                <div class="preview-titlebar">
                    <span class="tl-dot" style="background:#ff5f57;"></span>
                    <span class="tl-dot" style="background:#febc2e;"></span>
                    <span class="tl-dot" style="background:#28c840;"></span>
                    <div class="preview-search">⌘K — Ask anything about a past decision…</div>
                </div>
                <div class="preview-body">
                    <div class="preview-col">
                        <div class="preview-col-title">Recent queries</div>
                        <div class="preview-row active">Vendor X rejection</div>
                        <div class="preview-row">Azure Functions migration</div>
                        <div class="preview-row">Auth refactor risks</div>
                        <div class="preview-row">JWT token decisions</div>
                    </div>
                    <div class="preview-col">
                        <div class="preview-col-title">Answer</div>
                        <div class="preview-chat-line">Vendor X was rejected after the security review flagged missing
                        <span class="preview-code">SOC2</span> attestations and a 40% cost overrun versus the RFP baseline.</div>
                        <div class="preview-chat-line">Decision recorded by <span class="preview-code">@ravi.sharma</span> on
                        <span class="preview-code">2024-02-14</span>, confirmed in the vendor review meeting notes.</div>
                    </div>
                    <div class="preview-col">
                        <div class="preview-col-title">Evidence</div>
                        <div class="preview-badge-row">
                            <span class="preview-badge merged">3 sources</span>
                            <span class="preview-badge">Confidence 84%</span>
                        </div>
                        <div class="preview-row">Vendor Review — meeting notes</div>
                        <div class="preview-row">Security Assessment — Jira</div>
                        <div class="preview-row">Procurement thread — email</div>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='spec-section'>", unsafe_allow_html=True)
    spec_left, spec_right = st.columns([1, 1.15])
    with spec_left:
        st.markdown(
            """
            <div class="spec-heading">Bring structure to organizational memory with agent-driven retrieval</div>
            <div class="spec-copy">DecisionDNA turns your question into a
            <u>retrieval plan</u>, a <u>timeline reconstruction</u>, and a
            <u>sourced answer</u>, then runs each stage with a dedicated agent.
            Confidence scoring surfaces the gaps a single vector search would
            miss. With agent-driven retrieval, you get an answer that shows
            its work instead of a black-box summary.</div>
            """,
            unsafe_allow_html=True,
        )

    with spec_right:
        tab_plan, tab_retrieve, tab_synth = st.tabs(["Plan", "Retrieve", "Synthesize"])
        with tab_plan:
            st.markdown(
                """
                <div class="spec-card">
                    <div class="spec-card-head">
                        <span class="section-title" style="font-size:1rem;">Planner agent</span>
                        <span class="timeline-tag">Run pipeline</span>
                    </div>
                    <div class="task-tree">
                        <div class="task-l1"><span class="task-circle"></span> 1. Parse question and detect intent</div>
                        <div class="task-l2"><span class="task-circle"></span> 1.1 Classify query type</div>
                        <div class="task-leaf">Extract entities: <span class="task-code">project</span>, <span class="task-code">date_range</span>, <span class="task-code">keywords</span></div>
                        <div class="task-leaf">Route to <span class="task-code">planner_agent</span></div>
                        <div class="task-l1"><span class="task-circle"></span> 2. Build retrieval strategy</div>
                        <div class="task-l2"><span class="task-circle"></span> 2.1 Select search agent</div>
                        <div class="task-leaf">Apply project filter if present</div>
                        <div class="task-leaf">Rank by <span class="task-code">relevance_score</span></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with tab_retrieve:
            st.markdown(
                """
                <div class="spec-card">
                    <div class="spec-card-head">
                        <span class="section-title" style="font-size:1rem;">Search + timeline agents</span>
                        <span class="timeline-tag">Run pipeline</span>
                    </div>
                    <div class="task-tree">
                        <div class="task-l1"><span class="task-circle"></span> 3. Retrieve supporting documents</div>
                        <div class="task-l2"><span class="task-circle"></span> 3.1 Query vector store</div>
                        <div class="task-leaf">Embed question with <span class="task-code">text-embedding-3</span></div>
                        <div class="task-leaf">Fetch top-k chunks per source type</div>
                        <div class="task-l1"><span class="task-circle"></span> 4. Reconstruct timeline</div>
                        <div class="task-l2"><span class="task-circle"></span> 4.1 Order events by date</div>
                        <div class="task-leaf">Flag <span class="task-code">is_critical</span> and dissent events</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with tab_synth:
            st.markdown(
                """
                <div class="spec-card">
                    <div class="spec-card-head">
                        <span class="section-title" style="font-size:1rem;">Decision + answer agents</span>
                        <span class="timeline-tag">Run pipeline</span>
                    </div>
                    <div class="task-tree">
                        <div class="task-l1"><span class="task-circle"></span> 5. Assess outcome</div>
                        <div class="task-l2"><span class="task-circle"></span> 5.1 Score confidence</div>
                        <div class="task-leaf">Weight by source coverage and recency</div>
                        <div class="task-l1"><span class="task-circle"></span> 6. Write final answer</div>
                        <div class="task-l2"><span class="task-circle"></span> 6.1 Cite sources inline</div>
                        <div class="task-leaf">Fallback to <span class="task-code">degraded</span> mode if embeddings fail</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)

elif page == "Timeline":
    st.markdown(
        """
        <div class="hero" style="padding-top:1.6rem;">
            <div class="eyebrow">Chronology view</div>
            <h1 style="font-size:clamp(2rem,3.2vw,3rem); max-width:24ch;">A chronological view of a decision's evidence trail</h1>
            <p>Enter a topic and DecisionDNA orders every related event, flags critical
            moments and dissent, and estimates how confident it is in the outcome.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_stat_grid([
        {"label": "Input", "value": "Topic + project", "note": "The query is focused before the timeline request fires."},
        {"label": "Output", "value": "Event stream", "note": "Decision, issue, and implementation events arrive in order."},
        {"label": "Assessment", "value": "Outcome + confidence", "note": "The service estimates how the story resolves."},
    ])

    st.markdown("<div class='input-shell'>", unsafe_allow_html=True)
    topic = st.text_input("Topic to explore", placeholder="Azure migration, Vendor X, Auth refactor...", label_visibility="collapsed")
    build_clicked = st.button("Build Timeline", type="primary")
    st.markdown("</div>", unsafe_allow_html=True)

    if build_clicked and topic:
        with st.spinner("Building timeline..."):
            params = f"?topic={topic}" + (f"&project={project}" if project else "")
            result = call_gateway(f"/api/v1/timeline/{topic}{params}")
        if result:
            render_timeline(result)
    elif build_clicked:
        st.warning("Enter a topic first.")

elif page == "Graph":
    st.markdown(
        """
        <div class="hero" style="padding-top:1.6rem;">
            <div class="eyebrow">Relationship map</div>
            <h1 style="font-size:clamp(2rem,3.2vw,3rem); max-width:24ch;">Explore how decisions, people, and projects connect</h1>
            <p>Recent decisions on one side, entity exploration on the other — backed
            directly by the Neo4j graph.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns(2)
    with left:
        st.markdown("<div class='panel'><div class='section-title'>Recent Decisions</div><div class='section-kicker'>Newest items surfaced from Neo4j.</div>", unsafe_allow_html=True)
        result = call_gateway(f"/api/v1/graph/decisions" + (f"?project={project}" if project else ""))
        decisions = result.get("decisions", [])
        if decisions:
            for decision in decisions[:10]:
                st.markdown(
                    f"""
                    <div class='dna-card' style='padding:1rem; margin-bottom:0.7rem;'>
                        <div class='timeline-meta'>{escape_text(str(decision.get('date',''))[:10])} · {escape_text(decision.get('project',''))}</div>
                        <div style='margin-top:0.4rem; color:#fff; line-height:1.65;'>{to_sentence(str(decision.get('description',''))[:260])}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.markdown("<div class='empty-state'><strong>No decisions yet.</strong> Ingest data to populate the graph.</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown("<div class='panel'><div class='section-title'>Explore Entity</div><div class='section-kicker'>Look up a person, team, or project to inspect relationships.</div>", unsafe_allow_html=True)
        entity = st.text_input("Person or Project name", placeholder="Ravi Sharma", label_visibility="collapsed")
        explore_clicked = st.button("Explore Entity")
        if explore_clicked and entity:
            result = call_gateway(f"/api/v1/graph/entities/{entity}")
            connections = result.get("connections", [])
            if connections:
                for connection in connections[:10]:
                    label = connection.get("labels", ["?"])[0]
                    st.markdown(
                        f"""
                        <div class='dna-card' style='padding:0.9rem; margin-bottom:0.7rem;'>
                            <span style='color:#8f7bff;'>{connection.get('rel','?')}</span> →
                            {escape_text(label)}: {to_sentence(str(connection.get('m',''))[:100])}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
            else:
                st.markdown("<div class='empty-state'><strong>No connections found.</strong></div>", unsafe_allow_html=True)
        elif explore_clicked:
            st.warning("Enter an entity name first.")
        st.markdown("</div>", unsafe_allow_html=True)

elif page == "Ingest":
    st.markdown(
        """
        <div class="hero" style="padding-top:1.6rem;">
            <div class="eyebrow">Ingestion lane</div>
            <h1 style="font-size:clamp(2rem,3.2vw,3rem); max-width:24ch;">Load synthetic or real data into the stack</h1>
            <p>Place JSON files in the expected folders, then trigger ingestion,
            embedding, and graph population from a single job.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class='panel'>
            <div class='section-title'>Data sources</div>
            <div class='section-kicker'>Place JSON files in the expected synthetic directories before triggering ingestion.</div>
            <div class='metric-row'>
                <div class='metric-item'><div class='label'>Emails</div><div class='value'>data/synthetic/emails/</div><div class='sub'>Conversation exports and decision threads.</div></div>
                <div class='metric-item'><div class='label'>Meetings</div><div class='value'>data/synthetic/meetings/</div><div class='sub'>Notes, summaries, and follow-up items.</div></div>
            </div>
            <div style='height:0.75rem;'></div>
            <div class='metric-row'>
                <div class='metric-item'><div class='label'>Jira</div><div class='value'>data/synthetic/jira/</div><div class='sub'>Tickets, risks, and implementation signals.</div></div>
                <div class='metric-item'><div class='label'>Pipeline</div><div class='value'>Ingest → embed → graph</div><div class='sub'>Each stage can be triggered from the gateway.</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.button("Start Ingestion", type="primary"):
        with st.spinner("Ingesting..."):
            result = call_gateway(
                "/api/v1/ingest",
                "POST",
                {"data_dir": "/app/data/synthetic", "trigger_embedding": True, "trigger_graph": True},
            )

        if result:
            job_id = result.get("job_id", "?")
            st.success(f"Job started: {job_id}")
            st.info("Poll /api/v1/ingest/status/{job_id} for progress or refresh this page.")

elif page == "Health":
    st.markdown(
        """
        <div class="hero" style="padding-top:1.6rem;">
            <div class="eyebrow">Operational status</div>
            <h1 style="font-size:clamp(2rem,3.2vw,3rem); max-width:24ch;">A compact view of the gateway and every downstream service</h1>
            <p>Quick to scan, clear on state — not dense telemetry.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    result = call_gateway("/health")
    if result:
        services = result.get("services", {})
        gateway_status = result.get("gateway", "unknown")
        render_stat_grid([
            {"label": "API Gateway", "value": gateway_status.upper(), "note": f"Last checked: {escape_text(result.get('timestamp',''))}"},
            {"label": "Services", "value": str(len(services)), "note": "Healthy and degraded service states are listed below."},
            {"label": "Mode", "value": result.get("mode", "live"), "note": "The dashboard reads live service status from the gateway."},
        ])

        st.markdown("<div style='height:0.8rem;'></div>", unsafe_allow_html=True)
        cols = st.columns(min(max(len(services), 1), 4))
        for column, (name, status) in zip(cols, services.items()):
            with column:
                healthy = status == "healthy"
                color = "var(--green)" if healthy else "var(--red)"
                st.markdown(
                    f"""
                    <div class='stat-card'>
                        <div class='stat-label'>{escape_text(name)}</div>
                        <div class='stat-value' style='color:{color};'>{escape_text(status).upper()}</div>
                        <div class='stat-note'>Service status observed through the gateway health probe.</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
    else:
        st.markdown("<div class='empty-state'><strong>No health payload.</strong> Check whether the gateway is reachable.</div>", unsafe_allow_html=True)
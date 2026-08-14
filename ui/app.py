"""
InsightPilot — Autonomous Data Intelligence
Streamlit UI (presentation layer only).

This module renders the interface. It reads existing agent state and calls
existing backend nodes; it defines no analysis behavior of its own.
"""

import streamlit as st
import pandas as pd
import os
import sys
import time
import json
import re
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph import analyst_graph
from agent.state import AgentState, AnalysisStatus
from agent.memory.conversation import handle_followup_node
from agent.token_monitor import monitor as token_monitor
from agent.context_manager import measure_context, COMPRESSION_THRESHOLD

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="InsightPilot",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── Icons (inline SVG, stroke-based, inherit currentColor) ─────────────────────
_ICONS = {
    "logo": '<path d="M12 2l2.2 6.3L20.5 10l-6.3 2.2L12 18.5l-2.2-6.3L3.5 10l6.3-1.7z"/>',
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "insight": '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.5.4.8 1 .8 1.6v.5h5.4v-.5c0-.6.3-1.2.8-1.6A6 6 0 0 0 12 3z"/>',
    "quality": '<path d="M12 3l7.5 3v5.2c0 4.3-3 8.3-7.5 9.6-4.5-1.3-7.5-5.3-7.5-9.6V6z"/><path d="M9.2 12.1l1.9 1.9 3.7-3.9"/>',
    "activity": '<path d="M3 12h3.5l2.5-6 3.5 12 2.5-6H21"/>',
    "log": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8.5 8h7M8.5 12h7M8.5 16h4"/>',
    "chat": '<path d="M20 15a2.5 2.5 0 0 1-2.5 2.5H8L4 21V6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5z"/>',
    "download": '<path d="M12 3v11"/><path d="M8 11l4 4 4-4"/><path d="M4 18v1.5A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5V18"/>',
    "rows": '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "columns": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 4v16M3 9h18"/>',
    "tasks": '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.3l2.4 2.4 4.6-5"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "upload": '<path d="M12 20V9"/><path d="M8 12l4-4 4 4"/><path d="M4 20h16"/>',
    "check": '<path d="M4.5 12.5l5 5 10-11"/>',
    "alert": '<path d="M12 4.5L2.8 20h18.4z"/><path d="M12 10v4.2M12 17.2v.1"/>',
    "retry": '<path d="M20 11a8 8 0 1 0-1.9 6.1"/><path d="M20 20v-5h-5"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "play": '<path d="M7 4.5l12 7.5-12 7.5z"/>',
    "context": '<path d="M12 3a4.5 4.5 0 0 1 4.5 4.5c0 .9 .4 1.7 1 2.3A4.5 4.5 0 0 1 14.5 18h-5A4.5 4.5 0 0 1 6.5 9.8c.6-.6 1-1.4 1-2.3A4.5 4.5 0 0 1 12 3z"/>',
    "token": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v9M9.5 10h5M9.5 14h5"/>',
    "trend": '<path d="M4 16l5-5 3.5 3.5L20 7"/><path d="M15 7h5v5"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "code": '<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>',
    "spark": '<path d="M12 4l1.6 4.6L18 10l-4.4 1.4L12 16l-1.6-4.6L6 10l4.4-1.4z"/>',
}


def icon(name: str, size: int = 16, cls: str = "") -> str:
    """Return an inline SVG string for the given icon name."""
    path = _ICONS.get(name, _ICONS["overview"])
    return (
        f'<svg class="ip-icon {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round">{path}</svg>'
    )


# ── Theme tokens ───────────────────────────────────────────────────────────────
THEMES = {
    "light": {
        "bg": "#F6F7F9", "surface": "#FFFFFF", "surface_alt": "#FAFBFC",
        "border": "#E3E6EB", "border_strong": "#D3D8E0",
        "text": "#131722", "text_muted": "#697386", "text_faint": "#98A1B3",
        "accent": "#5B5BD6", "accent_soft": "#EEEEFB", "accent_text": "#4B4ACF",
        "success": "#16855C", "success_soft": "#E6F5EE",
        "warn": "#9A6A00", "warn_soft": "#FCF3E0",
        "danger": "#C0392F", "danger_soft": "#FCECEA",
        "shadow": "0 1px 2px rgba(19,23,34,.05), 0 1px 3px rgba(19,23,34,.04)",
    },
    "dark": {
        "bg": "#0A0C13", "surface": "#12151F", "surface_alt": "#171A26",
        "border": "#242838", "border_strong": "#323852",
        "text": "#E7E9F2", "text_muted": "#98A0B8", "text_faint": "#6B7391",
        "accent": "#6E6EF7", "accent_soft": "#1D1E3A", "accent_text": "#A5A5FB",
        "success": "#3DD68C", "success_soft": "#10261E",
        "warn": "#E5B455", "warn_soft": "#2A2113",
        "danger": "#F1776A", "danger_soft": "#2C1614",
        "shadow": "0 1px 2px rgba(0,0,0,.4)",
    },
}


FULLSCREEN_LIGHT_CSS = """
/* No container styling — only the icon colour is corrected for Light mode,
   so no white box appears behind it. Position, size and behaviour untouched. */
[data-testid="stElementToolbar"] {
  background: transparent !important;
  background-color: transparent !important;
  border: none !important;
  box-shadow: none !important;
}
[data-testid="stElementToolbar"] button,
[data-testid="stElementToolbarButton"] button {
  background: transparent !important;
  background-color: transparent !important;
  color: #131722 !important;
  border-color: transparent !important;
}
[data-testid="stElementToolbar"] * {
  background: transparent !important;
  background-color: transparent !important;
}
[data-testid="stElementToolbar"] svg { fill: #131722 !important; color: #131722 !important; }

/* Text input focus: Streamlit paints stTextInputRootElement with its primary
   (red). In Light mode use a neutral grey instead. Dark mode is untouched. */
[data-testid="stTextInputRootElement"],
[data-testid="stTextInputRootElement"]:focus-within {
  border-color: #C7CCD6 !important;
  box-shadow: none !important;
  outline: none !important;
}
[data-testid="stTextInputRootElement"]:focus-within {
  border-color: #8A93A3 !important;
}
"""


def inject_css(theme_name: str) -> None:
    t = THEMES[theme_name]
    # Dark mode keeps Streamlit's own fullscreen control appearance untouched.
    fs_light = FULLSCREEN_LIGHT_CSS if theme_name == "light" else ""
    scheme = "dark" if theme_name == "dark" else "light"
    st.markdown(
        f"""
<style>
:root {{
  --bg: {t['bg']};
  --surface: {t['surface']};
  --surface-alt: {t['surface_alt']};
  --border: {t['border']};
  --border-strong: {t['border_strong']};
  --text: {t['text']};
  --muted: {t['text_muted']};
  --faint: {t['text_faint']};
  --accent: {t['accent']};
  --accent-soft: {t['accent_soft']};
  --accent-text: {t['accent_text']};
  --success: {t['success']};
  --success-soft: {t['success_soft']};
  --warn: {t['warn']};
  --warn-soft: {t['warn_soft']};
  --danger: {t['danger']};
  --danger-soft: {t['danger_soft']};
  --shadow: {t['shadow']};
  --r: 10px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto,
          "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}}

/* Base surfaces.
   We also drive Streamlit's OWN theme tokens. Without this, when the user's
   Streamlit runs its dark base theme, native widgets (expander headers, the
   uploader, dataframes) keep Streamlit's dark colors while our shell is light —
   producing dark bars with unreadable text. */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"],
[data-testid="stSidebarContent"], [data-testid="stMain"] {{
  --background-color: {t['surface']};
  --secondary-background-color: {t['surface_alt']};
  --text-color: {t['text']};
  --primary-color: {t['accent']};
  --border-color: {t['border']};
}}
.stApp, [data-testid="stAppViewContainer"] {{ background: var(--bg); }}
:root {{ color-scheme: {scheme}; }}

/* Toggle + slider render in Streamlit's primary (red by default).
   These widgets use emotion-hashed class names, so target them structurally
   from their stable data-testid ancestors. Verified against the live DOM:
     toggle  DIV < LABEL < [stCheckbox]
     slider  DIV < DIV < (group) < [stSlider]  */
[data-testid="stCheckbox"] label > div:nth-child(2) {{
  background: var(--accent) !important;
}}
/* The filled portion of the slider track is a linear-gradient using
   Streamlit's primary. Recolour the red stop to our accent; the track's
   grey remainder is preserved by the gradient itself. */
[data-testid="stSlider"] div[role="group"] > div > div:first-child {{
  filter: hue-rotate(238deg) saturate(.92) !important;
}}
[data-testid="stSlider"] div[role="group"] > div > div:nth-child(2) {{
  background: var(--accent) !important;
}}
[data-testid="stSlider"] div[role="slider"] {{
  background: var(--accent) !important; border-color: var(--accent) !important;
}}
[data-testid="stSliderThumbValue"],
[data-testid="stSliderThumbValue"] * {{ color: var(--accent-text) !important; }}
[data-testid="stSliderTickBar"] * {{ color: var(--faint) !important; }}

/* Themed preview table (replaces canvas dataframe, which ignores CSS) */
.ip-table-wrap {{
  border:1px solid var(--border); border-radius:var(--r); overflow-x:auto;
  background:var(--surface); box-shadow:var(--shadow);
}}
.ip-table {{ border-collapse:collapse; width:100%; font-size:.79rem; }}
.ip-table th {{
  text-align:left; font-weight:600; color:var(--muted); background:var(--surface-alt);
  padding:.55rem .7rem; border-bottom:1px solid var(--border); white-space:nowrap;
}}
.ip-table td {{
  padding:.5rem .7rem; border-bottom:1px solid var(--border); color:var(--text);
  white-space:nowrap; font-variant-numeric:tabular-nums;
}}
.ip-table tr:last-child td {{ border-bottom:none; }}
.ip-table tbody tr:hover td {{ background:var(--surface-alt); }}

/* Native widget text must never inherit the base theme's colour */
[data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary div:not(.ip-tag),
[data-testid="stDataFrame"] *, [data-testid="stAlert"] p,
[data-testid="stFileUploaderFile"] * {{ color: var(--text) !important; }}
[data-testid="stFileUploaderDropzone"] span,
[data-testid="stFileUploaderDropzone"] small {{ color: var(--muted) !important; }}
[data-testid="stFileUploaderDropzone"] button {{
  background: var(--surface) !important; color: var(--text) !important;
  border: 1px solid var(--border) !important;
}}
[data-testid="stHeader"] {{ background: transparent; }}
/* Type: applied to text elements only. A universal `*` rule here would
   clobber Streamlit's Material Symbols ligature font and leak icon NAMES
   as literal text (e.g. "arrow_right") into expanders and uploaders. */
[data-testid="stAppViewContainer"] p, [data-testid="stAppViewContainer"] span,
[data-testid="stAppViewContainer"] div, [data-testid="stAppViewContainer"] label,
[data-testid="stAppViewContainer"] li, [data-testid="stAppViewContainer"] input,
[data-testid="stAppViewContainer"] textarea, [data-testid="stAppViewContainer"] button,
[data-testid="stAppViewContainer"] h1, [data-testid="stAppViewContainer"] h2,
[data-testid="stAppViewContainer"] h3, [data-testid="stAppViewContainer"] h4,
[data-testid="stSidebar"] p, [data-testid="stSidebar"] span,
[data-testid="stSidebar"] div, [data-testid="stSidebar"] label,
[data-testid="stSidebar"] button {{ font-family: var(--font); }}

/* Never restyle icon glyphs — they are ligature fonts, not text.
   Selectors are prefixed with the sidebar/main containers so they outrank the
   font rules above; without this, expander chevrons inside the sidebar render
   their ligature NAME ("keyboard_arrow_right") as literal text. */
[data-testid="stIconMaterial"], span[class*="material-symbols"],
[data-testid="stExpanderIcon"], [data-testid="stFileUploaderDropzone"] svg,
.stAlert svg, [data-testid="stExpander"] summary svg,
[data-testid="stSidebar"] [data-testid="stIconMaterial"],
[data-testid="stSidebar"] span[class*="material-symbols"],
[data-testid="stSidebar"] [data-testid="stExpanderIcon"],
[data-testid="stSidebar"] [data-testid="stExpander"] summary span,
[data-testid="stMain"] [data-testid="stIconMaterial"],
[data-testid="stMain"] span[class*="material-symbols"],
[data-testid="stAppViewContainer"] [data-testid="stIconMaterial"],
[data-testid="stAppViewContainer"] span[class*="material-symbols"] {{
  font-family: "Material Symbols Rounded", "Material Symbols Outlined",
               "Material Icons" !important;
}}
/* Our own inline SVG icons must keep inheriting normally */
[data-testid="stSidebar"] svg.ip-icon, [data-testid="stMain"] svg.ip-icon {{
  font-family: inherit !important;
}}
.block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1500px; }}
body, p, span, div, label, li {{ color: var(--text); }}
h1, h2, h3, h4, h5 {{ color: var(--text); letter-spacing: -.015em; }}
hr {{ border-color: var(--border); }}
a {{ color: var(--accent-text); }}
.ip-icon {{ vertical-align: -.15em; flex: none; }}

/* Sidebar */
[data-testid="stSidebar"] {{ background: var(--surface); border-right: 1px solid var(--border); }}
[data-testid="stSidebar"] > div:first-child {{ padding-top: 1.1rem; }}

.ip-brand {{ display:flex; align-items:center; gap:.65rem; padding:0 .25rem .9rem; }}
.ip-brand-mark {{
  width:32px; height:32px; border-radius:9px; display:grid; place-items:center;
  background: var(--accent-soft); color: var(--accent-text); flex:none;
}}
.ip-brand-name {{ font-size:1.05rem; font-weight:650; line-height:1.15; color:var(--text); }}
.ip-brand-sub {{ font-size:.7rem; color:var(--faint); }}

.ip-navlabel {{
  font-size:.66rem; font-weight:650; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); margin:1.1rem .35rem .45rem;
}}

/* Buttons */
.stButton > button, .stDownloadButton > button {{
  border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text);
  font-size: .855rem; font-weight: 500; padding: .46rem .7rem;
  transition: background .12s ease, border-color .12s ease; box-shadow: none;
}}
.stButton > button:hover, .stDownloadButton > button:hover {{
  background: var(--surface-alt); border-color: var(--border-strong); color: var(--text);
}}
.stButton > button[kind="primary"] {{
  background: var(--accent); border-color: var(--accent); color: #fff; font-weight:560;
}}
.stButton > button[kind="primary"]:hover {{ filter: brightness(1.07); color:#fff; }}
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible {{
  outline: 2px solid var(--accent); outline-offset: 2px;
}}
[data-testid="stSidebar"] .stButton > button {{ text-align:left; justify-content:flex-start; }}
[data-testid="stSidebar"] .stButton > button:disabled {{ opacity:.42; }}

/* Cards */
.ip-card {{
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
  padding: 1rem 1.1rem; box-shadow: var(--shadow);
}}
.ip-card + .ip-card {{ margin-top: 1.1rem; }}
/* Right rail: consistent 18px rhythm between stacked cards */
.ip-card--rail {{ margin-bottom: 18px; }}
/* Top offset is applied per-view (Overview only), so Agent Activity's
   right column aligns with the top of Run timeline. */
.ip-card-head {{
  display:flex; align-items:center; justify-content:space-between; gap:.6rem; margin-bottom:.7rem;
}}
.ip-card-title {{ font-size:.95rem; font-weight:620; color:var(--text); }}

/* KPI */
.ip-kpi {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--r);
  padding:.95rem 1rem; display:flex; gap:.8rem; align-items:center;
  box-shadow:var(--shadow); height:100%;
}}
.ip-kpi-icon {{
  width:38px; height:38px; border-radius:9px; display:grid; place-items:center; flex:none;
  background:var(--accent-soft); color:var(--accent-text);
}}
.ip-kpi-icon.tone-success {{ background:var(--success-soft); color:var(--success); }}
.ip-kpi-icon.tone-warn {{ background:var(--warn-soft); color:var(--warn); }}
.ip-kpi-label {{ font-size:.7rem; color:var(--muted); font-weight:550; }}
.ip-kpi-value {{ font-size:1.55rem; font-weight:660; line-height:1.15; color:var(--text); font-variant-numeric:tabular-nums; }}
.ip-kpi-sub {{ font-size:.7rem; color:var(--faint); }}
.ip-kpi-sub.ok {{ color:var(--success); font-weight:560; }}

/* Header */
.ip-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; margin-bottom:1.1rem; }}
.ip-h1 {{ font-size:1.45rem; font-weight:660; line-height:1.2; margin:0; color:var(--text); }}
.ip-sub {{ font-size:.83rem; color:var(--muted); margin-top:.18rem; }}
.ip-pill {{
  display:inline-flex; align-items:center; gap:.4rem; font-size:.74rem; font-weight:550;
  padding:.32rem .7rem; border-radius:999px; border:1px solid var(--border);
  background:var(--surface); color:var(--muted); white-space:nowrap;
}}
.ip-dot {{ width:7px; height:7px; border-radius:50%; background:var(--success); flex:none; }}
.ip-dot.idle {{ background:var(--faint); }}
.ip-dot.warn {{ background:var(--warn); }}

.ip-eyebrow {{
  font-size:.68rem; font-weight:650; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); margin:.2rem 0 .55rem;
}}
.ip-tag {{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.68rem; font-weight:560;
  padding:.16rem .5rem; border-radius:6px; background:var(--accent-soft);
  color:var(--accent-text); border:1px solid transparent;
}}
.ip-tag.neutral {{ background:var(--surface-alt); color:var(--muted); border-color:var(--border); }}
.ip-tag.success {{ background:var(--success-soft); color:var(--success); }}
.ip-tag.warn {{ background:var(--warn-soft); color:var(--warn); }}
.ip-tag.danger {{ background:var(--danger-soft); color:var(--danger); }}

/* Insight card */
.ip-insight {{
  background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--accent);
  border-radius:var(--r); padding:.95rem 1.05rem; margin-bottom:.7rem; box-shadow:var(--shadow);
}}
.ip-insight-body {{ font-size:.875rem; line-height:1.62; color:var(--text); }}
.ip-insight-body strong {{ font-weight:640; }}
/* Agent insights lead with a bold title — promote it to its own line */
.ip-insight-body > strong:first-child {{
  display:block; color:var(--accent-text); font-size:.83rem; font-weight:620;
  letter-spacing:.005em; margin-bottom:.35rem;
}}
.ip-insight-body code {{
  font-family:var(--mono); font-size:.8em; background:var(--surface-alt);
  padding:.05rem .3rem; border-radius:4px;
}}
.ip-insight {{ padding:1rem 1.15rem; }}
.ip-note {{ font-size:.74rem; color:var(--faint); margin-top:.5rem; line-height:1.45; }}

/* Dataset card */
.ip-ds {{
  background:var(--surface-alt); border:1px solid var(--border); border-radius:var(--r);
  padding:.7rem .8rem; display:flex; gap:.6rem; align-items:flex-start;
}}
.ip-ds-icon {{ color:var(--muted); margin-top:.1rem; }}
.ip-ds-name {{ font-size:.82rem; font-weight:600; color:var(--text); word-break:break-all; }}
.ip-ds-meta {{ font-size:.7rem; color:var(--faint); margin-top:.1rem; }}

/* Timeline */
.ip-tl {{ position:relative; padding-left:1.35rem; }}
.ip-tl::before {{
  content:""; position:absolute; left:.46rem; top:.5rem; bottom:.5rem; width:1px; background:var(--border);
}}
.ip-tl-item {{ position:relative; padding:.6rem 0; }}
.ip-tl-item + .ip-tl-item {{ border-top:1px solid var(--border); }}
.ip-tl-node {{
  position:absolute; left:-1.11rem; top:.62rem; width:11px; height:11px; border-radius:50%;
  background:var(--surface); border:2px solid var(--border-strong);
}}
.ip-tl-node.done {{ border-color:var(--success); background:var(--success); }}
.ip-tl-node.warn {{ border-color:var(--warn); background:var(--warn); }}
.ip-tl-node.active {{ border-color:var(--accent); background:var(--accent); }}
.ip-tl-row {{ display:flex; align-items:baseline; justify-content:space-between; gap:.5rem; }}
.ip-tl-title {{ font-size:.815rem; font-weight:620; color:var(--text); }}
.ip-tl-time {{ font-size:.68rem; color:var(--faint); font-family:var(--mono); white-space:nowrap; }}
.ip-tl-detail {{
  font-size:.735rem; color:var(--muted); margin-top:.2rem; line-height:1.45;
  overflow-wrap:anywhere;
}}

/* Log rows */
.ip-log {{ display:flex; gap:.6rem; padding:.5rem .1rem; border-bottom:1px solid var(--border); }}
.ip-log:last-child {{ border-bottom:none; }}
.ip-log-ico {{ margin-top:.12rem; flex:none; }}
.ip-log-ico.ok {{ color:var(--success); }}
.ip-log-ico.err {{ color:var(--danger); }}
.ip-log-ico.warn {{ color:var(--warn); }}
.ip-log-ico.info {{ color:var(--faint); }}
.ip-log-time {{ font-family:var(--mono); font-size:.7rem; color:var(--faint); white-space:nowrap; }}
.ip-log-node {{ font-size:.74rem; font-weight:600; color:var(--accent-text); }}
.ip-log-action {{ font-size:.8rem; color:var(--text); }}
.ip-log-detail {{ font-size:.73rem; color:var(--muted); margin-top:.12rem; }}

/* Chat */
.ip-msg {{ display:flex; margin-bottom:.55rem; }}
.ip-msg.user {{ justify-content:flex-end; }}
.ip-bubble {{ max-width:88%; padding:.6rem .8rem; border-radius:12px; font-size:.84rem; line-height:1.5; }}
.ip-bubble.user {{ background:var(--accent); color:#fff; border-bottom-right-radius:4px; }}
.ip-bubble.agent {{
  background:var(--surface-alt); color:var(--text);
  border:1px solid var(--border); border-bottom-left-radius:4px;
}}

/* Meters */
.ip-meter {{ height:6px; border-radius:999px; background:var(--surface-alt); border:1px solid var(--border); overflow:hidden; }}
.ip-meter > span {{ display:block; height:100%; background:var(--accent); }}
.ip-meter.warn > span {{ background:var(--warn); }}
.ip-stat {{ display:flex; justify-content:space-between; font-size:.75rem; padding:.22rem 0; gap:.6rem; }}
.ip-stat b {{ font-weight:600; font-variant-numeric:tabular-nums; color:var(--text); }}
.ip-stat span {{ color:var(--muted); }}

/* Chart frame — generated PNGs stay legible in both themes */
[data-testid="stImage"] {{
  background:#FFFFFF; border:1px solid var(--border); border-radius:var(--r); padding:.55rem;
}}
[data-testid="stImage"] img {{ border-radius:4px; }}
[data-testid="stImageCaption"] {{ color:var(--muted); font-size:.72rem; }}

/* Widget polish */
[data-testid="stMetricValue"] {{ font-size:1.35rem; color:var(--text); }}
[data-testid="stMetricLabel"] {{ color:var(--muted); }}
/* Expander — every layer gets an explicit surface so the Streamlit base
   theme (which follows the OS preference) cannot show through. */
[data-testid="stExpander"] {{
  border:1px solid var(--border); border-radius:var(--r);
  background:var(--surface); box-shadow:var(--shadow); overflow:hidden;
}}
[data-testid="stExpander"] details,
[data-testid="stExpander"] details > div {{ background:var(--surface) !important; }}
[data-testid="stExpander"] summary {{
  background:var(--surface) !important; color:var(--text) !important;
  font-size:.87rem; font-weight:560; padding:.6rem .9rem; border-radius:0;
}}
[data-testid="stExpander"] summary:hover {{
  background:var(--surface-alt) !important; color:var(--accent-text) !important;
}}
[data-testid="stExpander"] summary p {{ color:inherit !important; font-weight:560; }}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {{ background:var(--surface); }}

/* File uploader */
[data-testid="stFileUploaderDropzone"] {{
  background:var(--surface-alt) !important; border:1px dashed var(--border-strong);
  border-radius:var(--r);
}}
[data-testid="stFileUploaderDropzone"] * {{ color:var(--muted) !important; }}
[data-testid="stFileUploaderDropzone"] button {{
  background:var(--surface) !important; border:1px solid var(--border) !important;
  color:var(--text) !important;
}}
[data-testid="stFileUploaderDropzoneInstructions"] span {{ color:var(--muted) !important; }}

/* Other native surfaces */
[data-testid="stForm"], [data-testid="stVerticalBlockBorderWrapper"] {{
  background:var(--surface); border-color:var(--border);
}}
[data-testid="stToolbar"] {{ background:transparent; }}
[data-testid="stSpinner"] > div {{ color:var(--muted); }}
[data-testid="stAlert"] p {{ color:var(--text); }}
[data-testid="stSliderTickBarMin"], [data-testid="stSliderTickBarMax"] {{ color:var(--faint); }}
.stTextInput input, .stTextArea textarea {{
  background:var(--surface); border:1px solid var(--border); border-radius:8px;
  color:var(--text); font-size:.85rem;
}}
/* Focus: Streamlit/BaseWeb draws a red ring on the wrapper and we previously
   added an accent halo — both are removed. Focus is shown as a single accent
   border. Applies to every text input in the app. */
.stTextInput div[data-baseweb="input"],
.stTextInput div[data-baseweb="base-input"],
.stTextArea div[data-baseweb="textarea"] {{
  border: 1px solid var(--border) !important;
  box-shadow: none !important; outline: none !important;
}}
.stTextInput div[data-baseweb="input"]:focus-within,
.stTextInput div[data-baseweb="base-input"]:focus-within,
.stTextArea div[data-baseweb="textarea"]:focus-within {{
  border-color: var(--accent) !important;
  box-shadow: none !important; outline: none !important;
}}
.stTextInput input, .stTextArea textarea {{ box-shadow: none !important; }}
.stTextInput input:focus, .stTextInput input:focus-visible,
.stTextArea textarea:focus, .stTextArea textarea:focus-visible {{
  outline: none !important; box-shadow: none !important;
  border: none !important; caret-color: var(--accent);
}}

/* Fullscreen control — APPEARANCE ONLY.
   Streamlit owns the button, its DOM, positioning and events; none of that is
   touched here. Dark mode already rendered correctly, so it gets no overrides
   at all. Light mode only recolours the control's surface and icon, because
   Streamlit paints it from its own (dark) base theme.
   No opacity / position / z-index / overflow / pointer-events changes. */
{fs_light}
/* Tooltip: dark surface with light text — the conventional, readable pairing,
   and the appearance already confirmed correct in Dark mode. */
div[data-baseweb="tooltip"], div[data-baseweb="tooltip"] > div, [role="tooltip"] {{
  background: #1B1F2B !important;
  color: #FFFFFF !important;
  border-radius: 6px !important;
  font-size: .74rem !important;
}}
div[data-baseweb="tooltip"] *, [role="tooltip"] * {{ color: #FFFFFF !important; }}

/* Equal-height cards inside column rows, and uniform chart spacing */
.ip-card--eq {{ height: 100%; }}
[data-testid="stImage"] {{ margin-bottom: .9rem; }}
[data-testid="stDataFrame"] {{ border:1px solid var(--border); border-radius:var(--r); }}
[data-testid="stProgress"] > div > div > div {{ background:var(--accent); }}
[data-testid="stAlert"] {{ border-radius:var(--r); border:1px solid var(--border); }}
.stSlider label, .stToggle label {{ color:var(--muted); font-size:.8rem; }}
[data-testid="stCaptionContainer"] {{ color:var(--faint); }}
::-webkit-scrollbar {{ width:9px; height:9px; }}
::-webkit-scrollbar-thumb {{ background:var(--border-strong); border-radius:9px; }}
::-webkit-scrollbar-track {{ background:transparent; }}

@media (max-width: 1100px) {{
  .ip-head {{ flex-direction:column; align-items:flex-start; }}
  .ip-kpi-value {{ font-size:1.3rem; }}
  .block-container {{ padding-left:1rem; padding-right:1rem; }}
}}
</style>
""",
        unsafe_allow_html=True,
    )


# ── Session state ──────────────────────────────────────────────────────────────
def init_session():
    defaults = {
        "agent_state": None,
        "analysis_complete": False,
        "awaiting_approval": False,
        "uploaded_path": None,
        "dataset_name": "",
        "chat_input": "",
        "run_count": 0,
        "theme": "light",
        "nav": "Overview",
        "confirm_reset": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_session()
inject_css(st.session_state.theme)


# ── Render helpers ─────────────────────────────────────────────────────────────
def kpi(label, value, sub="", ico="rows", tone="", sub_ok=False):
    return f"""<div class="ip-kpi">
  <div class="ip-kpi-icon {tone}">{icon(ico, 19)}</div>
  <div>
    <div class="ip-kpi-label">{label}</div>
    <div class="ip-kpi-value">{value}</div>
    <div class="ip-kpi-sub {'ok' if sub_ok else ''}">{sub}</div>
  </div>
</div>"""


def page_head(title, subtitle, status_text, status_tone="idle"):
    st.markdown(
        f"""<div class="ip-head">
  <div>
    <div class="ip-h1">{title}</div>
    <div class="ip-sub">{subtitle}</div>
  </div>
  <div class="ip-pill"><span class="ip-dot {status_tone}"></span>{status_text}</div>
</div>""",
        unsafe_allow_html=True,
    )


def df_preview_html(df, rows: int = 6) -> str:
    """Render a small DataFrame preview as a themed HTML table.

    st.dataframe is canvas-rendered and follows Streamlit's base theme, so it
    ignores our CSS and shows dark in light mode. A plain table stays themed.
    """
    import html as _html
    head = df.head(rows)
    ths = "".join(f"<th>{_html.escape(str(c))}</th>" for c in head.columns)
    trs = []
    for _, r in head.iterrows():
        tds = "".join(f"<td>{_html.escape(str(v))}</td>" for v in r.tolist())
        trs.append(f"<tr>{tds}</tr>")
    return (f'<div class="ip-table-wrap"><table class="ip-table">'
            f"<thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table></div>")


def md_inline(text) -> str:
    """Render the small Markdown subset the agent emits (bold/italic/code) as
    HTML. Everything else is escaped, so raw ** never reaches the user."""
    import html as _h
    s = _h.escape(str(text))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s, flags=re.S)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", s, flags=re.S)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def clean_detail(text) -> str:
    """Strip developer filesystem paths from user-facing text, keeping the
    file name only. Underlying values are untouched."""
    if not text:
        return ""
    return re.sub(r"/(?:[\w .\-]+/)+([\w .\-]+)", r"\1", str(text))


def confidence_band(conf: float):
    """Uses the existing 0.8 / 0.5 thresholds."""
    if conf >= 0.8:
        return "High confidence", "success"
    if conf >= 0.5:
        return "Moderate confidence", "warn"
    return "Low confidence", "danger"


TYPE_ICONS = {
    "descriptive": "chart", "correlation": "activity", "outlier": "alert",
    "trend": "trend", "forecast": "spark", "distribution": "chart",
    "comparative": "columns", "custom": "code",
}

STAGE_MAP = {
    "data_ingestion": ("Profiling dataset", "Loaded and quality-checked the data"),
    "planner": ("Planning analysis", "Generated the analysis plan"),
    "code_generator": ("Generating code", "Wrote analysis code for tasks"),
    "executor": ("Executing tasks", "Ran analysis code in the sandbox"),
    "interpreter": ("Interpreting results", "Produced findings and insights"),
    "reflection": ("Reflecting", "Recorded lessons from this run"),
    "context_manager": ("Managing context", "Compressed older conversation"),
    "report_generator": ("Generating report", "Exported report artifacts"),
    "conversation": ("Follow-up answered", "Responded from stored findings"),
}


def agent_activity_html(state, limit=8):
    """Timeline built strictly from execution_log entries that exist."""
    log = state.get("execution_log", []) if state else []
    if not log:
        return '<div class="ip-note">No agent activity recorded yet.</div>'

    seen = {}
    for e in log:
        node = e.get("node", "")
        if node in STAGE_MAP:
            seen[node] = e  # keep the latest entry per stage

    rows = []
    for node, e in seen.items():
        title, fallback = STAGE_MAP[node]
        status = e.get("status", "")
        cls = "warn" if status in ("warning", "retry") else "done" if status == "success" else "active"
        ts = str(e.get("timestamp", ""))[11:19]
        detail = clean_detail(str(e.get("detail") or fallback))[:96]
        rows.append(
            f'<div class="ip-tl-item"><span class="ip-tl-node {cls}"></span>'
            f'<div class="ip-tl-row"><span class="ip-tl-title">{title}</span>'
            f'<span class="ip-tl-time">{ts}</span></div>'
            f'<div class="ip-tl-detail">{detail}</div></div>'
        )
    return f'<div class="ip-tl">{"".join(rows[:limit])}</div>'


def chat_thread_html(state):
    turns = state.get("conversation_history", []) if state else []
    if not turns:
        return '<div class="ip-note">Ask a question about the analysis to start.</div>'
    out = []
    for t in turns:
        role = "user" if t.get("role") == "user" else "agent"
        out.append(
            f'<div class="ip-msg {role}"><div class="ip-bubble {role}">{t.get("message", "")}</div></div>'
        )
    return "".join(out)


def submit_followup(text, state):
    """Calls the existing follow-up node — backend behavior unchanged."""
    if not text or not text.strip():
        return
    with st.spinner("Consulting stored findings..."):
        updated = handle_followup_node(
            {**state, "current_user_message": text, "is_followup_query": True}
        )
        st.session_state.agent_state = updated
        st.rerun()


# ── Sidebar ────────────────────────────────────────────────────────────────────
DEMO_DATASETS = {
    "Sales Data": "sales_data.csv",
    "Attendance Data": "attendance_data.csv",
    "Student Performance": "student_data.csv",
    "E-commerce Time Series": "ecommerce_timeseries.csv",
}

NAV_ITEMS = ["Overview", "Insights", "Data Quality", "Agent Activity",
             "Execution Log", "Follow-up Chat", "Downloads"]

with st.sidebar:
    st.markdown(
        f"""<div class="ip-brand">
  <div class="ip-brand-mark">{icon('logo', 18)}</div>
  <div>
    <div class="ip-brand-name">InsightPilot</div>
    <div class="ip-brand-sub">Autonomous Data Intelligence</div>
  </div>
</div>""",
        unsafe_allow_html=True,
    )

    ready = st.session_state.analysis_complete
    for label in NAV_ITEMS:
        active = st.session_state.nav == label
        if st.button(
            label,
            key=f"nav_{label}",
            use_container_width=True,
            type="primary" if active else "secondary",
            disabled=(not ready and label != "Overview"),
        ):
            st.session_state.nav = label
            st.rerun()

    st.markdown('<div class="ip-navlabel">Dataset</div>', unsafe_allow_html=True)
    if st.session_state.uploaded_path:
        try:
            _prev = pd.read_csv(st.session_state.uploaded_path)
            _shape = f"{_prev.shape[0]:,} rows &middot; {_prev.shape[1]} columns"
        except Exception:
            _shape = "Preview unavailable"
        _q = (st.session_state.agent_state or {}).get("data_quality_report") or {}
        _qline = (
            f'<span class="ip-tag success">Quality {_q.get("quality_score")}/100</span>'
            if _q.get("quality_score") is not None else ""
        )
        st.markdown(
            f"""<div class="ip-ds">
  <div class="ip-ds-icon">{icon('file', 16)}</div>
  <div>
    <div class="ip-ds-name">{os.path.basename(st.session_state.uploaded_path)}</div>
    <div class="ip-ds-meta">{_shape}</div>
    <div style="margin-top:.35rem">{_qline}</div>
  </div>
</div>""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="ip-note">No dataset selected. Upload a CSV or choose a sample below.</div>',
            unsafe_allow_html=True,
        )

    uploaded = st.file_uploader("Upload dataset", type=["csv"])
    if uploaded:
        save_path = f"/tmp/{uploaded.name}"
        with open(save_path, "wb") as f:
            f.write(uploaded.getbuffer())
        st.session_state.uploaded_path = save_path
        st.session_state.dataset_name = uploaded.name.replace(".csv", "").replace("_", " ").title()

    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    with st.expander("Sample datasets", expanded=not st.session_state.uploaded_path):
        for label, fname in DEMO_DATASETS.items():
            fpath = os.path.join(data_dir, fname)
            if os.path.exists(fpath) and st.button(label, key=f"ds_{fname}",
                                                   use_container_width=True):
                st.session_state.uploaded_path = fpath
                st.session_state.dataset_name = label
                st.session_state.agent_state = None
                st.session_state.analysis_complete = False
                st.session_state.awaiting_approval = False
                st.session_state.nav = "Overview"
                st.rerun()

    st.markdown('<div class="ip-navlabel">Run settings</div>', unsafe_allow_html=True)
    safe_mode = st.toggle("Review plan before running", value=True)
    with st.expander("Advanced settings", expanded=False):
        max_retries = st.slider("Max retries per task", 1, 5, 3)

    # Developer metrics (context, tokens, cost, LLM calls) live in
    # Agent Activity as technical diagnostics — not in the product sidebar.
    with st.expander("Settings", expanded=False):
        st.markdown('<div class="ip-navlabel" style="margin-top:0">Appearance</div>',
                    unsafe_allow_html=True)
        tcol1, tcol2 = st.columns(2)
        with tcol1:
            if st.button("Light", use_container_width=True,
                         type="primary" if st.session_state.theme == "light" else "secondary"):
                st.session_state.theme = "light"
                st.rerun()
        with tcol2:
            if st.button("Dark", use_container_width=True,
                         type="primary" if st.session_state.theme == "dark" else "secondary"):
                st.session_state.theme = "dark"
                st.rerun()

        st.markdown('<div class="ip-navlabel">Session</div>', unsafe_allow_html=True)
        if not st.session_state.get("confirm_reset"):
            if st.button("Reset session", use_container_width=True):
                st.session_state.confirm_reset = True
                st.rerun()
        else:
            st.markdown('<div class="ip-note">This clears the current analysis '
                        'and results. Continue?</div>', unsafe_allow_html=True)
            rc1, rc2 = st.columns(2)
            with rc1:
                if st.button("Confirm", use_container_width=True, type="primary"):
                    for key in ["agent_state", "analysis_complete", "awaiting_approval", "run_count"]:
                        st.session_state[key] = None if key == "agent_state" else False if "complete" in key or "approval" in key else 0
                    st.session_state.confirm_reset = False
                    st.session_state.nav = "Overview"
                    st.rerun()
            with rc2:
                if st.button("Cancel", use_container_width=True):
                    st.session_state.confirm_reset = False
                    st.rerun()


# ── No dataset selected ────────────────────────────────────────────────────────
if not st.session_state.uploaded_path:
    page_head("Dashboard", "Autonomous analysis of your dataset", "Waiting for data", "idle")
    st.markdown(
        f"""<div class="ip-card" style="padding:2rem 1.6rem; text-align:center">
  <div style="color:var(--accent-text); margin-bottom:.6rem">{icon('upload', 26)}</div>
  <div style="font-size:1rem; font-weight:620">Select a dataset to begin</div>
  <div class="ip-note" style="margin-top:.35rem">
    Upload a CSV or pick a sample dataset from the sidebar. InsightPilot profiles the data,
    proposes an analysis plan for your review, then runs it.
  </div>
</div>""",
        unsafe_allow_html=True,
    )
    st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3, gap="medium")
    for col, (ic, title, body) in zip(
        (c1, c2, c3),
        [
            ("insight", "Plans its own analysis",
             "Reads the data profile and proposes the analyses worth running."),
            ("retry", "Corrects its own code",
             "Classifies execution errors and rewrites the code before retrying."),
            ("chat", "Remembers the findings",
             "Answers follow-up questions from the results it produced."),
        ],
    ):
        with col:
            st.markdown(
                f"""<div class="ip-card ip-card--eq">
  <div style="color:var(--accent-text)">{icon(ic, 18)}</div>
  <div style="font-weight:600; font-size:.9rem; margin:.5rem 0 .25rem">{title}</div>
  <div class="ip-note">{body}</div>
</div>""",
                unsafe_allow_html=True,
            )
    st.stop()


# ── Pre-run: dataset preview ───────────────────────────────────────────────────
if not st.session_state.analysis_complete and not st.session_state.awaiting_approval:
    preview = pd.read_csv(st.session_state.uploaded_path)
    page_head("Dashboard", f"{st.session_state.dataset_name} — ready to analyze",
              "Agent ready", "idle")

    _missing = int(preview.isna().sum().sum())
    k1, k2, k3 = st.columns(3)
    k1.markdown(kpi("Rows", f"{preview.shape[0]:,}", "Total records", "rows"), unsafe_allow_html=True)
    k2.markdown(kpi("Columns", f"{preview.shape[1]}", "Total columns", "columns"), unsafe_allow_html=True)
    k3.markdown(kpi("Missing values", f"{_missing:,}", "Across all cells", "alert",
                    tone="tone-warn" if _missing else ""), unsafe_allow_html=True)

    st.markdown('<div class="ip-eyebrow" style="margin-top:1.1rem">Data preview</div>',
                unsafe_allow_html=True)
    st.markdown(df_preview_html(preview, 6), unsafe_allow_html=True)

    if st.button("Run autonomous analysis", type="primary", use_container_width=True):
        initial_state: AgentState = {
            "csv_path": st.session_state.uploaded_path,
            "dataset_name": st.session_state.dataset_name,
            "df_summary": None, "df_dtypes": None, "df_shape": None, "df_sample": None,
            "data_quality_report": None,
            "analysis_plan": [], "plan_approved": False, "user_edits_to_plan": None,
            "current_task_index": 0, "completed_tasks": [], "failed_tasks": [],
            "max_retries": max_retries,
            "chart_paths": [], "execution_log": [],
            "final_report_md": None, "final_report_pdf": None, "notebook_path": None,
            "business_insights": [],
            "conversation_history": [],
            "current_user_message": None, "agent_response": None,
            "safe_mode": safe_mode,
            "is_followup_query": False,
            "error_message": None, "next_action": None
        }

        progress_bar = st.progress(0, text="Initializing agent...")
        status_placeholder = st.empty()

        with st.spinner("Agent is working..."):
            try:
                # Run until await_approval or completion
                for i, step_state in enumerate(analyst_graph.stream(initial_state)):
                    node_name = list(step_state.keys())[0]
                    current = step_state[node_name]

                    progress = min(0.1 + i * 0.05, 0.85)
                    progress_bar.progress(progress, text=f"Running: {node_name}...")

                    last_log = current.get("execution_log", [{}])[-1]
                    status_placeholder.info(f"**{node_name}:** {last_log.get('detail', '...')}")

                    st.session_state.agent_state = current

                    # Pause at approval gate
                    if node_name == "await_approval" and safe_mode:
                        st.session_state.awaiting_approval = True
                        progress_bar.progress(0.45, text="Waiting for your approval...")
                        st.rerun()
                        break

                if not st.session_state.awaiting_approval:
                    progress_bar.progress(1.0, text="Analysis complete!")
                    st.session_state.analysis_complete = True
                    st.rerun()

            except Exception as e:
                st.error(f"Agent error: {str(e)}")
                st.exception(e)


# ── Plan approval ──────────────────────────────────────────────────────────────
elif st.session_state.awaiting_approval:
    state = st.session_state.agent_state
    page_head("Review analysis plan",
              "The agent proposed these analyses. Approve to run, or edit first.",
              "Awaiting approval", "warn")

    q = state.get("data_quality_report") or {}
    if q:
        k1, k2, k3, k4 = st.columns(4)
        k1.markdown(kpi("Rows", f"{q.get('total_rows', 0):,}", "Total records", "rows"),
                    unsafe_allow_html=True)
        k2.markdown(kpi("Columns", f"{q.get('total_columns', 0)}", "Total columns", "columns"),
                    unsafe_allow_html=True)
        k3.markdown(kpi("Duplicates", f"{q.get('duplicate_rows', 0):,}", "Repeated rows", "alert",
                        tone="tone-warn" if q.get("duplicate_rows") else ""), unsafe_allow_html=True)
        k4.markdown(kpi("Quality score", f"{q.get('quality_score', 0)}", "out of 100", "quality",
                        tone="tone-success"), unsafe_allow_html=True)

        recs = q.get("recommendations") or []
        if recs:
            with st.expander(f"Data quality notes ({len(recs)})"):
                for r in recs:
                    st.markdown(f'<div class="ip-note">{r}</div>', unsafe_allow_html=True)

    st.markdown('<div class="ip-eyebrow" style="margin-top:1.2rem">Proposed analyses</div>',
                unsafe_allow_html=True)

    plan = state.get("analysis_plan", [])
    for i, task in enumerate(plan):
        ico = TYPE_ICONS.get(task["analysis_type"], "chart")
        with st.expander(f"{i + 1}.  {task['title']}", expanded=i < 3):
            st.markdown(
                f"""<div style="display:flex; gap:.4rem; margin-bottom:.5rem">
  <span class="ip-tag">{icon(ico, 12)} {task['analysis_type']}</span>
  <span class="ip-tag neutral">Priority {task['priority']}</span>
</div>
<div class="ip-insight-body">{md_inline(task['description'])}</div>""",
                unsafe_allow_html=True,
            )

    user_edits = st.text_area(
        "Edit the plan (optional)",
        placeholder="For example: skip task 3, and add a revenue-by-product analysis",
        height=80,
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        if st.button("Approve and run", type="primary", use_container_width=True):
            state["plan_approved"] = True
            state["user_edits_to_plan"] = user_edits if user_edits.strip() else None
            state["awaiting_approval"] = False

            if user_edits.strip():
                # Replan with edits first
                from agent.nodes.planner import planner_node
                state = planner_node({**state, "user_edits_to_plan": user_edits})

            st.session_state.awaiting_approval = False

            progress_bar = st.progress(0.45, text="Resuming analysis...")
            status_placeholder = st.empty()

            with st.spinner("Executing analysis tasks..."):
                try:
                    # Continue from generate_code
                    from agent.nodes.code_generator import code_generator_node
                    from agent.nodes.executor import executor_node
                    from agent.nodes.interpreter import interpreter_node, advance_task_node
                    from agent.nodes.report_generator import report_generator_node

                    current = state
                    total_tasks = len([t for t in current["analysis_plan"] if t["status"] == "pending"])
                    done_tasks = 0

                    while True:
                        idx = current.get("current_task_index", 0)
                        plan = current.get("analysis_plan", [])

                        if idx >= len(plan):
                            break

                        task = plan[idx]
                        if task["status"] in (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED):
                            current = advance_task_node(current)
                            continue

                        # Generate code
                        status_placeholder.info(f"Generating code: **{task['title']}**")
                        current = code_generator_node(current)

                        # Execute with retry loop
                        while True:
                            status_placeholder.info(
                                f"Executing: **{task['title']}** "
                                f"(attempt {plan[idx].get('retry_count', 0) + 1})")
                            current = executor_node(current)
                            next_a = current.get("next_action")

                            if next_a == "generate_code":
                                current = code_generator_node(current)
                            else:
                                break

                        # Interpret
                        if current.get("next_action") == "interpret":
                            status_placeholder.info(f"Interpreting: **{task['title']}**")
                            current = interpreter_node(current)

                        current = advance_task_node(current)
                        done_tasks += 1
                        progress = 0.45 + (done_tasks / max(total_tasks, 1)) * 0.45
                        progress_bar.progress(min(progress, 0.9),
                                              text=f"Completed {done_tasks}/{total_tasks} analyses")

                    # Generate report
                    status_placeholder.info("Generating final report...")
                    current = report_generator_node(current)
                    progress_bar.progress(1.0, text="Done!")

                    st.session_state.agent_state = current
                    st.session_state.analysis_complete = True
                    st.rerun()

                except Exception as e:
                    st.error(f"Execution error: {str(e)}")
                    st.exception(e)

    with col2:
        if st.button("Discard plan", use_container_width=True):
            st.session_state.agent_state["analysis_plan"] = []
            st.session_state.awaiting_approval = False
            st.rerun()


# ── Results dashboard ──────────────────────────────────────────────────────────
elif st.session_state.analysis_complete:
    state = st.session_state.agent_state
    nav = st.session_state.nav
    q = state.get("data_quality_report") or {}
    completed = [t for t in state.get("completed_tasks", [])
                 if t["status"] == AnalysisStatus.COMPLETED]
    failed = state.get("failed_tasks", [])
    retries = sum(t.get("retry_count", 0) for t in state.get("analysis_plan", []))

    page_head(nav, f"{state['dataset_name']} — {len(completed)} analyses completed",
              "Agent ready", "")

    # ---------- Overview ----------
    if nav == "Overview":
        k1, k2, k3, k4 = st.columns(4)
        k1.markdown(kpi("Rows analyzed", f"{q.get('total_rows', 0):,}", "Total records", "rows"),
                    unsafe_allow_html=True)
        k2.markdown(kpi("Columns analyzed", f"{q.get('total_columns', 0)}", "Total columns", "columns"),
                    unsafe_allow_html=True)
        k3.markdown(kpi("Tasks completed", f"{len(completed)}",
                        f"{len(failed)} failed" if failed else "All succeeded", "tasks"),
                    unsafe_allow_html=True)
        k4.markdown(kpi("Quality score", f"{q.get('quality_score', 0)}", "out of 100", "quality",
                        tone="tone-success", sub_ok=True), unsafe_allow_html=True)

        main, rail = st.columns([2.4, 1], gap="medium")

        with main:
            st.markdown('<div class="ip-eyebrow" style="margin-top:1.1rem">Key insights</div>',
                        unsafe_allow_html=True)
            insights = state.get("business_insights", [])
            if insights:
                for ins in insights[:4]:
                    st.markdown(
                        f'<div class="ip-insight"><div class="ip-insight-body">{md_inline(ins)}</div></div>',
                        unsafe_allow_html=True)
            else:
                st.markdown('<div class="ip-note">No business insights were generated for this run.</div>',
                            unsafe_allow_html=True)

            st.markdown('<div class="ip-eyebrow" style="margin-top:1.2rem">Analyses</div>',
                        unsafe_allow_html=True)
            for task in completed:
                conf = task.get("confidence_score") or 0
                band, tone = confidence_band(conf)
                ico = TYPE_ICONS.get(task.get("analysis_type", ""), "chart")
                with st.expander(task["title"], expanded=True):
                    st.markdown(
                        f"""<div style="display:flex; gap:.4rem; margin-bottom:.55rem">
  <span class="ip-tag">{icon(ico, 12)} {task.get('analysis_type', '')}</span>
  <span class="ip-tag {tone}">{band} &middot; {conf:.0%}</span>
</div>""",
                        unsafe_allow_html=True,
                    )
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        if task.get("interpretation"):
                            st.markdown(
                                f'<div class="ip-insight-body">{md_inline(task["interpretation"])}</div>',
                                unsafe_allow_html=True)
                        if task.get("assumptions"):
                            st.markdown(
                                f'<div class="ip-note">Assumptions: {"; ".join(task["assumptions"])}</div>',
                                unsafe_allow_html=True)
                    with c2:
                        if task.get("chart_path") and os.path.exists(task["chart_path"]):
                            st.image(task["chart_path"], use_container_width=True)

            if failed:
                st.markdown(
                    f'<div class="ip-card" style="border-left:3px solid var(--danger)">'
                    f'<div class="ip-card-title">{icon("alert", 14)} '
                    f'{len(failed)} analyses did not complete</div>'
                    f'<div class="ip-note">They reached the retry limit. '
                    f'Details are in the execution log.</div></div>',
                    unsafe_allow_html=True,
                )

        with rail:
            st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Agent activity</span></div>'
                f'{agent_activity_html(state)}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Run signals</span></div>'
                f'<div class="ip-stat"><span>Retries handled</span><b>{retries}</b></div>'
                f'<div class="ip-stat"><span>Charts produced</span>'
                f'<b>{len(state.get("chart_paths", []))}</b></div>'
                f'<div class="ip-stat"><span>Log entries</span>'
                f'<b>{len(state.get("execution_log", []))}</b></div></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Follow-up</span></div>'
                f'{chat_thread_html(state)}</div>',
                unsafe_allow_html=True,
            )
            q_side = st.text_input("Ask a follow-up", key="chat_overview",
                                   placeholder="Ask about these results",
                                   label_visibility="collapsed")
            if st.button("Send", key="send_overview", use_container_width=True):
                submit_followup(q_side, state)

    # ---------- Insights ----------
    elif nav == "Insights":
        insights = state.get("business_insights", [])
        if insights:
            for ins in insights:
                st.markdown(f'<div class="ip-insight"><div class="ip-insight-body">{md_inline(ins)}</div></div>',
                            unsafe_allow_html=True)
        else:
            st.markdown('<div class="ip-card"><div class="ip-note">No business insights were '
                        'generated for this run.</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="ip-eyebrow" style="margin-top:1.2rem">Confidence by analysis</div>',
                    unsafe_allow_html=True)
        for task in completed:
            conf = task.get("confidence_score") or 0
            band, tone = confidence_band(conf)
            st.markdown(
                f'<div class="ip-card" style="padding:.7rem .95rem">'
                f'<div class="ip-card-head" style="margin-bottom:.4rem">'
                f'<span class="ip-card-title" style="font-size:.87rem">{task["title"]}</span>'
                f'<span class="ip-tag {tone}">{conf:.0%}</span></div>'
                f'<div class="ip-meter"><span style="width:{conf * 100:.0f}%"></span></div>'
                f'<div class="ip-note">{band}</div></div>',
                unsafe_allow_html=True,
            )

    # ---------- Data Quality ----------
    elif nav == "Data Quality":
        missing_map = q.get("missing_values") or {}
        missing_total = sum(missing_map.values())
        k1, k2, k3, k4 = st.columns(4, gap="medium")
        k1.markdown(kpi("Quality score", f"{q.get('quality_score', 0)}", "out of 100", "quality",
                        tone="tone-success"), unsafe_allow_html=True)
        k2.markdown(kpi("Duplicate rows", f"{q.get('duplicate_rows', 0):,}", "Exact repeats", "alert",
                        tone="tone-warn" if q.get("duplicate_rows") else ""), unsafe_allow_html=True)
        k3.markdown(kpi("Missing values", f"{missing_total:,}",
                        f"across {len(missing_map)} columns", "alert",
                        tone="tone-warn" if missing_total else ""), unsafe_allow_html=True)
        k4.markdown(kpi("Outlier columns", f"{len(q.get('outlier_columns') or [])}",
                        "Flagged by IQR", "activity"), unsafe_allow_html=True)

        st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2, gap="medium")
        with c1:
            rows = "".join(
                f'<div class="ip-stat"><span>{col}</span><b>{cnt:,}</b></div>'
                for col, cnt in missing_map.items()
            ) or '<div class="ip-note">No missing values detected.</div>'
            st.markdown(f'<div class="ip-card ip-card--eq"><div class="ip-card-head">'
                        f'<span class="ip-card-title">Missing values by column</span></div>'
                        f'{rows}</div>', unsafe_allow_html=True)
        with c2:
            oc = q.get("outlier_columns") or []
            rows = "".join(f'<div class="ip-stat"><span>{o}</span></div>' for o in oc) \
                or '<div class="ip-note">No outliers flagged.</div>'
            st.markdown(f'<div class="ip-card ip-card--eq"><div class="ip-card-head">'
                        f'<span class="ip-card-title">Outliers</span></div>{rows}</div>',
                        unsafe_allow_html=True)

        recs = q.get("recommendations") or []
        if recs:
            st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)
            body = "".join(f'<div class="ip-note" style="margin-top:.3rem">{r}</div>' for r in recs)
            st.markdown(f'<div class="ip-card"><div class="ip-card-head">'
                        f'<span class="ip-card-title">Recommendations</span></div>{body}</div>',
                        unsafe_allow_html=True)

    # ---------- Agent Activity ----------
    elif nav == "Agent Activity":
        c1, c2 = st.columns([1.4, 1], gap="medium")
        with c1:
            st.markdown(f'<div class="ip-card"><div class="ip-card-head">'
                        f'<span class="ip-card-title">Run timeline</span></div>'
                        f'{agent_activity_html(state, limit=20)}</div>', unsafe_allow_html=True)
        with c2:
            _cm2 = measure_context(state)
            _warn2 = _cm2["pct"] > COMPRESSION_THRESHOLD
            st.markdown(
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Context window</span></div>'
                f'<div class="ip-stat"><span>Used</span>'
                f'<b>{_cm2["tokens"]:,} / {_cm2["max_tokens"]:,}</b></div>'
                f'<div class="ip-meter {"warn" if _warn2 else ""}">'
                f'<span style="width:{min(_cm2["pct"], 1) * 100:.1f}%"></span></div>'
                f'<div class="ip-note">{_cm2["pct"]:.0%} of the window'
                f'{" — older turns are being summarized" if _warn2 else ""}</div></div>',
                unsafe_allow_html=True,
            )
            _tm2 = token_monitor.summary()
            by_skill = "".join(
                f'<div class="ip-stat"><span>{s}</span><b>{d["total"]:,}</b></div>'
                for s, d in (_tm2.get("by_skill") or {}).items()
            ) or '<div class="ip-note">No attributed usage yet.</div>'
            st.markdown(
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Run signals</span></div>'
                f'<div class="ip-stat"><span>Retries handled</span><b>{retries}</b></div>'
                f'<div class="ip-stat"><span>Tasks completed</span><b>{len(completed)}</b></div>'
                f'<div class="ip-stat"><span>Tasks failed</span><b>{len(failed)}</b></div></div>'
                f'<div class="ip-card ip-card--rail"><div class="ip-card-head">'
                f'<span class="ip-card-title">Token usage</span></div>'
                f'<div class="ip-stat"><span>Total tokens</span>'
                f'<b>{_tm2["total_tokens"]:,}</b></div>'
                f'<div class="ip-stat"><span>Estimated cost</span>'
                f'<b>${_tm2["estimated_cost_usd"]:.4f}</b></div>'
                f'<div class="ip-stat"><span>LLM calls</span><b>{_tm2["calls"]}</b></div>'
                f'<div class="ip-navlabel" style="margin:.7rem 0 .3rem">By skill</div>'
                f'{by_skill}</div>',
                unsafe_allow_html=True,
            )

    # ---------- Execution Log ----------
    elif nav == "Execution Log":
        log = state.get("execution_log", [])
        tone_map = {"success": ("check", "ok"), "error": ("close", "err"),
                    "failed": ("close", "err"), "retry": ("retry", "warn"),
                    "warning": ("alert", "warn")}
        rows = []
        for e in log:
            ic, cls = tone_map.get(e.get("status", ""), ("play", "info"))
            ts = str(e.get("timestamp", ""))[:19].replace("T", " ")
            detail = (f'<div class="ip-log-detail">{clean_detail(e.get("detail", ""))}</div>'
                      if e.get("detail") else "")
            rows.append(
                f'<div class="ip-log"><span class="ip-log-ico {cls}">{icon(ic, 14)}</span>'
                f'<div style="flex:1">'
                f'<div style="display:flex; gap:.5rem; align-items:baseline; '
                f'justify-content:space-between">'
                f'<span class="ip-log-node">{e.get("node", "")}</span>'
                f'<span class="ip-log-time">{ts}</span></div>'
                f'<div class="ip-log-action">{e.get("action", "")}</div>{detail}</div></div>'
            )
        body = "".join(rows) or '<div class="ip-note">No log entries.</div>'
        st.markdown(f'<div class="ip-card">{body}</div>', unsafe_allow_html=True)

    # ---------- Follow-up Chat ----------
    elif nav == "Follow-up Chat":
        st.markdown(
            f'<div class="ip-card"><div class="ip-card-head">'
            f'<span class="ip-card-title">Conversation</span>'
            f'<span class="ip-tag neutral">Answers use stored findings</span></div>'
            f'{chat_thread_html(state)}</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div style="height:.8rem"></div>', unsafe_allow_html=True)
        c1, c2 = st.columns([5, 1], gap="small", vertical_alignment="bottom")
        with c1:
            q_full = st.text_input("Ask a follow-up", key="chat_full",
                                   placeholder="Ask about the analysis results",
                                   label_visibility="collapsed")
        with c2:
            send = st.button("Send", key="send_full", use_container_width=True, type="primary")
        if send:
            submit_followup(q_full, state)

    # ---------- Downloads ----------
    elif nav == "Downloads":
        c1, c2, c3 = st.columns(3, gap="medium")
        with c1:
            md_path = state.get("final_report_md")
            if md_path and os.path.exists(md_path):
                with open(md_path, "r") as f:
                    st.download_button("Markdown report", f.read(),
                                       file_name=os.path.basename(md_path),
                                       mime="text/markdown", use_container_width=True)
        with c2:
            pdf_path = state.get("final_report_pdf")
            if pdf_path and os.path.exists(str(pdf_path)):
                with open(pdf_path, "rb") as f:
                    st.download_button("PDF report", f.read(),
                                       file_name=os.path.basename(pdf_path),
                                       mime="application/pdf", use_container_width=True)
        with c3:
            nb_path = state.get("notebook_path")
            if nb_path and os.path.exists(nb_path):
                with open(nb_path, "r") as f:
                    st.download_button("Jupyter notebook", f.read(),
                                       file_name=os.path.basename(nb_path),
                                       mime="application/json", use_container_width=True)

        st.markdown('<div class="ip-eyebrow" style="margin-top:1.2rem">Generated charts</div>',
                    unsafe_allow_html=True)
        chart_paths = state.get("chart_paths", [])
        if chart_paths:
            cols = st.columns(2, gap="medium")
            for i, cp in enumerate(chart_paths):
                if os.path.exists(cp):
                    with cols[i % 2]:
                        st.image(cp, caption=os.path.basename(cp), use_container_width=True)
        else:
            st.markdown('<div class="ip-note">No charts were produced in this run.</div>',
                        unsafe_allow_html=True)

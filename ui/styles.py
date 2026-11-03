"""Product styling: trusted local font bytes and our own container keys only."""
import base64
from pathlib import Path
import streamlit as st

@st.cache_data(show_spinner=False, max_entries=1)
def font_css():
    root = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    rules = []
    for weight, name in [(400, "Regular"), (600, "SemiBold")]:
        content = (root / f"IBMPlexSansArabic-{name}.ttf").read_bytes()
        if not content.startswith(b"\x00\x01\x00\x00"):
            raise ValueError("Invalid bundled font")
        encoded = base64.b64encode(content).decode("ascii")
        rules.append("@font-face{font-family:'IBM Plex Sans Arabic';font-weight:" + str(weight)
                     + ";font-style:normal;font-display:swap;src:url(data:font/ttf;base64,"
                     + encoded + ") format('truetype');}")
    return "".join(rules)


def apply_styles():
    st.html("<style>" + font_css() + """
    .stApp {text-align:right;}
    .stApp main, .stApp [aria-label="Sidebar"] {direction:rtl;}
    .stApp [aria-label="Sidebar"] {overflow-x:hidden;}
    .stApp textarea {direction:rtl; text-align:right;}
    .stApp p, .stApp pre {overflow-wrap:anywhere;}
    /* Semantic caption/Markdown blocks; avoid generated Emotion class names. */
    .stApp main div:has(> p) {opacity:1;color:light-dark(#68738A,#B8C1D6);}
    .stApp main button div:has(> p) {color:inherit;}
    .stApp .st-key-cost_panel div:has(> p) {color:inherit;}
    .stApp h1 {font-size:1.85rem; letter-spacing:-.025em;}
    .stApp h3 {font-size:1.18rem;}
    .stApp button:focus-visible, .stApp summary:focus-visible {outline:3px solid #6038E8;outline-offset:3px;}
    .stApp [aria-label="Sidebar"] button:focus-visible {outline-color:#C8BAFF;}
    .st-key-brand h1 {color:light-dark(#111B38,#E7EDF5);}
    .st-key-brand {padding:.6rem 1rem;border:1px solid #E5E8F1;border-color:light-dark(#E5E8F1,#304256);border-radius:1rem;
      background:light-dark(#FFFFFF,#18213D);margin-bottom:.6rem;}
    .st-key-sidebar_brand {padding:.5rem .2rem 1rem;}
    .st-key-sidebar_brand strong {font-size:1.15rem;font-weight:600;}
    .st-key-welcome {background:linear-gradient(135deg,rgba(96,56,232,.08),rgba(96,56,232,.02));padding:1.3rem;}
    .st-key-chat_surface {min-height:20rem;background:light-dark(#FFFFFF,#18213D);}
    .st-key-chat_surface [aria-label="Chat message from user"] {background:rgba(96,56,232,.07);border-radius:1rem;}
    .st-key-chat_surface [aria-label="Chat message from assistant"] {background:transparent;}
    .st-key-approval_card {background:light-dark(#FFFFFF,#18213D);border:1px solid #F59E0B;border-inline-start:4px solid #F59E0B;}
    .st-key-activity_panel,.st-key-cost_panel {background:light-dark(#FFFFFF,#18213D);font-variant-numeric:tabular-nums;}
    .st-key-agent_workspace {min-width:0;}
    .st-key-workspace_grid {min-width:0;container-type:inline-size;container-name:ss-workspace;}
    .st-key-latest_source {border-inline-start:3px solid #6038E8;background:rgba(96,56,232,.04);}
    .st-key-session_summary {border-top:1px solid rgba(255,255,255,.2);padding-top:.8rem;}
    @media (min-width:1024px) { .stApp {direction:rtl;} }
    @media (max-width:1023px) { .stApp {direction:ltr;} }
    @media (max-width:640px) {
      .stApp h1 {font-size:1.5rem;}
      .st-key-brand {padding:.7rem;}
      .st-key-welcome {padding:.8rem;}
      .st-key-chat_surface {min-height:12rem;}
    }
    /* The native column row is scoped to our own grid, never nested metrics.
       Use available content width, including an open tablet sidebar. */
    @container ss-workspace (max-width:700px) {
      .st-key-workspace_grid:has(.st-key-agent_workspace) > div > div {
        display:grid;grid-template-columns:minmax(0,1fr);
      }
      .st-key-workspace_grid:has(.st-key-agent_workspace) > div > div > div {
        width:100%;min-width:0;max-width:100%;
      }
    }
    </style>""")

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
    .stApp {text-align:right;direction:rtl;}
    .stApp main, .stApp [aria-label="Sidebar"] {direction:rtl;}
    .stApp [aria-label="Sidebar"] {overflow-x:hidden;}
    .stApp textarea {direction:rtl; text-align:right;}
    .stApp p, .stApp pre {overflow-wrap:anywhere;}
    /* Semantic caption/Markdown blocks; avoid generated Emotion class names. */
    .stApp main div:has(> p) {opacity:1;}
    .stApp main button div:has(> p) {color:inherit;}
    .stApp .st-key-cost_panel div:has(> p) {color:inherit;}
    .stApp h1 {font-size:1.8rem; letter-spacing:-.025em;line-height:1.6;}
    .stApp h2 {font-size:1.6rem;line-height:1.7;}
    .stApp h3 {font-size:1.12rem;line-height:1.7;}
    .stApp input {direction:rtl;text-align:right;}
    .stApp button {min-height:2.7rem;transition:box-shadow .18s ease;}
    .stApp button:hover {box-shadow:0 3px 12px #1025440C;}
    .stApp button:focus-visible, .stApp summary:focus-visible {outline:3px solid #6038E8;outline-offset:3px;}
    .stApp [aria-label="Sidebar"] button:focus-visible {outline-color:#C8BAFF;}
    .st-key-brand h1 {color:light-dark(#102544,#E7EDF5);}
    .st-key-brand {padding:.2rem 0 1rem;border-bottom:1px solid light-dark(#E4EAF1,#304256);margin-bottom:.5rem;}
    .st-key-sidebar_brand {padding:.5rem .2rem 1rem;}
    .st-key-sidebar_brand strong {font-size:1.35rem;font-weight:600;}
    .st-key-page_nav label {padding:.7rem .6rem;border-radius:.7rem;}
    .st-key-page_nav label:has(input:checked) {background:light-dark(#E6F7F4,#233C48);color:light-dark(#087F8C,#8EE4DD);font-weight:600;}
    .st-key-page_nav {margin-bottom:1rem;}
    .st-key-page_nav label[data-testid="stRadioOption"] {width:100%;min-height:3rem;}
    .st-key-page_nav label[data-testid="stRadioOption"] > div > div:first-child {display:none;}
    .st-key-page_nav label:focus-within {outline:3px solid #087F8C;outline-offset:2px;}
    .st-key-welcome {background:linear-gradient(125deg,rgba(18,182,166,.06),rgba(21,132,189,.03));padding:1.3rem;}
    .st-key-chat_surface {min-height:20rem;background:light-dark(#FFFFFF,#18213D);}
    .st-key-chat_surface [aria-label="Chat message from user"] {background:light-dark(#F2F4F7,#25334A);border-radius:1rem; margin-inline-start:12%;}
    .st-key-chat_surface [aria-label="Chat message from assistant"] {background:light-dark(#EDF7FD,#172F47);border-radius:1rem;margin-inline-end:5%;}
    .st-key-chat_surface [aria-label="Chat message from assistant"] p {line-height:1.95;}
    .st-key-approval_card {background:light-dark(#FCFBFF,#211E3E);border:1px solid light-dark(#DAD5FA,#6654D9);padding:1.3rem;}
    .st-key-approval_card button[kind="primary"] {background:#6654D9;border-color:#6654D9;color:white;}
    .st-key-approval_card h3 {font-size:1.45rem;}
    .st-key-activity_panel,.st-key-cost_panel {background:light-dark(#FFFFFF,#18213D);font-variant-numeric:tabular-nums;}
    .st-key-agent_workspace {min-width:0;}
    .st-key-workspace_grid {min-width:0;container-type:inline-size;container-name:ss-workspace;}
    .st-key-latest_source {border-inline-start:3px solid #12B6A6;background:rgba(18,182,166,.04);}
    .st-key-session_summary {border:1px solid light-dark(#E4EAF1,#304256);border-radius:.85rem;padding:1rem;background:light-dark(#FAFDFD,#18213D);}
    .st-key-dashboard_hero {padding:1.5rem 0 .8rem;}
    .st-key-dashboard_hero h2 {font-size:clamp(1.7rem,3vw,2.6rem);}
    .st-key-overview_cards [data-testid="stMetric"] {background:light-dark(#FFFFFF,#18213D);padding:1.3rem;border:1px solid light-dark(#E4EAF1,#304256);border-radius:1rem;}
    .st-key-overview_cards [data-testid="stMetricValue"] {direction:ltr;text-align:right;}
    .st-key-login_card {max-width:32rem;margin:2rem auto;padding:2rem;border:1px solid light-dark(#E4EAF1,#304256);border-radius:1.3rem;background:light-dark(#FFFFFF,#18213D);box-shadow:0 12px 60px #10254409;}
    .st-key-login_intro {padding:3rem 1rem;}
    .st-key-login_intro h1 {font-size:2.7rem;}
    .st-key-login_grid {container-type:inline-size;container-name:ss-login;}
    .st-key-search_panel,.st-key-order_card,.st-key-security_panel {background:light-dark(#FFFFFF,#18213D);padding:1.25rem;}
    .st-key-conversation_browser {background:light-dark(#FFFFFF,#18213D);padding:1rem;}
    .st-key-order_steps {padding:1rem;border-radius:1rem;background:light-dark(#F0FAF6,#182F35);}
    .st-key-footer {border-top:1px solid light-dark(#E4EAF1,#304256);padding-top:1rem;margin-top:1rem;}
    @media (prefers-reduced-motion:reduce) {.stApp button {transition:none;}}
    @container ss-login (max-width:800px) {
      .st-key-login_grid > div {flex-direction:column;}
      .st-key-login_grid > div > div {width:100%;max-width:100%;min-width:0;}
    }
    @media (max-width:640px) {
      .stApp h1 {font-size:1.5rem;}
      .st-key-brand {padding:.7rem;}
      .st-key-welcome {padding:.8rem;}
      .st-key-chat_surface {min-height:12rem;}
      .st-key-login_card {padding:1.2rem;margin:.4rem auto;}
      .st-key-login_intro {padding:.5rem;}
      .st-key-login_intro h1 {font-size:1.8rem;}
      .st-key-chat_surface [aria-label^="Chat message"] {margin-inline:0;}
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

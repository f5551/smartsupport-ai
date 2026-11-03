"""Streamlit resource factory. Shared registry, strictly private conversations."""

import streamlit as st

from web_runtime import WebBackend


@st.cache_resource(max_entries=1, show_spinner=False, on_release=lambda backend: backend.close())
def get_web_backend(directory: str, bind_address: str):
    """UI v4 durable journal with proof-based recovery and read-only history.

    Changing this factory invalidates the old nonpersistent cached resource.
    """
    return WebBackend(directory, bind_address=bind_address)

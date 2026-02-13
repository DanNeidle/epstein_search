# © Dan Neidle and Tax Policy Associates 2026
import inspect
import os
from typing import Any

import streamlit as st

from ai_search.config import (
    ASSISTANT_AVATAR_PATH,
    CSS_PATH,
    LOGO_PATH,
    USER_AVATAR_PATH,
)


def ensure_chat_avatar_assets() -> None:
    user_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0b247d"/>
<circle cx="32" cy="24" r="10" fill="#ffffff"/>
<path d="M16 50c0-9.3 7.3-16 16-16s16 6.7 16 16" fill="#ffffff"/>
</svg>
"""
    assistant_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#1133af"/>
<rect x="16" y="14" width="32" height="36" rx="6" fill="#ffffff"/>
<rect x="22" y="22" width="20" height="4" rx="2" fill="#1133af"/>
<rect x="22" y="30" width="20" height="4" rx="2" fill="#1133af"/>
<rect x="22" y="38" width="14" height="4" rx="2" fill="#1133af"/>
</svg>
"""
    if not os.path.isfile(USER_AVATAR_PATH):
        with open(USER_AVATAR_PATH, "w", encoding="utf-8") as f:
            f.write(user_svg)
    if not os.path.isfile(ASSISTANT_AVATAR_PATH):
        with open(ASSISTANT_AVATAR_PATH, "w", encoding="utf-8") as f:
            f.write(assistant_svg)


def get_chat_avatar(role: str) -> str:
    if role == "user" and os.path.isfile(USER_AVATAR_PATH):
        return USER_AVATAR_PATH
    if role == "assistant" and os.path.isfile(ASSISTANT_AVATAR_PATH):
        return ASSISTANT_AVATAR_PATH
    return "👤" if role == "user" else "🧠"


def material_icon_button(
    label: str,
    *,
    icon_name: str | None = None,
    fallback_label: str | None = None,
    **kwargs: Any,
) -> bool:
    if icon_name:
        try:
            if "icon" in inspect.signature(st.button).parameters:
                return bool(st.button(label, icon=f":material/{icon_name}:", **kwargs))
        except Exception:
            pass
    label_fallback = fallback_label if fallback_label is not None else label
    return bool(st.button(label_fallback, **kwargs))


def apply_brand_theme() -> None:
    if not os.path.isfile(CSS_PATH):
        return
    with open(CSS_PATH, "r") as f:
        css = f.read()
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def render_brand_header(subtitle: str, compact: bool = False) -> None:
    compact_class = " tpa-hero--compact" if compact else ""
    st.markdown(
        (
            f'<section class="tpa-hero{compact_class}">'
            '<p class="tpa-hero__eyebrow">Tax Policy Associates</p>'
            '<h1 class="tpa-hero__title">Epstein agentic search</h1>'
            f'<p class="tpa-hero__subtitle">{subtitle}</p>'
            "</section>"
        ),
        unsafe_allow_html=True,
    )


def scrollable_container(height: int | None, key: str):
    if height is None:
        return st.container(key=key)
    try:
        return st.container(height=height, key=key)
    except TypeError:
        return st.container(key=key)

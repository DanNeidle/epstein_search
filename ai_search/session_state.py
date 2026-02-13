# © Dan Neidle and Tax Policy Associates 2026
import streamlit as st

from ai_search.assets_utils import ensure_static_files_for_messages
from ai_search.auth_db import authenticate_session_token, get_auth_cookie
from ai_search.chat_db import load_conversation_messages, reset_chat_state


def ensure_auth_session_state() -> None:
    if "auth_user" not in st.session_state:
        st.session_state.auth_user = None
    if "auth_token" not in st.session_state:
        st.session_state.auth_token = None
    if "admin_view" not in st.session_state:
        st.session_state.admin_view = None
    if "admin_edit_user" not in st.session_state:
        st.session_state.admin_edit_user = None
    if "clear_auth_cookie" not in st.session_state:
        st.session_state.clear_auth_cookie = False
    if "current_conversation_id" not in st.session_state:
        st.session_state.current_conversation_id = None
    if "pending_delete_conversation_id" not in st.session_state:
        st.session_state.pending_delete_conversation_id = None
    if "pending_delete_conversation_title" not in st.session_state:
        st.session_state.pending_delete_conversation_title = ""
    if "pending_delete_user_username" not in st.session_state:
        st.session_state.pending_delete_user_username = ""


def restore_auth_from_cookie_if_needed() -> None:
    if st.session_state.auth_user is not None:
        return
    token = get_auth_cookie()
    if not token:
        return
    user = authenticate_session_token(token)
    if user is None:
        st.session_state.clear_auth_cookie = True
        return
    st.session_state.auth_user = user
    st.session_state.auth_token = token


def load_conversation_into_session(conversation_id: int) -> None:
    reset_chat_state()
    st.session_state.current_conversation_id = conversation_id
    loaded_messages = load_conversation_messages(conversation_id)
    st.session_state.messages = ensure_static_files_for_messages(loaded_messages)


def ensure_session_state_defaults() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "chat_session" not in st.session_state:
        st.session_state.chat_session = None
    if "chat_client" not in st.session_state:
        st.session_state.chat_client = None
    if "chat_api_key" not in st.session_state:
        st.session_state.chat_api_key = None
    if "chat_max_loops" not in st.session_state:
        st.session_state.chat_max_loops = None
    if "doc_id_to_source_path" not in st.session_state:
        st.session_state.doc_id_to_source_path = {}

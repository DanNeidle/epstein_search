# © Dan Neidle and Tax Policy Associates 2026
import os

import streamlit as st

from ai_search.auth_db import revoke_auth_session
from ai_search.chat_db import create_conversation, delete_conversation, list_conversations, reset_chat_state
from ai_search.config import LOGO_PATH
from ai_search.session_state import load_conversation_into_session
from ai_search.ui_components import material_icon_button, scrollable_container
from ai_search.ui_admin import handle_delete_user_confirmation


def render_sidebar(gemini_api_key: str) -> str:
    api_key = ""
    with st.sidebar:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width="stretch")
        auth_user = st.session_state.auth_user
        if auth_user is None:
            st.markdown("---")
            st.info("Sign in from the main panel.")
        else:
            user_id = int(auth_user["id"])
            username = str(auth_user.get("username", "user"))
            role_label = "admin" if auth_user.get("is_admin") else "user"
            is_admin_user = bool(auth_user.get("is_admin"))
            options_col = None
            users_col = None
            if is_admin_user:
                info_col, signout_col, options_col, users_col = st.columns([6, 1, 1, 1], gap="small")
            else:
                info_col, signout_col = st.columns([8, 1], gap="small")
            with info_col:
                st.markdown(
                    f'<div class="sidebar-userline">{username} <span>({role_label})</span></div>',
                    unsafe_allow_html=True,
                )
            with signout_col:
                if material_icon_button(
                    " ",
                    icon_name="logout",
                    fallback_label="Sign out",
                    key="sidebar-signout",
                    help="Sign out",
                    type="secondary",
                ):
                    token = str(st.session_state.auth_token or "")
                    if token:
                        revoke_auth_session(token)
                    st.session_state.auth_user = None
                    st.session_state.auth_token = None
                    st.session_state.admin_view = None
                    st.session_state.admin_edit_user = None
                    st.session_state.pending_delete_user_username = ""
                    st.session_state.clear_auth_cookie = True
                    st.session_state.pending_delete_conversation_id = None
                    st.session_state.pending_delete_conversation_title = ""
                    reset_chat_state()
                    st.rerun()
            if is_admin_user and options_col is not None and users_col is not None:
                with options_col:
                    if material_icon_button(
                        " ",
                        icon_name="settings",
                        fallback_label="Options",
                        key="admin-options-toggle",
                        help="Options",
                        type="secondary",
                    ):
                        current = st.session_state.get("admin_view")
                        st.session_state.admin_view = None if current == "options" else "options"
                        st.session_state.admin_edit_user = None
                        st.rerun()
                with users_col:
                    if material_icon_button(
                        " ",
                        icon_name="group",
                        fallback_label="Users",
                        key="admin-users-toggle",
                        help="User management",
                        type="secondary",
                    ):
                        current = st.session_state.get("admin_view")
                        st.session_state.admin_view = None if current == "users" else "users"
                        st.session_state.admin_edit_user = None
                        st.rerun()

            st.markdown("---")
            if material_icon_button(
                "New chat",
                icon_name="add_comment",
                key="new-chat-btn",
                use_container_width=True,
            ):
                new_id = create_conversation(user_id)
                st.session_state.current_conversation_id = new_id
                reset_chat_state()
                st.session_state.current_conversation_id = new_id
                st.rerun()
            st.markdown('<div class="sidebar-section-title">Chat History</div>', unsafe_allow_html=True)

            conversations = list_conversations(user_id)
            if not conversations:
                new_id = create_conversation(user_id)
                conversations = list_conversations(user_id)
                st.session_state.current_conversation_id = new_id

            conversation_ids = [c["id"] for c in conversations]
            current_id = st.session_state.current_conversation_id
            if current_id not in conversation_ids:
                current_id = conversation_ids[0]
                st.session_state.current_conversation_id = current_id

            with scrollable_container(None, key="history-pane"):
                for conv in conversations:
                    conv_id = conv["id"]
                    title = str(conv.get("title", "")).strip() or "Untitled chat"
                    if len(title) > 54:
                        title = title[:51] + "..."
                    updated = str(conv.get("updated_at", ""))[:16].replace("T", " ")
                    row_left, row_right = st.columns([9, 1], gap="small")
                    with row_left:
                        button_type = "primary" if conv_id == st.session_state.current_conversation_id else "secondary"
                        if st.button(
                            title,
                            key=f"chat-select-{conv_id}",
                            use_container_width=True,
                            type=button_type,
                        ):
                            if conv_id != st.session_state.current_conversation_id:
                                load_conversation_into_session(conv_id)
                                st.rerun()
                        st.markdown(
                            f'<div class="history-updated">{updated}</div>',
                            unsafe_allow_html=True,
                        )
                    with row_right:
                        if material_icon_button(
                            " ",
                            icon_name="delete",
                            fallback_label="Delete",
                            key=f"chat-del-{conv_id}",
                            help="Delete chat",
                            use_container_width=True,
                            type="secondary",
                        ):
                            st.session_state.pending_delete_conversation_id = conv_id
                            st.session_state.pending_delete_conversation_title = conv["title"]
                            st.rerun()

            current_id = st.session_state.current_conversation_id
            if current_id is not None and not st.session_state.messages:
                load_conversation_into_session(current_id)

            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or gemini_api_key

    return api_key


@st.dialog("Delete chat")
def confirm_delete_dialog() -> None:
    pending_id = st.session_state.pending_delete_conversation_id
    title = st.session_state.pending_delete_conversation_title or "this chat"
    auth_user = st.session_state.auth_user
    if pending_id is None or auth_user is None:
        st.session_state.pending_delete_conversation_id = None
        st.session_state.pending_delete_conversation_title = ""
        st.rerun()
        return

    st.markdown(f"Delete **{title}**? This cannot be undone.")
    col_cancel, col_delete = st.columns(2)
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.session_state.pending_delete_conversation_id = None
            st.session_state.pending_delete_conversation_title = ""
            st.rerun()
    with col_delete:
        if st.button("Delete", use_container_width=True, type="primary"):
            user_id = int(auth_user["id"])
            deleted = delete_conversation(int(pending_id), user_id)
            st.session_state.pending_delete_conversation_id = None
            st.session_state.pending_delete_conversation_title = ""
            if not deleted:
                st.warning("Unable to delete chat.")
                st.rerun()
                return

            conversations = list_conversations(user_id)
            if not conversations:
                new_id = create_conversation(user_id)
                conversations = list_conversations(user_id)
            remaining_ids = [c["id"] for c in conversations]
            current_id = st.session_state.current_conversation_id
            next_id = current_id if current_id in remaining_ids else remaining_ids[0]
            load_conversation_into_session(next_id)
            st.rerun()


@st.dialog("Delete user")
def confirm_delete_user_dialog() -> None:
    handle_delete_user_confirmation()

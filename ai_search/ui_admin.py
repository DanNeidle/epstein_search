# © Dan Neidle and Tax Policy Associates 2026
import os
import re

import streamlit as st

from ai_search.auth_db import (
    create_user,
    delete_user,
    list_users,
    update_user_admin_flag,
    update_user_password,
)
from ai_search.config import (
    MAX_LOOPS,
    MIN_LOOPS,
    SYSTEM_PROMPT_PATH,
)
from ai_search.es_client import get_es_client
from ai_search.ui_components import material_icon_button


def render_admin_options_panel(
    current_api_key: str,
    current_max_loops: int,
    gemini_api_key: str,
) -> tuple[str, int]:
    st.markdown("### Admin options")
    api_key = current_api_key
    api_key_env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key_env:
        st.success("API key found in env")
    elif gemini_api_key:
        st.info("Using key from secrets module")
    api_key_input = st.text_input("Set API key", type="password", key="admin_api_key_input")
    if api_key_input:
        os.environ["GOOGLE_API_KEY"] = api_key_input
        api_key = api_key_input
        st.success("API key updated for this session")

    max_loops = st.slider(
        "Max Autonomous Steps",
        MIN_LOOPS,
        MAX_LOOPS,
        int(current_max_loops),
        key="admin_max_loops_slider",
    )
    es_client = get_es_client()
    ok, msg = es_client.healthcheck()
    if ok:
        st.success(msg)
    else:
        st.error(msg)

    return api_key, max_loops


def render_admin_users_panel() -> None:
    st.markdown("### Create user")
    with st.form("create_user_form", clear_on_submit=True):
        new_username = st.text_input("Username", key="new_username")
        new_password = st.text_input("Password", type="password", key="new_password")
        new_is_admin = st.checkbox(
            "Admin user",
            key="new_is_admin",
            label_visibility="visible",
        )
        create_submit = st.form_submit_button("Create user")
    if create_submit:
        ok, msg = create_user(new_username, new_password, new_is_admin)
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    users = list_users()
    st.markdown("### Users")
    if not users:
        st.info("No users found.")
        return

    st.markdown(
        '<div class="admin-users-header">'
        '<span>User</span><span>Role</span><span>Edit</span><span>Delete</span>'
        "</div>",
        unsafe_allow_html=True,
    )

    for user in users:
        username = str(user["username"])
        key_slug = re.sub(r"[^A-Za-z0-9_-]", "_", username)
        role = "admin" if bool(user["is_admin"]) else "user"
        col_user, col_role, col_edit, col_delete = st.columns([6, 2, 1, 1], gap="small")
        with col_user:
            st.markdown(f'<div class="admin-user-cell">{username}</div>', unsafe_allow_html=True)
        with col_role:
            st.markdown(f'<div class="admin-user-cell admin-user-role">{role}</div>', unsafe_allow_html=True)
        with col_edit:
            if material_icon_button(
                " ",
                icon_name="edit",
                fallback_label="Edit",
                key=f"user-edit-{key_slug}",
                help=f"Amend {username}",
                type="secondary",
            ):
                st.session_state.admin_edit_user = username
                st.rerun()
        with col_delete:
            if material_icon_button(
                " ",
                icon_name="delete",
                fallback_label="Delete",
                key=f"user-delete-{key_slug}",
                help=f"Delete {username}",
                type="secondary",
            ):
                st.session_state.pending_delete_user_username = username
                st.rerun()

    editing_user = st.session_state.admin_edit_user
    if not editing_user:
        return
    target = next((u for u in users if u["username"] == editing_user), None)
    if target is None:
        st.session_state.admin_edit_user = None
        return

    key_slug = re.sub(r"[^A-Za-z0-9_-]", "_", str(editing_user))
    st.markdown("### Amend user")
    st.markdown(f"Editing `{editing_user}`")
    with st.form("amend_user_form", clear_on_submit=False):
        new_pw = st.text_input("New password", type="password", key=f"amend-password-{key_slug}")
        is_admin_flag = st.checkbox(
            "Admin user",
            value=bool(target["is_admin"]),
            key=f"amend-is-admin-{key_slug}",
            label_visibility="visible",
        )
        save_submit = st.form_submit_button("Save changes")
        cancel_submit = st.form_submit_button("Cancel")

    if cancel_submit:
        st.session_state.admin_edit_user = None
        st.rerun()
    if save_submit:
        if new_pw:
            ok_pw, msg_pw = update_user_password(str(editing_user), new_pw)
            if ok_pw:
                st.success(msg_pw)
            else:
                st.error(msg_pw)
        ok_admin, msg_admin = update_user_admin_flag(str(editing_user), bool(is_admin_flag))
        if ok_admin:
            st.success(msg_admin)
            st.session_state.admin_edit_user = None
            st.rerun()
        else:
            st.error(msg_admin)


def handle_delete_user_confirmation() -> None:
    pending_username = str(st.session_state.pending_delete_user_username or "").strip()
    auth_user = st.session_state.auth_user
    if not pending_username or auth_user is None or not bool(auth_user.get("is_admin")):
        st.session_state.pending_delete_user_username = ""
        st.rerun()
        return

    st.markdown(f"Delete user **{pending_username}**? This cannot be undone.")
    col_cancel, col_delete = st.columns(2)
    with col_cancel:
        if st.button("Cancel", use_container_width=True, key="cancel-delete-user"):
            st.session_state.pending_delete_user_username = ""
            st.rerun()
    with col_delete:
        if st.button("Delete", use_container_width=True, type="primary", key="confirm-delete-user"):
            ok, msg = delete_user(pending_username, str(auth_user.get("username", "")))
            st.session_state.pending_delete_user_username = ""
            if ok:
                if st.session_state.admin_edit_user == pending_username:
                    st.session_state.admin_edit_user = None
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

# © Dan Neidle and Tax Policy Associates 2026
import os
import re
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import streamlit as st

from ai_search.agent_loop import QuoteValidationError, run_autonomous_loop
from ai_search.assets_utils import load_system_prompt
from ai_search.auth_db import (
    authenticate_user,
    create_auth_session,
    init_auth_db,
    sync_auth_cookie,
)
from ai_search.chat_db import (
    conversation_belongs_to_user,
    create_conversation,
    init_chat_db,
    save_conversation_message,
    update_conversation_title_if_default,
    reset_chat_state,
)
from ai_search.citations import build_downloads_from_response, format_assistant_message
from ai_search.config import ASSETS_DIR, LOGO_PATH, MAX_LOOPS, STATIC_DIR, UNVERIFIED_DRAFT_MARKER
from ai_search.session_state import (
    ensure_auth_session_state,
    ensure_session_state_defaults,
    restore_auth_from_cookie_if_needed,
)
from ai_search.tooling import (
    APIError,
    _escape_markdown_inline,
    create_chat_session,
    estimate_turn_cost,
    format_tool_call_signature,
    genai,
    render_cost_summary,
    render_tool_preview_block,
    summarize_intent_for_ui,
    summarize_tool_output_for_ui,
    types,
)
from ai_search.ui_admin import render_admin_options_panel, render_admin_users_panel
from ai_search.ui_components import (
    apply_brand_theme,
    ensure_chat_avatar_assets,
    get_chat_avatar,
    render_brand_header,
)
from ai_search.ui_sidebar import (
    confirm_delete_dialog,
    confirm_delete_user_dialog,
    render_sidebar,
)
from ai_search.verification_agent import run_verification_agent

ENV_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None

    match = ENV_ASSIGNMENT_RE.match(raw)
    if match is None:
        return None

    key = match.group("key").strip()
    value = match.group("value").strip()

    if value and value[0] in {"'", '"'}:
        quote = value[0]
        end_idx = value.rfind(quote)
        if end_idx > 0:
            value = value[1:end_idx]
        else:
            value = value[1:]
        return key, value

    # Unquoted dotenv values may include inline comments after at least one space.
    value = re.sub(r"\s+#.*$", "", value).strip()
    return key, value


def _read_home_env_var(var_name: str) -> str:
    env_path = os.path.expanduser("~/.env")
    if not os.path.isfile(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                parsed = _parse_env_assignment(raw_line)
                if parsed is None:
                    continue
                key, value = parsed
                if key == var_name:
                    return value
    except OSError:
        return ""
    return ""



def _load_gemini_api_key() -> str:
    existing = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if existing:
        return existing

    from_home = _read_home_env_var("GEMINI_API_KEY")
    if from_home:
        os.environ["GEMINI_API_KEY"] = from_home
        return from_home
 
    from_home = _read_home_env_var("gemini_api_key")
    if from_home:
        os.environ["GEMINI_API_KEY"] = from_home
        return from_home

    return ""


gemini_api_key = _load_gemini_api_key()


def _split_assistant_content(content: str) -> tuple[str, bool]:
    raw = str(content or "")
    stripped = raw.lstrip()
    if not stripped.startswith(UNVERIFIED_DRAFT_MARKER):
        return raw, False
    _, _, tail = stripped.partition(UNVERIFIED_DRAFT_MARKER)
    return tail.lstrip("\n"), True


def _mark_unverified_draft(content: str) -> str:
    raw = str(content or "")
    if raw.lstrip().startswith(UNVERIFIED_DRAFT_MARKER):
        return raw
    return f"{UNVERIFIED_DRAFT_MARKER}\n{raw}"


def _render_assistant_content(content: str, downloads: list[dict[str, str]]) -> str:
    body, is_unverified = _split_assistant_content(content)
    formatted = format_assistant_message(body, downloads)
    if not is_unverified:
        return formatted
    return (
        "<div class='tpa-unverified-state'>"
        "<strong>Unverified draft answer</strong>"
        "<span>Quote checks failed after 3 attempts. Review citations carefully before relying on this response.</span>"
        "</div>"
        f"{formatted}"
    )


def run() -> None:
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    ensure_chat_avatar_assets()

    init_auth_db()
    init_chat_db()
    ensure_auth_session_state()
    restore_auth_from_cookie_if_needed()

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
    if "pending_assistant_prompt" not in st.session_state:
        st.session_state.pending_assistant_prompt = ""

    st.set_page_config(
        page_title="Tax Policy Associates | Epstein Archive AI",
        page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else "🕵️‍♀️",
        layout="wide",
        initial_sidebar_state="expanded" if st.session_state.auth_user is None else "auto",
    )
    apply_brand_theme()
    if st.session_state.clear_auth_cookie:
        sync_auth_cookie(None)
        st.session_state.clear_auth_cookie = False
    elif st.session_state.auth_user is not None and st.session_state.auth_token:
        sync_auth_cookie(str(st.session_state.auth_token))

    api_key = ""
    max_loops = MAX_LOOPS
    system_prompt = load_system_prompt()
    if system_prompt is None:
        st.error("Missing system prompt file: ai_search/system_prompt.md")
        st.stop()
        return

    api_key = render_sidebar(gemini_api_key)

    auth_user = st.session_state.auth_user
    if auth_user and auth_user.get("is_admin"):
        admin_view = st.session_state.get("admin_view")
        if admin_view == "options":
            max_loops = int(st.session_state.get("admin_max_loops_slider", MAX_LOOPS))
            api_key_input_state = str(st.session_state.get("admin_api_key_input", "")).strip()
            if api_key_input_state:
                os.environ["GOOGLE_API_KEY"] = api_key_input_state
                api_key = api_key_input_state
    else:
        st.session_state.admin_view = None
        st.session_state.admin_edit_user = None

    if st.session_state.pending_delete_conversation_id is not None:
        confirm_delete_dialog()
    if st.session_state.pending_delete_user_username:
        confirm_delete_user_dialog()

    if st.session_state.auth_user is None:
        render_brand_header("Sign in to start investigating the archive.")
        _, login_col, _ = st.columns([1.0, 1.3, 1.0], gap="large")
        with login_col:
            st.markdown("### Sign in")
            with st.form("main_login_form", clear_on_submit=False):
                login_username = st.text_input("Username")
                login_password = st.text_input("Password", type="password")
                login_submit = st.form_submit_button("Sign in", use_container_width=True)
            if login_submit:
                user = authenticate_user(login_username, login_password)
                if user is None:
                    st.error("Invalid username or password.")
                else:
                    token = create_auth_session(int(user["id"]))
                    st.session_state.auth_user = user
                    st.session_state.auth_token = token
                    st.session_state.admin_view = None
                    st.session_state.admin_edit_user = None
                    st.session_state.pending_delete_user_username = ""
                    st.session_state.clear_auth_cookie = False
                    reset_chat_state()
                    st.rerun()
        st.stop()

    ensure_session_state_defaults()

    if api_key and (
        st.session_state.chat_session is None
        or st.session_state.chat_api_key != api_key
        or st.session_state.chat_max_loops != max_loops
    ):
        client, chat = create_chat_session(api_key, system_prompt, max_loops)
        st.session_state.chat_client = client
        st.session_state.chat_session = chat
        st.session_state.chat_api_key = api_key
        st.session_state.chat_max_loops = max_loops

    render_brand_header(
        "Prototype workspace for source-backed answers and document downloads.",
        compact=True,
    )
    auth_user = st.session_state.auth_user
    admin_view = st.session_state.get("admin_view")
    if auth_user and auth_user.get("is_admin") and admin_view in {"options", "users"}:
        if admin_view == "options":
            st.markdown("## Options")
            api_key, max_loops = render_admin_options_panel(api_key, max_loops, gemini_api_key)
        elif admin_view == "users":
            st.markdown("## User Management")
            render_admin_users_panel()
        st.stop()

    for idx, msg in enumerate(st.session_state.messages):
        role = str(msg["role"])
        with st.chat_message(role, avatar=get_chat_avatar(role)):
            if msg["role"] == "assistant":
                display_text = _render_assistant_content(msg["content"], msg.get("downloads", []))
                st.markdown(display_text, unsafe_allow_html=True)
            else:
                st.markdown(msg["content"])
            if "cost" in msg:
                render_cost_summary(msg["cost"])
            if "tool_calls" in msg:
                with st.expander("🔎 View Investigation Steps"):
                    for idx_step, tool_call in enumerate(msg["tool_calls"], start=1):
                        if not isinstance(tool_call, dict):
                            preview, _ = summarize_tool_output_for_ui(str(tool_call), max_lines=10, max_chars=1200)
                            st.markdown(f"**Step {idx_step}**")
                            if preview:
                                render_tool_preview_block(preview)
                            else:
                                st.caption("No tool output.")
                            continue
                        intent_block = str(tool_call.get("intent", "")).strip()
                        if "cmd" in tool_call:
                            signature = f"./ep.py {tool_call['cmd']}"
                        else:
                            tool_name = str(tool_call.get("tool", "tool"))
                            tool_args = tool_call.get("args", {})
                            safe_args = tool_args if isinstance(tool_args, dict) else {}
                            signature = format_tool_call_signature(tool_name, safe_args, include_intent=False)

                        st.markdown(f"**Step {idx_step}**")
                        intent_display = summarize_intent_for_ui(intent_block)
                        st.markdown(f"**{_escape_markdown_inline(intent_display)}**")
                        st.markdown(f"*{_escape_markdown_inline(signature)}*")

                        output_value = tool_call.get("output_preview", tool_call.get("output", ""))
                        preview, truncated = summarize_tool_output_for_ui(output_value)
                        if preview:
                            render_tool_preview_block(preview)
                        else:
                            st.caption("No tool output.")
                        if truncated or bool(tool_call.get("output_truncated_for_ui")):
                            st.caption("Preview truncated for readability.")

    pending_prompt = str(st.session_state.get("pending_assistant_prompt", "") or "").strip()
    if pending_prompt:
        prompt = pending_prompt
        st.session_state.pending_assistant_prompt = ""
        if genai is None or types is None:
            st.error("Missing dependency: google-genai. Install with: pip install google-genai")
            st.stop()

        if not api_key:
            st.error("Please provide an API Key first.")
            st.stop()

        conversation_id = st.session_state.current_conversation_id
        auth_user = st.session_state.auth_user
        if auth_user is None:
            st.error("Please sign in.")
            st.stop()
        if conversation_id is None:
            conversation_id = create_conversation(int(auth_user["id"]))
            st.session_state.current_conversation_id = conversation_id
        if not conversation_belongs_to_user(int(conversation_id), int(auth_user["id"])):
            st.error("Invalid conversation selection.")
            st.stop()

        with st.chat_message("assistant", avatar=get_chat_avatar("assistant")):
            message_placeholder = st.empty()
            status_container = st.status("Investigating...", expanded=True)
            with st.expander("Investigation steps", expanded=False):
                steps_placeholder = st.empty()
                steps_placeholder.caption("Waiting for tool calls...")

            try:
                chat_session = st.session_state.chat_session
                if chat_session is None:
                    st.error("Chat session is not initialized.")
                    st.stop()

                final_text, tool_log, response, loop_count = run_autonomous_loop(
                    prompt,
                    chat_session,
                    max_loops,
                    status_container,
                    steps_placeholder,
                )

                if loop_count >= max_loops:
                    final_text += "\n\n[Stopped: reached max autonomous steps.]"
                status_container.update(label="Verifying findings...", expanded=True)
                final_text = run_verification_agent(st.session_state.chat_client, final_text)
                downloads = build_downloads_from_response(final_text)
                display_text = _render_assistant_content(final_text, downloads)
                cost = estimate_turn_cost(prompt, final_text, response)
                status_container.update(label="Investigation complete", state="complete", expanded=False)
                message_placeholder.markdown(display_text, unsafe_allow_html=True)
                render_cost_summary(cost)

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": final_text,
                    "tool_calls": tool_log,
                    "downloads": downloads,
                    "cost": cost,
                })
                save_conversation_message(
                    int(conversation_id),
                    "assistant",
                    final_text,
                    tool_calls=tool_log,
                    downloads=downloads,
                    cost=cost,
                )
            except APIError as e:
                st.error(f"Gemini API Error: {e}")
                st.stop()
            except QuoteValidationError as e:
                draft_text = str(e.draft_text or "").strip() or "No draft answer was available."
                if e.loop_count >= max_loops:
                    draft_text += "\n\n[Stopped: reached max autonomous steps.]"
                unverified_content = _mark_unverified_draft(draft_text)
                downloads = build_downloads_from_response(draft_text)
                display_text = _render_assistant_content(unverified_content, downloads)
                cost = estimate_turn_cost(prompt, draft_text, e.response)

                status_container.update(
                    label="Investigation complete (safe mode: unverified draft)",
                    state="complete",
                    expanded=False,
                )
                message_placeholder.markdown(display_text, unsafe_allow_html=True)
                st.warning(str(e))
                render_cost_summary(cost)

                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": unverified_content,
                        "tool_calls": e.tool_log,
                        "downloads": downloads,
                        "cost": cost,
                    }
                )
                save_conversation_message(
                    int(conversation_id),
                    "assistant",
                    unverified_content,
                    tool_calls=e.tool_log,
                    downloads=downloads,
                    cost=cost,
                )
            except Exception as e:
                st.error(f"API Error: {e}")
                st.stop()

    with st.container():
        with st.form(key="investigation_input_form", clear_on_submit=True):
            user_input = st.text_area(
                "Ask a follow-up or start a new investigation:",
                height=100,
                key="inline_chat_input",
                placeholder="e.g. What specific dates was he in London?",
            )
            col_submit, col_empty = st.columns([1, 5])
            with col_submit:
                submitted = st.form_submit_button("Investigate", use_container_width=True)

    if submitted and user_input.strip():
        prompt = user_input.strip()

        if genai is None or types is None:
            st.error("Missing dependency: google-genai. Install with: pip install google-genai")
            st.stop()

        if not api_key:
            st.error("Please provide an API Key first.")
            st.stop()

        conversation_id = st.session_state.current_conversation_id
        auth_user = st.session_state.auth_user
        if auth_user is None:
            st.error("Please sign in.")
            st.stop()
        if conversation_id is None:
            conversation_id = create_conversation(int(auth_user["id"]))
            st.session_state.current_conversation_id = conversation_id
        if not conversation_belongs_to_user(int(conversation_id), int(auth_user["id"])):
            st.error("Invalid conversation selection.")
            st.stop()

        st.session_state.messages.append({"role": "user", "content": prompt})
        save_conversation_message(int(conversation_id), "user", prompt)
        update_conversation_title_if_default(int(conversation_id), prompt)
        st.session_state.pending_assistant_prompt = prompt
        st.rerun()


if __name__ == "__main__":
    run()

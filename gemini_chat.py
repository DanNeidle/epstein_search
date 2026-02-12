#!/usr/bin/env python3

"""
This is a proof of concept AI/agentic search of the Epstein files.
Install it after following the previous steps in the GitHub, with PDFs downloaded and the containers running and tested.

It comes with three very large caveats

1. The agent code is currently a prototype. It is prone to hallucination. 
The prompt needs significant expansion. The logic needs a final checking/evidence run - and probably more.

2. each use costs £ - probably $0.07 on average. So be careful before you make widely available

3. the user/login code is extremely simple and this application should under no circumstances be exposed to the internet

Despite this, we've found it useful - a small team, with the application proxied with Cloudflared and protected behind Cloudflare Zero Trust authentication.

"""


import os
import re
import shutil
import sqlite3
import subprocess
import sys
import shlex
import hashlib
import hmac
import secrets
import json
from urllib.parse import quote
from typing import Any, cast

import streamlit as st

try:
    from google import genai as _genai 
    from google.genai import types as _types 
    from google.genai.errors import APIError as _APIError  
except ImportError:
    _genai = None
    _types = None
    _APIError = Exception

genai = cast(Any, _genai)
types = cast(Any, _types)
APIError = cast(Any, _APIError)


def _in_streamlit_runtime() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


if __name__ == "__main__" and not _in_streamlit_runtime():
    # Allow running via "python gemini_chat.py" by re-launching through Streamlit.
    os.execvp("streamlit", ["streamlit", "run", os.path.abspath(__file__)])


# Add the directory containing the module to the Python path
secrets_path = '/home/dan'
if secrets_path not in sys.path:
    sys.path.append(secrets_path)

from home_automation_secrets import gemini_api_key  # type: ignore


# --- CONFIGURATION ---
MAX_LOOPS = 50  # Safety limit: max number of autonomous tool calls per user request
MODEL_NAME = "gemini-3-pro-preview"
DATA_DIR = os.path.abspath("./data")
ASSETS_DIR = os.path.abspath("./assets")
STATIC_DIR = os.path.abspath("./static")
USERS_DB_PATH = os.path.abspath("./users.db")
CSS_PATH = os.path.abspath("./gemini_chat.css")
LOGO_PATH = os.path.abspath("./logo_full_white_on_blue.jpg")
PBKDF2_ITERATIONS = 200_000

DOC_URL_RE = re.compile(r"https?://[^)\s]+/f/([0-9a-f]{32})")
BATES_RE = re.compile(r"\bEFTA\d{8}\b")
DOC_RESULT_LINE_RE = re.compile(
    r"^(?P<name>.+?) \((?:\d+|\?) pages(?:, [\d,]+ bytes)?\) https?://[^\s]+/f/(?P<doc_id>[0-9a-f]{32})(?:\s+\[NEAR-DUPLICATE\])?$"
)

INPUT_RATE_LE_200K = 2.00
INPUT_RATE_GT_200K = 4.00
OUTPUT_RATE_LE_200K = 12.00
OUTPUT_RATE_GT_200K = 18.00
CACHE_RATE_LE_200K = 0.20
CACHE_RATE_GT_200K = 0.40
MAX_TITLE_LEN = 64
ALLOWED_EP_COMMANDS = {"search", "count", "read", "cooccur", "notes", "save"}
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.pdf$")
MIN_FULL_DOC_READS = 3

# Ensure directories expected by runtime features exist at startup.
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return pwd_hash.hex(), salt.hex()


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    computed_hash_hex, _ = hash_password(password, salt_hex=salt_hex)
    return hmac.compare_digest(computed_hash_hex, expected_hash_hex)


def init_auth_db() -> None:
    with get_db_connection() as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if table_exists is None:
            conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        else:
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            ]
            if "email" in columns:
                conn.execute(
                    """
                    CREATE TABLE users_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        password_salt TEXT NOT NULL,
                        is_admin INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO users_new (id, username, password_hash, password_salt, is_admin, created_at)
                    SELECT id, username, password_hash, password_salt, is_admin, created_at
                    FROM users
                    """
                )
                conn.execute("DROP TABLE users")
                conn.execute("ALTER TABLE users_new RENAME TO users")

        # Initial bootstrap admin user requested by user.
        row = conn.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if row is None:
            password_hash, password_salt = hash_password("admin")
            conn.execute(
                """
                INSERT INTO users (username, password_hash, password_salt, is_admin)
                VALUES (?, ?, ?, ?)
                """,
                ("admin", password_hash, password_salt, 1),
            )


def init_chat_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                downloads_json TEXT NOT NULL DEFAULT '[]',
                cost_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
            """
        )


def create_user(username: str, password: str, is_admin: bool) -> tuple[bool, str]:
    username = username.strip()
    if not username:
        return False, "Username is required."
    if not password:
        return False, "Password is required."

    password_hash, password_salt = hash_password(password)
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, password_salt, is_admin)
                VALUES (?, ?, ?, ?)
                """,
                (username, password_hash, password_salt, 1 if is_admin else 0),
            )
        return True, f"Created user '{username}'."
    except sqlite3.IntegrityError:
        return False, "Username already exists."


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    username = username.strip()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, password_salt, is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
    }


def list_users() -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT username, is_admin, created_at FROM users ORDER BY username ASC"
        ).fetchall()
    return [
        {
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def update_user_password(username: str, new_password: str) -> tuple[bool, str]:
    if not new_password:
        return False, "New password is required."
    password_hash, password_salt = hash_password(new_password)
    with get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE username = ?",
            (password_hash, password_salt, username),
        )
    if cursor.rowcount == 0:
        return False, "User not found."
    return True, f"Password updated for '{username}'."


def update_user_admin_flag(username: str, is_admin: bool) -> tuple[bool, str]:
    with get_db_connection() as conn:
        if not is_admin:
            admins = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()
            target = conn.execute(
                "SELECT is_admin FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if target and target["is_admin"] and admins and int(admins["c"]) <= 1:
                return False, "Cannot remove admin role from the last admin user."
        cursor = conn.execute(
            "UPDATE users SET is_admin = ? WHERE username = ?",
            (1 if is_admin else 0, username),
        )
    if cursor.rowcount == 0:
        return False, "User not found."
    return True, f"Admin flag updated for '{username}'."


def create_conversation(user_id: int, title: str = "New chat") -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO conversations (user_id, title)
            VALUES (?, ?)
            """,
            (user_id, title[:MAX_TITLE_LEN]),
        )
        last_id = cursor.lastrowid
        if last_id is None:
            row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
            if row is None:
                raise RuntimeError("Failed to create conversation.")
            last_id = int(row["id"])
    return int(last_id)


def list_conversations(user_id: int) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at
            FROM conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def conversation_belongs_to_user(conversation_id: int, user_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return row is not None


def load_conversation_messages(conversation_id: int) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT role, content, tool_calls_json, downloads_json, cost_json
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()
    messages = []
    for row in rows:
        entry: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        tool_calls = json.loads(row["tool_calls_json"] or "[]")
        downloads = json.loads(row["downloads_json"] or "[]")
        cost = json.loads(row["cost_json"] or "{}")
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if downloads:
            entry["downloads"] = downloads
        if cost:
            entry["cost"] = cost
        messages.append(entry)
    return messages


def update_conversation_title_if_default(conversation_id: int, prompt: str) -> None:
    title = prompt.strip().replace("\n", " ")
    if not title:
        return
    new_title = title[:MAX_TITLE_LEN]
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row and row["title"] == "New chat":
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_title, conversation_id),
            )


def save_conversation_message(
    conversation_id: int,
    role: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    downloads: list[dict[str, Any]] | None = None,
    cost: dict[str, Any] | None = None,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id, role, content, tool_calls_json, downloads_json, cost_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls or []),
                json.dumps(downloads or []),
                json.dumps(cost or {}),
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    if not conversation_belongs_to_user(conversation_id, user_id):
        return False
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
    return cursor.rowcount > 0


def reset_chat_state() -> None:
    st.session_state.messages = []
    st.session_state.chat_session = None
    st.session_state.chat_client = None
    st.session_state.chat_api_key = None
    st.session_state.chat_max_loops = None
    st.session_state.doc_id_to_source_path = {}
    st.session_state.current_conversation_id = None


def ensure_auth_session_state() -> None:
    if "auth_user" not in st.session_state:
        st.session_state.auth_user = None
    if "show_admin_options" not in st.session_state:
        st.session_state.show_admin_options = False
    if "current_conversation_id" not in st.session_state:
        st.session_state.current_conversation_id = None
    if "pending_delete_conversation_id" not in st.session_state:
        st.session_state.pending_delete_conversation_id = None
    if "pending_delete_conversation_title" not in st.session_state:
        st.session_state.pending_delete_conversation_title = ""


def load_conversation_into_session(conversation_id: int) -> None:
    reset_chat_state()
    st.session_state.current_conversation_id = conversation_id
    loaded_messages = load_conversation_messages(conversation_id)
    st.session_state.messages = ensure_static_files_for_messages(loaded_messages)


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


def load_system_prompt() -> str:
    if os.path.exists("CLAUDE.md"):
        with open("CLAUDE.md", "r") as f:
            return f.read()
    if os.path.exists("claude.md"):
        with open("claude.md", "r") as f:
            return f.read()
    return "You are a helpful investigator."


def ensure_static_file_for_download(download: dict[str, Any]) -> dict[str, Any]:
    file_name = str(download.get("name", "")).strip()
    if not file_name:
        bates = str(download.get("bates", "")).strip()
        if bates:
            file_name = f"{bates}.pdf"
    # Normalize and strictly validate the filename to prevent traversal.
    file_name = os.path.basename(file_name)
    if not file_name:
        return download
    if not SAFE_FILENAME_RE.fullmatch(file_name):
        return download

    static_path = os.path.join(STATIC_DIR, file_name)
    if os.path.isfile(static_path):
        updated = dict(download)
        updated["static_path"] = static_path
        return updated

    def is_under(base_dir: str, path: str) -> bool:
        try:
            base_real = os.path.realpath(base_dir)
            path_real = os.path.realpath(path)
            return os.path.commonpath([base_real, path_real]) == base_real
        except Exception:
            return False

    candidate_paths: list[str] = []
    # Never trust arbitrary absolute paths from stored metadata.
    explicit_path = str(download.get("path", "")).strip()
    if explicit_path:
        explicit_base = os.path.basename(explicit_path)
        if explicit_base == file_name and (
            is_under(DATA_DIR, explicit_path) or is_under(ASSETS_DIR, explicit_path) or is_under(STATIC_DIR, explicit_path)
        ):
            candidate_paths.append(explicit_path)
    candidate_paths.append(os.path.join(ASSETS_DIR, file_name))
    candidate_paths.append(os.path.join(DATA_DIR, file_name))

    for candidate in candidate_paths:
        if (
            candidate
            and os.path.isfile(candidate)
            and (is_under(DATA_DIR, candidate) or is_under(ASSETS_DIR, candidate) or is_under(STATIC_DIR, candidate))
        ):
            shutil.copy2(candidate, static_path)
            updated = dict(download)
            updated["static_path"] = static_path
            if not updated.get("path"):
                updated["path"] = candidate
            if not updated.get("name"):
                updated["name"] = file_name
            return updated

    return download


def ensure_static_files_for_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        updated_msg = dict(msg)
        downloads = msg.get("downloads", [])
        if isinstance(downloads, list) and downloads:
            updated_msg["downloads"] = [ensure_static_file_for_download(d) for d in downloads if isinstance(d, dict)]
        normalized.append(updated_msg)
    return normalized


def apply_brand_theme() -> None:
    if not os.path.isfile(CSS_PATH):
        return
    with open(CSS_PATH, "r") as f:
        css = f.read()
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


init_auth_db()
init_chat_db()
ensure_auth_session_state()

# Page Config
st.set_page_config(
    page_title="Epstein Archive AI", 
    page_icon="🕵️‍♀️", 
    layout="wide",
    initial_sidebar_state="expanded" if st.session_state.auth_user is None else "auto",
)
apply_brand_theme()

# --- SIDEBAR & SETUP ---
with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width="stretch")
    st.markdown("---")

    auth_user = st.session_state.auth_user
    if auth_user is None:
        st.markdown('<div class="sidebar-section-title">Login</div>', unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            login_username = st.text_input("Username")
            login_password = st.text_input("Password", type="password")
            login_submit = st.form_submit_button("Sign in")
        if login_submit:
            user = authenticate_user(login_username, login_password)
            if user is None:
                st.error("Invalid username or password.")
            else:
                st.session_state.auth_user = user
                reset_chat_state()
                st.rerun()
        api_key = ""
        max_loops = MAX_LOOPS
        claude_prompt = load_system_prompt()
    else:
        user_id = int(auth_user["id"])
        st.success(f"Signed in as `{auth_user['username']}`")
        st.caption(f"Role: {'admin' if auth_user.get('is_admin') else 'user'}")
        if st.button("Sign out"):
            st.session_state.auth_user = None
            st.session_state.show_admin_options = False
            st.session_state.pending_delete_conversation_id = None
            st.session_state.pending_delete_conversation_title = ""
            reset_chat_state()
            st.rerun()

        st.markdown('<div class="sidebar-section-title">Chat History</div>', unsafe_allow_html=True)
        if st.button("New chat"):
            new_id = create_conversation(user_id)
            st.session_state.current_conversation_id = new_id
            reset_chat_state()
            st.session_state.current_conversation_id = new_id
            st.rerun()

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

        for conv in conversations:
            conv_id = conv["id"]
            conv_label = f"{conv['title']} ({conv['updated_at'][:16]})"
            row_left, row_right = st.columns([9, 1], gap="small")
            with row_left:
                button_type = "primary" if conv_id == st.session_state.current_conversation_id else "secondary"
                if st.button(
                    conv_label,
                    key=f"chat-select-{conv_id}",
                    use_container_width=True,
                    type=button_type,
                ):
                    if conv_id != st.session_state.current_conversation_id:
                        load_conversation_into_session(conv_id)
                        st.rerun()
            with row_right:
                if st.button(
                    "✕",
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

        # API Key Handling (hidden by default; admin-only via Options)
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or gemini_api_key

        claude_prompt = load_system_prompt()
        max_loops = MAX_LOOPS
        if auth_user.get("is_admin"):
            options_label = "Hide options" if st.session_state.show_admin_options else "Options"
            if st.button(options_label):
                st.session_state.show_admin_options = not st.session_state.show_admin_options
                st.rerun()

            if st.session_state.show_admin_options:
                st.markdown('<div class="sidebar-section-title">API Key</div>', unsafe_allow_html=True)
                api_key_env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                if api_key_env:
                    st.success("✅ API key found in env")
                elif gemini_api_key:
                    st.info("Using key from secrets module")
                api_key_input = st.text_input("Set API key", type="password", key="admin_api_key_input")
                if api_key_input:
                    os.environ["GOOGLE_API_KEY"] = api_key_input
                    api_key = api_key_input
                    st.success("✅ API key updated for this session")

                with st.expander("User management", expanded=True):
                    with st.form("create_user_form", clear_on_submit=True):
                        st.markdown("Create user")
                        new_username = st.text_input("Username", key="new_username")
                        new_password = st.text_input("Password", type="password", key="new_password")
                        new_is_admin = st.checkbox("Admin user", key="new_is_admin")
                        create_submit = st.form_submit_button("Create user")
                    if create_submit:
                        ok, msg = create_user(new_username, new_password, new_is_admin)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

                    users = list_users()
                    usernames = [u["username"] for u in users]
                    if usernames:
                        with st.form("edit_user_form", clear_on_submit=True):
                            st.markdown("Update user")
                            target_user = st.selectbox("User", usernames, key="target_user")
                            target_data = next((u for u in users if u["username"] == target_user), None)
                            new_pw = st.text_input("New password", type="password", key="target_password")
                            new_admin_flag = st.checkbox(
                                "Admin user",
                                value=bool(target_data and target_data["is_admin"]),
                                key="target_is_admin",
                            )
                            update_submit = st.form_submit_button("Apply updates")
                        if update_submit:
                            if target_user is None:
                                st.error("Select a user first.")
                                st.stop()
                            if new_pw:
                                ok_pw, msg_pw = update_user_password(target_user, new_pw)
                                if ok_pw:
                                    st.success(msg_pw)
                                else:
                                    st.error(msg_pw)
                            ok_admin, msg_admin = update_user_admin_flag(target_user, new_admin_flag)
                            if ok_admin:
                                st.success(msg_admin)
                            else:
                                st.error(msg_admin)
                    st.markdown("Current users")
                    st.table(
                        [
                            {
                                "username": u["username"],
                                "role": "admin" if u["is_admin"] else "user",
                            }
                            for u in list_users()
                        ]
                    )

                st.markdown('<div class="sidebar-section-title">Settings</div>', unsafe_allow_html=True)
                max_loops = st.slider("Max Autonomous Steps", 5, 50, MAX_LOOPS)

                st.markdown("---")
                st.markdown('<div class="sidebar-section-title">System Status</div>', unsafe_allow_html=True)
                if os.path.exists("./ep.py"):
                    st.success("✅ ep.py found")
                else:
                    st.error("❌ ep.py not found! (Required)")

                if os.path.exists("CLAUDE.md"):
                    st.success("✅ CLAUDE.md found")
                elif os.path.exists("claude.md"):
                    st.success("✅ claude.md found")
                else:
                    st.warning("⚠️ CLAUDE.md not found. Using default prompt.")

if st.session_state.pending_delete_conversation_id is not None:
    confirm_delete_dialog()

if st.session_state.auth_user is None:
    st.title("Epstein intelligent search")
    st.info("Sign in to use the app.")
    st.stop()

# --- TOOL DEFINITION ---
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


def index_documents_from_tool_output(output: str) -> None:
    mapping = st.session_state.doc_id_to_source_path
    for line in output.splitlines():
        match = DOC_RESULT_LINE_RE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip()
        doc_id = match.group("doc_id")
        source_path = os.path.join(DATA_DIR, name)
        if os.path.isfile(source_path):
            mapping[doc_id] = source_path


def sanitize_response_links(text: str) -> str:
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+/f/[0-9a-f]{32})\)",
        r"\1 (download below)",
        text,
    )
    return DOC_URL_RE.sub("[download below]", text)


def build_downloads_from_response(text: str) -> list[dict[str, str]]:
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    mapping = st.session_state.doc_id_to_source_path
    doc_ids = list(dict.fromkeys(DOC_URL_RE.findall(text)))
    downloads: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def add_download_from_source(source_path: str, doc_id: str = "") -> None:
        if not source_path or not os.path.isfile(source_path):
            return
        filename = os.path.basename(source_path)
        target_path = os.path.join(ASSETS_DIR, filename)
        static_path = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(target_path):
            shutil.copy2(source_path, target_path)
        if not os.path.isfile(static_path):
            shutil.copy2(source_path, static_path)
        if target_path in seen_paths:
            return
        seen_paths.add(target_path)
        bates = os.path.splitext(filename)[0]
        downloads.append(
            {
                "name": filename,
                "path": target_path,
                "static_path": static_path,
                "doc_id": doc_id,
                "bates": bates,
            }
        )

    for doc_id in doc_ids:
        source_path = mapping.get(doc_id)
        add_download_from_source(source_path or "", doc_id=doc_id)

    # Also resolve cited Bates numbers directly from ./data.
    for bates in dict.fromkeys(BATES_RE.findall(text)):
        source_path = os.path.join(DATA_DIR, f"{bates}.pdf")
        add_download_from_source(source_path)

    return downloads


def build_inline_download_anchor(path: str, file_name: str, label: str) -> str:
    if not file_name:
        return label
    # Streamlit static route; host-relative so it works when app is accessed over LAN.
    href = f"/app/static/{quote(file_name)}"
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'


def format_assistant_message(text: str, downloads: list[dict[str, str]]) -> str:
    formatted = sanitize_response_links(text)
    formatted = formatted.replace("(download below)", "")
    formatted = re.sub(r"\s{2,}", " ", formatted)

    for dl in downloads:
        path = dl.get("path", "")
        name = dl.get("name", "document.pdf")
        bates = dl.get("bates", os.path.splitext(name)[0])
        anchor = build_inline_download_anchor(path, name, bates)
        # Replace standalone Bates mentions with hyperlinks.
        formatted = re.sub(rf"\b{re.escape(bates)}\b", anchor, formatted)

    return formatted


def as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def get_usage_field(usage: Any, *names: str) -> int:
    if usage is None:
        return 0
    for name in names:
        if hasattr(usage, name):
            return as_int(getattr(usage, name))
        if isinstance(usage, dict) and name in usage:
            return as_int(usage.get(name))
    return 0


def estimate_tokens_fallback(prompt: str, final_text: str) -> tuple[int, int]:
    client = st.session_state.get("chat_client")
    if client is None:
        return 0, 0
    try:
        in_resp = client.models.count_tokens(model=MODEL_NAME, contents=prompt)
        out_resp = client.models.count_tokens(model=MODEL_NAME, contents=final_text)
        in_tokens = get_usage_field(in_resp, "total_tokens", "total_tokens_count", "token_count")
        out_tokens = get_usage_field(out_resp, "total_tokens", "total_tokens_count", "token_count")
        return in_tokens, out_tokens
    except Exception:
        return 0, 0


def estimate_turn_cost(prompt: str, final_text: str, response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None)
    in_tokens = get_usage_field(
        usage,
        "prompt_token_count",
        "input_token_count",
        "prompt_tokens",
        "input_tokens",
    )
    out_tokens = get_usage_field(
        usage,
        "candidates_token_count",
        "output_token_count",
        "output_tokens",
    )
    thoughts_tokens = get_usage_field(
        usage,
        "thoughts_token_count",
        "thinking_token_count",
        "thought_token_count",
    )
    cached_tokens = get_usage_field(
        usage,
        "cached_content_token_count",
        "cache_read_input_tokens",
        "cached_tokens",
    )

    # Output price includes thinking tokens. If thoughts are separately exposed, include them.
    out_total = out_tokens + thoughts_tokens

    if in_tokens == 0 and out_total == 0:
        in_tokens, out_total = estimate_tokens_fallback(prompt, final_text)
        thoughts_tokens = 0
        cached_tokens = 0

    prompt_is_large = in_tokens > 200_000
    input_rate = INPUT_RATE_GT_200K if prompt_is_large else INPUT_RATE_LE_200K
    output_rate = OUTPUT_RATE_GT_200K if prompt_is_large else OUTPUT_RATE_LE_200K
    cache_rate = CACHE_RATE_GT_200K if prompt_is_large else CACHE_RATE_LE_200K

    input_cost = (in_tokens / 1_000_000) * input_rate
    output_cost = (out_total / 1_000_000) * output_rate
    cache_cost = (cached_tokens / 1_000_000) * cache_rate
    total_cost = input_cost + output_cost + cache_cost

    return {
        "input_tokens": in_tokens,
        "output_tokens": out_total,
        "thinking_tokens": thoughts_tokens,
        "cached_tokens": cached_tokens,
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "cache_cost_usd": cache_cost,
        "total_cost_usd": total_cost,
        "price_bucket": ">200k prompt tokens" if prompt_is_large else "<=200k prompt tokens",
    }


def render_cost_summary(cost: dict[str, Any]) -> None:
    st.caption(
        "Estimated request cost: "
        f"${cost['total_cost_usd']:.6f} "
        f"(in {cost['input_tokens']:,} tok, out {cost['output_tokens']:,} tok, "
        f"cache {cost['cached_tokens']:,} tok, tier {cost['price_bucket']})"
    )


def extract_tool_output_from_function_response(function_response: Any) -> str:
    response_obj = getattr(function_response, "response", None)
    if response_obj is None:
        return "N/A (tool response not exposed by SDK)"

    if isinstance(response_obj, dict):
        value = response_obj.get("result", response_obj.get("output", response_obj))
    else:
        value = response_obj

    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except Exception:
            text = str(value)

    if len(text) > 5000:
        return text[:5000] + "\n...[Tool output truncated for UI]..."
    return text


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def bates_from_text(text: str) -> list[str]:
    return unique_preserve_order(BATES_RE.findall(text or ""))


def read_bates_from_cmd(cmd: str) -> str | None:
    try:
        parts = shlex.split(cmd)
    except Exception:
        return None
    if not parts:
        return None
    if parts[0] != "read":
        return None
    if len(parts) < 2:
        return None
    bates = parts[1].strip().upper()
    if BATES_RE.fullmatch(bates):
        return bates
    return None


def search_archive(command_args: str):
    """
    Executes the ./ep.py script with the provided arguments.
    Search the document archive. Use this for ALL queries.
    Args:
        command_args: The arguments to pass to ep.py (e.g. 'search "Mandelson"' or 'read EFTA12345')
    """
    try:
        args = shlex.split(command_args)
        if not args:
            return "Tool Execution Error: empty command."
        if args[0] not in ALLOWED_EP_COMMANDS:
            return f"Tool Execution Error: unsupported command '{args[0]}'."

        # Execute safely without invoking a shell.
        cmd_list = [sys.executable, "./ep.py"] + args

        # Run command
        result = subprocess.run(
            cmd_list,
            shell=False,
            capture_output=True, 
            text=True,
            timeout=30 # Prevent hangs
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
            
        # Truncate if massive (Gemini 1.5 has 1M context, but let's be sane)
        if len(output) > 100000:
            output = output[:100000] + "\n...[Output Truncated]..."

        index_documents_from_tool_output(output)
        return output
    except ValueError as e:
        return f"Tool Execution Error: invalid command arguments ({str(e)})."
    except Exception as e:
        return f"Tool Execution Error: {str(e)}"

tools_def = [search_archive]


def build_system_instruction(base_prompt: str) -> str:
    return f"""
    {base_prompt}
    
    IMPORTANT ARCHITECTURE NOTE:
    You are running in an autonomous loop.
    1. When the user asks a question, DO NOT just do one search and answer.
    2. You must autonomously run MULTIPLE searches, cross-references, and document reads.
    3. Keep calling the 'search_archive' tool until you have a watertight case.
    4. Only when you have gathered all evidence, output your final answer as text.
    5. Formulate links using the markdown provided by the tool output.
    """


def create_chat_session(api_key_value: str, base_prompt: str, max_remote_calls: int):
    if genai is None or types is None:
        raise RuntimeError("google-genai is not installed. Run: pip install google-genai")

    # Prefer env-based auth per SDK guidance.
    os.environ["GOOGLE_API_KEY"] = api_key_value
    client = genai.Client()
    config = types.GenerateContentConfig(
        system_instruction=build_system_instruction(base_prompt),
        tools=tools_def,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )
    chat = client.chats.create(model=MODEL_NAME, config=config)
    return client, chat


# --- SESSION STATE ---
ensure_session_state_defaults()

if api_key and (
    st.session_state.chat_session is None
    or st.session_state.chat_api_key != api_key
    or st.session_state.chat_max_loops != max_loops
):
    client, chat = create_chat_session(api_key, claude_prompt, max_loops)
    st.session_state.chat_client = client
    st.session_state.chat_session = chat
    st.session_state.chat_api_key = api_key
    st.session_state.chat_max_loops = max_loops


# --- MAIN UI ---
st.markdown('<h1 class="brand-title">Epstein intelligent search</h1>', unsafe_allow_html=True)

# Display History
for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            display_text = format_assistant_message(msg["content"], msg.get("downloads", []))
            st.markdown(display_text, unsafe_allow_html=True)
        else:
            st.markdown(msg["content"])
        if "cost" in msg:
            render_cost_summary(msg["cost"])
        if "tool_calls" in msg:
            with st.expander("🔎 View Investigation Steps"):
                for tool_call in msg["tool_calls"]:
                    st.code(f"./ep.py {tool_call['cmd']}")
                    st.code(tool_call["output"])

# Chat Input
if prompt := st.chat_input("What should we investigate?"):
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
        
    # 1. Add User Message
    st.session_state.messages.append({"role": "user", "content": prompt})
    save_conversation_message(int(conversation_id), "user", prompt)
    update_conversation_title_if_default(int(conversation_id), prompt)
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. Start The Autonomous Loop
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        status_container = st.status("🕵️‍♀️ Investigating...", expanded=True)
        with st.expander("Investigation steps", expanded=False):
            steps_placeholder = st.empty()
            steps_placeholder.caption("Waiting for tool calls...")
        
        try:
            chat_session = st.session_state.chat_session
            if chat_session is None:
                st.error("Chat session is not initialized.")
                st.stop()

            tool_log = []
            loop_count = 0
            read_bates: set[str] = set()
            discovered_bates: list[str] = []
            enforcement_rounds = 0
            response = chat_session.send_message(prompt)

            while True:
                while response.function_calls and loop_count < max_loops:
                    function_response_parts = []
                    for fn in response.function_calls:
                        if getattr(fn, "name", "") != "search_archive":
                            continue
                        cmd = ""
                        args = getattr(fn, "args", None)
                        if args and "command_args" in args:
                            cmd = args["command_args"]

                        loop_count += 1
                        status_container.update(label=f"🕵️‍♀️ Investigating... (step {loop_count})", expanded=True)
                        tool_output = search_archive(cmd)
                        tool_log.append({"cmd": cmd, "output": tool_output})

                        read_bates_cmd = read_bates_from_cmd(cmd)
                        if read_bates_cmd:
                            read_bates.add(read_bates_cmd)
                        discovered_bates.extend(bates_from_text(tool_output))

                        lines = []
                        for i, tc in enumerate(tool_log, start=1):
                            lines.append(f"{i}. `./ep.py {tc['cmd']}`")
                        steps_placeholder.markdown("\n".join(lines))

                        function_response_parts.append(
                            types.Part.from_function_response(
                                name="search_archive",
                                response={"result": tool_output},
                            )
                        )

                        if loop_count >= max_loops:
                            break

                    if loop_count >= max_loops:
                        break
                    if not function_response_parts:
                        break

                    response = chat_session.send_message(function_response_parts)

                if not tool_log:
                    steps_placeholder.caption("No tool calls were needed for this response.")

                final_text = response.text or "No textual response."
                cited_bates = bates_from_text(final_text)
                discovered_unique = unique_preserve_order(discovered_bates)

                required_reads: list[str] = []
                required_reads.extend([b for b in cited_bates if b not in read_bates])
                extra_needed = max(0, MIN_FULL_DOC_READS - (len(read_bates) + len(required_reads)))
                if extra_needed > 0:
                    required_reads.extend(
                        [b for b in discovered_unique if b not in read_bates and b not in required_reads][:extra_needed]
                    )
                required_reads = unique_preserve_order(required_reads)

                if (
                    required_reads
                    and enforcement_rounds < 2
                    and loop_count < max_loops
                ):
                    enforcement_rounds += 1
                    forced_read_blocks: list[str] = []
                    for bates in required_reads:
                        if loop_count >= max_loops:
                            break
                        loop_count += 1
                        cmd = f"read {bates}"
                        status_container.update(label=f"🕵️‍♀️ Investigating... (step {loop_count})", expanded=True)
                        tool_output = search_archive(cmd)
                        tool_log.append({"cmd": cmd, "output": tool_output})
                        read_bates.add(bates)
                        discovered_bates.extend(bates_from_text(tool_output))
                        forced_read_blocks.append(f"[READ {bates}]\n{tool_output}")

                        lines = []
                        for i, tc in enumerate(tool_log, start=1):
                            lines.append(f"{i}. `./ep.py {tc['cmd']}`")
                        steps_placeholder.markdown("\n".join(lines))

                    if forced_read_blocks:
                        followup = (
                            "You must now produce the final answer using the full-document reads below. "
                            "Only cite documents that were read in full.\n\n"
                            + "\n\n".join(forced_read_blocks)
                        )
                        response = chat_session.send_message(followup)
                        continue

                break

            if loop_count >= max_loops:
                final_text += "\n\n[Stopped: reached max autonomous steps.]"
            downloads = build_downloads_from_response(final_text)
            display_text = format_assistant_message(final_text, downloads)
            cost = estimate_turn_cost(prompt, final_text, response)
            status_container.update(label="✅ Investigation Complete", state="complete", expanded=False)
            message_placeholder.markdown(display_text, unsafe_allow_html=True)
            render_cost_summary(cost)
            
            # Save to history
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
        except Exception as e:
            st.error(f"API Error: {e}")
            st.stop()

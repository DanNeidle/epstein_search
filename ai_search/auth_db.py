# © Dan Neidle and Tax Policy Associates 2026
import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Any

import streamlit as st

from ai_search.config import AUTH_COOKIE_NAME, PBKDF2_ITERATIONS, SESSION_TOKEN_BYTES, USERS_DB_PATH

try:
    import streamlit.components.v1 as components
except Exception:
    components = None


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
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


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_session(user_id: int) -> str:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    token_hash = hash_session_token(token)
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id)
            VALUES (?, ?)
            """,
            (token_hash, user_id),
        )
    return token


def authenticate_session_token(token: str) -> dict[str, Any] | None:
    token = token.strip()
    if not token:
        return None
    token_hash = hash_session_token(token)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.is_admin
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
                (token_hash,),
            )
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
    }


def revoke_auth_session(token: str) -> None:
    token = token.strip()
    if not token:
        return
    token_hash = hash_session_token(token)
    with get_db_connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def get_auth_cookie() -> str:
    try:
        ctx = getattr(st, "context", None)
        if ctx is None:
            return ""
        cookies = getattr(ctx, "cookies", None)
        if cookies is None:
            return ""
        value = cookies.get(AUTH_COOKIE_NAME, "")
        if isinstance(value, list):
            return str(value[0]).strip() if value else ""
        return str(value).strip()
    except Exception:
        return ""


def sync_auth_cookie(token: str | None) -> None:
    if components is None:
        return
    token = (token or "").strip()
    cookie_name_js = json.dumps(AUTH_COOKIE_NAME)
    token_js = json.dumps(token)
    if token:
        js = (
            "<script>"
            f"const name = {cookie_name_js};"
            f"const value = {token_js};"
            "document.cookie = `${name}=${encodeURIComponent(value)}; Max-Age=31536000; Path=/; SameSite=Lax`;"
            "</script>"
        )
    else:
        js = (
            "<script>"
            f"const name = {cookie_name_js};"
            "document.cookie = `${name}=; Max-Age=0; Path=/; SameSite=Lax`;"
            "</script>"
        )
    components.html(js, height=0, width=0)


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


def delete_user(username: str, acting_username: str) -> tuple[bool, str]:
    username = username.strip()
    acting_username = acting_username.strip()
    if not username:
        return False, "User not found."
    if username == acting_username:
        return False, "You cannot delete your own account."
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return False, "User not found."
        target_user_id = int(row["id"])
        target_is_admin = bool(row["is_admin"])
        if target_is_admin:
            admins = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE is_admin = 1"
            ).fetchone()
            if admins and int(admins["c"]) <= 1:
                return False, "Cannot delete the last admin user."

        conversation_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?",
                (target_user_id,),
            ).fetchall()
        ]
        for conv_id in conversation_ids:
            conn.execute(
                "DELETE FROM conversation_messages WHERE conversation_id = ?",
                (conv_id,),
            )
        conn.execute("DELETE FROM conversations WHERE user_id = ?", (target_user_id,))
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (target_user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
    return True, f"Deleted user '{username}'."

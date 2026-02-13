# © Dan Neidle and Tax Policy Associates 2026
import os
import shutil
from typing import Any

from ai_search.config import (
    ASSETS_DIR,
    DATA_DIR,
    SAFE_FILENAME_RE,
    STATIC_DIR,
    SYSTEM_PROMPT_PATH,
)


def load_system_prompt():
    if os.path.exists(SYSTEM_PROMPT_PATH):
        with open(SYSTEM_PROMPT_PATH, "r") as f:
            return f.read()
    else:
        return None


def ensure_static_file_for_download(download: dict[str, Any]) -> dict[str, Any]:
    file_name = str(download.get("name", "")).strip()
    if not file_name:
        bates = str(download.get("bates", "")).strip()
        if bates:
            file_name = f"{bates}.pdf"
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
    explicit_path = str(download.get("path", "")).strip()
    if explicit_path:
        explicit_base = os.path.basename(explicit_path)
        if explicit_base == file_name and (
            is_under(DATA_DIR, explicit_path)
            or is_under(ASSETS_DIR, explicit_path)
            or is_under(STATIC_DIR, explicit_path)
        ):
            candidate_paths.append(explicit_path)
    candidate_paths.append(os.path.join(ASSETS_DIR, file_name))
    candidate_paths.append(os.path.join(DATA_DIR, file_name))

    for candidate in candidate_paths:
        if (
            candidate
            and os.path.isfile(candidate)
            and (
                is_under(DATA_DIR, candidate)
                or is_under(ASSETS_DIR, candidate)
                or is_under(STATIC_DIR, candidate)
            )
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
            updated_msg["downloads"] = [
                ensure_static_file_for_download(d) for d in downloads if isinstance(d, dict)
            ]
        normalized.append(updated_msg)
    return normalized

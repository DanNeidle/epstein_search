# © Dan Neidle and Tax Policy Associates 2026
import html
import json
import os
import re
import shutil
from typing import Any, cast
from urllib.parse import quote

import streamlit as st

from ai_search.config import (
    ASSETS_DIR,
    BATES_EXACT_RE,
    BATES_RE,
    CONTROL_CHARS_RE,
    DATA_DIR,
    DOC_URL_RE,
    SIST2_URL,
    STATIC_DIR,
)

VERIFICATION_INFO_TOOLTIP = (
    "The Verification Agent runs after the report is completed, and checks its findings against "
    "the source documents. It's intended to reduce hallucinations, although it won't be perfect"
)


def _app_static_href(file_name: str) -> str:
    base_path = ""
    try:
        base_path = str(st.get_option("server.baseUrlPath") or "").strip("/")
    except Exception:
        base_path = ""
    quoted = quote(file_name)
    if base_path:
        return f"/{base_path}/app/static/{quoted}"
    return f"/app/static/{quoted}"


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

    for bates in dict.fromkeys(BATES_RE.findall(text)):
        source_path = os.path.join(DATA_DIR, f"{bates}.pdf")
        add_download_from_source(source_path)

    return downloads


def build_inline_download_anchor(path: str, file_name: str, label: str) -> str:
    safe_label = html.escape(str(label or ""))
    if not file_name:
        return safe_label

    static_path = os.path.join(STATIC_DIR, file_name)
    if not os.path.isfile(static_path):
        return safe_label

    href = _app_static_href(file_name)
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'


def is_valid_structured_citation(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    required = {"source_doc_id", "page_number", "exact_quote_snippet"}
    if not required.issubset(set(obj.keys())):
        return False
    source_doc_id = str(obj.get("source_doc_id", "")).strip()
    if not source_doc_id:
        return False
    quote_snippet = str(obj.get("exact_quote_snippet", "")).strip()
    if not quote_snippet:
        return False
    return True


def resolve_source_anchor_html(source_doc_id: str, downloads: list[dict[str, str]]) -> str:
    source = str(source_doc_id).strip()
    source_up = source.upper()
    if BATES_EXACT_RE.fullmatch(source_up):
        for dl in downloads:
            if str(dl.get("bates", "")).upper() != source_up:
                continue
            path = str(dl.get("path", ""))
            name = str(dl.get("name", ""))
            if path and name:
                return build_inline_download_anchor(path, name, source_up)
        source = source_up

    if re.fullmatch(r"[0-9a-f]{32}", source.lower()):
        href = f"{SIST2_URL}/f/{source.lower()}"
        safe_label = html.escape(source)
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'

    return html.escape(source)


def render_structured_citation_html(citation: dict[str, Any], downloads: list[dict[str, str]]) -> str:
    source_doc_id = str(citation.get("source_doc_id", "")).strip()
    page_number = str(citation.get("page_number", "")).strip()
    quote_snippet = CONTROL_CHARS_RE.sub("", str(citation.get("exact_quote_snippet", "")).strip())

    source_html = resolve_source_anchor_html(source_doc_id, downloads)
    page_html = html.escape(page_number or "?")
    quote_html = html.escape(quote_snippet)
    return (
        "<details class='tpa-citation-inline tpa-quote-toggle'>"
        "<summary title='Show or hide exact quote' aria-label='Toggle exact quote'>"
        "<span class='tpa-citation-meta'>[citation: "
        f"{source_html}, p.{page_html}]"
        "</span>"
        "<span class='tpa-quote-icon'>❝❞</span>"
        "</summary>"
        f"<span class='tpa-quote-body'>{quote_html}</span>"
        "</details>"
    )


def extract_structured_citations(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    citations: list[dict[str, Any]] = []
    i = 0
    length = len(text)

    while i < length:
        if text[i] != "{":
            i += 1
            continue

        try:
            obj, end_offset = decoder.raw_decode(text[i:])
        except Exception:
            i += 1
            continue

        if is_valid_structured_citation(obj):
            citations.append(cast(dict[str, Any], obj))
            i += end_offset
            continue

        i += 1

    return citations


def extract_structured_citations_with_placeholders(
    text: str,
    downloads: list[dict[str, str]],
) -> tuple[str, dict[str, str]]:
    decoder = json.JSONDecoder()
    out: list[str] = []
    placeholders: dict[str, str] = {}
    i = 0
    last = 0
    length = len(text)
    cite_idx = 0

    while i < length:
        if text[i] != "{":
            i += 1
            continue

        try:
            obj, end_offset = decoder.raw_decode(text[i:])
        except Exception:
            i += 1
            continue

        if not is_valid_structured_citation(obj):
            i += 1
            continue

        out.append(text[last:i])
        placeholder = f"@@TPA_CIT_{cite_idx}@@"
        placeholders[placeholder] = render_structured_citation_html(cast(dict[str, Any], obj), downloads)
        out.append(placeholder)
        cite_idx += 1
        i += end_offset
        last = i

    out.append(text[last:])
    return "".join(out), placeholders


def replace_bates_mentions_outside_html(text: str, downloads: list[dict[str, str]]) -> str:
    formatted = text
    parts = re.split(r"(<[^>]+>)", formatted)
    for idx, part in enumerate(parts):
        if not part or part.startswith("<"):
            continue
        updated = part
        for dl in downloads:
            path = dl.get("path", "")
            name = dl.get("name", "document.pdf")
            bates = dl.get("bates", os.path.splitext(name)[0])
            anchor = build_inline_download_anchor(path, name, bates)
            updated = re.sub(rf"\b{re.escape(bates)}\b", anchor, updated)
        parts[idx] = updated
    return "".join(parts)


def preprocess_assistant_markdown(text: str) -> str:
    cleaned = str(text or "").strip()

    if cleaned.startswith("```") and re.search(r"```\s*$", cleaned):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*\n?", "", cleaned, count=1)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned, count=1).strip()

    cleaned = re.sub(
        r"`\s*(\{[^`]*\"source_doc_id\"[^`]*\})\s*`",
        r"\1",
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned


def highlight_verification_modifications(text: str) -> str:
    highlighted = text

    def wrap_auditor_note(match: re.Match[str]) -> str:
        block = match.group(1)
        return f"<span class='tpa-verification-edit'><strong>{html.escape(block)}</strong></span>"

    highlighted = re.sub(
        r"\*\*(\[(?:Auditor Note|SECTION REMOVED):[^\]]+\])\*\*",
        wrap_auditor_note,
        highlighted,
        flags=re.IGNORECASE,
    )
    return highlighted


def add_verification_report_info_icon(text: str) -> str:
    tooltip = html.escape(VERIFICATION_INFO_TOOLTIP, quote=True)
    title_html = (
        "<span class='tpa-verification-report-title'>"
        "<strong>Verification Report</strong>"
        f"<span class='tpa-verification-info' title='{tooltip}' data-tooltip='{tooltip}' aria-label='{tooltip}' tabindex='0'>ⓘ</span>"
        "</span>"
    )
    return re.sub(
        r"(?im)^\s*(?:\*\*)?verification report(?:\*\*)?\s*$",
        title_html,
        text,
    )


def normalize_investigation_log_section(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_log = False
    in_docs = False

    for line in lines:
        stripped = line.strip()

        if re.match(r"^\s{0,3}(?:#{1,6}\s*)?investigation log\s*:?\s*$", stripped, flags=re.IGNORECASE):
            in_log = True
            in_docs = False
            out.append(line)
            continue

        if in_log and re.match(r"^\s{0,3}#{1,6}\s+\S", stripped):
            in_log = False
            in_docs = False

        if in_log:
            line = re.sub(r"`([^`\n]+)`", r"***\1***", line)
            normalized = re.sub(r"^[\-\*\s]+", "", stripped).replace("*", "").lower()
            if normalized.startswith("documents read:"):
                in_docs = True
            elif normalized.startswith("searched terms:") or normalized.startswith("negative results:"):
                in_docs = False

            doc_candidate = line.strip()
            if (
                in_docs
                and doc_candidate
                and not doc_candidate.startswith("-")
                and not doc_candidate.startswith("*")
                and not re.match(r"(?i)^documents read:", re.sub(r"^\s+", "", doc_candidate))
            ):
                if doc_candidate.startswith("<a ") or doc_candidate.startswith("[") or re.match(r"^EFTA\d{8}\b", doc_candidate):
                    line = f"- {doc_candidate}"

        out.append(line)

    return "\n".join(out)


def format_assistant_message(text: str, downloads: list[dict[str, str]]) -> str:
    formatted = preprocess_assistant_markdown(text)
    formatted = highlight_verification_modifications(formatted)
    formatted = add_verification_report_info_icon(formatted)
    formatted = normalize_investigation_log_section(formatted)
    formatted = sanitize_response_links(formatted)
    formatted = formatted.replace("(download below)", "")
    formatted, citation_placeholders = extract_structured_citations_with_placeholders(formatted, downloads)
    formatted = replace_bates_mentions_outside_html(formatted, downloads)
    for token, html_block in citation_placeholders.items():
        formatted = formatted.replace(token, html_block)
    return formatted

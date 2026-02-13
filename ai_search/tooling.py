# © Dan Neidle and Tax Policy Associates 2026
import html
import inspect
import json
import os
import re
from typing import Any, cast

import streamlit as st

from ai_search.config import (
    BATES_RE,
    CONTROL_CHARS_RE,
    DEFAULT_HIGHLIGHT_FRAGMENT_SIZE,
    DEFAULT_HIGHLIGHT_FRAGMENTS,
    DEFAULT_LIMIT,
    INPUT_RATE_GT_200K,
    INPUT_RATE_LE_200K,
    INTENT_BLOCK_RE,
    MAX_INTENT_BODY_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    MIN_FULL_DOC_READS,
    MODEL_NAME,
    OUTPUT_RATE_GT_200K,
    OUTPUT_RATE_LE_200K,
    CACHE_RATE_GT_200K,
    CACHE_RATE_LE_200K,
    COST_PROMPT_LARGE_THRESHOLD,
    DOC_RESULT_SUMMARY_RE,
    ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT,
)
from ai_search.es_client import get_es_client

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

    out_total = out_tokens + thoughts_tokens

    if in_tokens == 0 and out_total == 0:
        in_tokens, out_total = estimate_tokens_fallback(prompt, final_text)
        thoughts_tokens = 0
        cached_tokens = 0

    prompt_is_large = in_tokens > COST_PROMPT_LARGE_THRESHOLD
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


def summarize_tool_output_for_ui(
    output: Any,
    *,
    max_lines: int = 10,
    max_chars: int = 1200,
) -> tuple[str, bool]:
    if isinstance(output, str):
        text = output
    else:
        try:
            text = json.dumps(output, ensure_ascii=False, indent=2)
        except Exception:
            text = str(output)

    clean = CONTROL_CHARS_RE.sub("", text or "")

    def clamp_line(line: str, width: int = 160) -> str:
        compact = " ".join(line.strip().split())
        if len(compact) <= width:
            return compact
        return compact[: width - 3].rstrip() + "..."

    lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
    header = ""
    if lines and re.match(r"^\[\d+\s+of\s+.*\s+results\]$", lines[0].strip()):
        header = lines[0].strip()

    docs: list[dict[str, str]] = []
    doc_index = -1
    for raw in lines:
        m = DOC_RESULT_SUMMARY_RE.match(raw.strip())
        if m:
            docs.append(
                {
                    "name": m.group("name").strip(),
                    "pages": m.group("pages").strip(),
                    "link": m.group("link").strip(),
                    "snippet": "",
                }
            )
            doc_index = len(docs) - 1
            continue
        if doc_index >= 0 and raw.strip().startswith(">"):
            if not docs[doc_index]["snippet"]:
                snippet = raw.strip().lstrip(">") .strip()
                docs[doc_index]["snippet"] = clamp_line(snippet, width=130)

    if docs:
        max_docs = 3
        out_lines: list[str] = []
        if header:
            out_lines.append(header)
        out_lines.append("Top matches:")
        for i, doc in enumerate(docs[:max_docs], start=1):
            out_lines.append(f"{i}. {doc['name']} ({doc['pages']} pages)")
            out_lines.append(f"   {doc['link']}")
            if doc["snippet"]:
                out_lines.append(f"   {doc['snippet']}")
        truncated = len(docs) > max_docs
        if truncated:
            out_lines.append(f"... and {len(docs) - max_docs} more results.")
        return "\n".join(out_lines).strip(), truncated

    truncated = False
    if len(clean) > max_chars:
        clean = clean[:max_chars]
        truncated = True
    raw_lines = [clamp_line(ln, width=160) for ln in clean.splitlines() if ln.strip()]
    if len(raw_lines) > max_lines:
        raw_lines = raw_lines[:max_lines]
        truncated = True
    if truncated:
        raw_lines.append("...[truncated for readability]...")
    return "\n".join(raw_lines).strip(), truncated


def render_tool_preview_block(text: str) -> None:
    safe = html.escape(text or "")
    st.markdown(
        f"<pre class='tpa-tool-preview'>{safe}</pre>",
        unsafe_allow_html=True,
    )


def validate_intent_block(value: Any) -> tuple[str, str, bool, str]:
    raw = CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    if not raw:
        return "", "", False, "missing required `intent`; include `<intent>...</intent>` in every tool call."

    match = INTENT_BLOCK_RE.fullmatch(raw)
    if match is None:
        return "", "", False, "invalid `intent` format; use exactly `<intent>...</intent>`."

    body = " ".join(match.group("body").split())
    if not body:
        return "", "", False, "empty `intent`; include a short rationale."
    if len(body) > MAX_INTENT_BODY_CHARS:
        return "", "", False, f"`intent` text too long; keep it under {MAX_INTENT_BODY_CHARS} characters."

    normalized = f"<intent>{body}</intent>"
    return normalized, body, True, ""


def summarize_intent_for_ui(intent_block: str, max_chars: int = 96) -> str:
    _, body, ok, _ = validate_intent_block(intent_block)
    if not ok:
        return "Missing or invalid intent"
    if len(body) > max_chars:
        body = body[: max_chars - 3].rstrip() + "..."
    return body


def _escape_markdown_inline(text: str) -> str:
    escaped = str(text or "")
    escaped = escaped.replace("\\", "\\\\")
    escaped = escaped.replace("*", r"\*")
    escaped = escaped.replace("_", r"\_")
    escaped = escaped.replace("`", r"\`")
    return escaped


def render_steps_markdown(tool_log: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, tc in enumerate(tool_log, start=1):
        tc_name = str(tc.get("tool", "tool"))
        tc_args = tc.get("args", {})
        tc_safe_args = tc_args if isinstance(tc_args, dict) else {}
        intent_block = str(tc.get("intent", "")).strip()
        intent_preview = summarize_intent_for_ui(intent_block)
        signature = format_tool_call_signature(tc_name, tc_safe_args, include_intent=False)
        lines.append(f"{i}. **{_escape_markdown_inline(intent_preview)}**")
        lines.append(f"   *{_escape_markdown_inline(signature)}*")
    return "\n".join(lines)


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


def _coerce_terms(value: Any) -> list[str]:
    if isinstance(value, list):
        source = value
    elif value is None:
        source = []
    else:
        source = [value]
    terms: list[str] = []
    for item in source:
        text = str(item).strip()
        if text:
            terms.append(text)
    return terms


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        source = value
    elif value is None:
        source = []
    else:
        source = [value]
    out: list[str] = []
    for item in source:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _normalize_tool_result(tool_result: Any) -> dict[str, Any]:
    result: dict[str, Any]
    if isinstance(tool_result, dict):
        result = {str(k): v for k, v in tool_result.items()}
    else:
        result = {"result": str(tool_result)}
    text = result.get("result", "")
    if isinstance(text, str):
        output_text = text
    else:
        try:
            output_text = json.dumps(text, ensure_ascii=False, indent=2)
        except Exception:
            output_text = str(text)
    if len(output_text) > MAX_TOOL_OUTPUT_CHARS:
        output_text = output_text[:MAX_TOOL_OUTPUT_CHARS] + "\n...[Output Truncated]..."
    result["result"] = output_text
    if not isinstance(result.get("documents"), list):
        result["documents"] = []
    return result


def es_search(
    terms: list[str],
    limit: int = DEFAULT_LIMIT,
    fuzzy: bool = False,
    cooccur: bool = False,
    exclude: list[str] | None = None,
    min_pages: int | None = None,
    max_pages: int | None = None,
    fragment_size: int = DEFAULT_HIGHLIGHT_FRAGMENT_SIZE,
    fragments: int = DEFAULT_HIGHLIGHT_FRAGMENTS,
    intent: str = "",
) -> dict[str, Any]:
    return get_es_client().search(
        terms=_coerce_terms(terms),
        limit=limit,
        fuzzy=fuzzy,
        cooccur=cooccur,
        exclude=_coerce_str_list(exclude),
        min_pages=min_pages,
        max_pages=max_pages,
        fragment_size=fragment_size,
        fragments=fragments,
    )


def es_count(
    terms: list[str],
    fuzzy: bool = False,
    cooccur: bool = False,
    intent: str = "",
) -> dict[str, Any]:
    return get_es_client().count(
        terms=_coerce_terms(terms),
        fuzzy=fuzzy,
        cooccur=cooccur,
    )


def es_read(
    bates: str,
    max_chars: int | None = None,
    intent: str = "",
) -> dict[str, Any]:
    return get_es_client().read(bates=str(bates), max_chars=max_chars)


def es_read_batch(
    bates_list: list[str],
    max_chars_total: int = ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT,
    intent: str = "",
) -> dict[str, Any]:
    return get_es_client().read_batch(
        bates_list=_coerce_terms(bates_list),
        max_chars_total=max_chars_total,
    )


def es_list(
    query: str,
    fuzzy: bool = False,
    intent: str = "",
) -> dict[str, Any]:
    return get_es_client().list_documents(query=str(query), fuzzy=fuzzy)


TOOL_HANDLERS: dict[str, Any] = {
    "es_search": es_search,
    "es_count": es_count,
    "es_read": es_read,
    "es_read_batch": es_read_batch,
    "es_list": es_list,
}
tools_def = list(TOOL_HANDLERS.values())


def format_tool_call_signature(tool_name: str, args: dict[str, Any], include_intent: bool = True) -> str:
    display_args = dict(args or {})
    if not include_intent:
        display_args.pop("intent", None)
    if not display_args:
        return f"{tool_name}()"
    try:
        args_text = json.dumps(display_args, ensure_ascii=False, sort_keys=True)
    except Exception:
        args_text = str(display_args)
    return f"{tool_name}({args_text})"


def invoke_tool(tool_name: str, args: Any) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return _normalize_tool_result(
            {"result": f"Tool Execution Error: unsupported tool '{tool_name}'."}
        )

    safe_args = args if isinstance(args, dict) else {}
    try:
        sig = inspect.signature(handler)
        accepted_kwargs = {
            name: safe_args[name]
            for name in sig.parameters
            if name in safe_args
        }
        result = handler(**accepted_kwargs)
        return _normalize_tool_result(result)
    except TypeError as e:
        return _normalize_tool_result({"result": f"Tool Execution Error: invalid arguments ({e})."})
    except Exception as e:
        return _normalize_tool_result({"result": f"Tool Execution Error: {e}"})


def read_bates_from_tool_call(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name != "es_read":
        return None
    bates = str(args.get("bates", "")).strip().upper()
    if BATES_RE.fullmatch(bates):
        return bates
    return None


def bates_from_tool_result(result: dict[str, Any]) -> list[str]:
    discovered: list[str] = []
    documents = result.get("documents", [])
    if isinstance(documents, list):
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            name = os.path.basename(str(doc.get("name", "")))
            stem, _ = os.path.splitext(name)
            stem_up = stem.upper()
            if BATES_RE.fullmatch(stem_up):
                discovered.append(stem_up)
    discovered.extend(bates_from_text(str(result.get("result", ""))))
    return unique_preserve_order(discovered)


def bates_from_tool_documents(result: dict[str, Any]) -> list[str]:
    discovered: list[str] = []
    documents = result.get("documents", [])
    if isinstance(documents, list):
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            name = os.path.basename(str(doc.get("name", "")))
            stem, _ = os.path.splitext(name)
            stem_up = stem.upper()
            if BATES_RE.fullmatch(stem_up):
                discovered.append(stem_up)
    return unique_preserve_order(discovered)


def build_system_instruction(base_prompt: str) -> str:
    return f"""
    {base_prompt}

    IMPORTANT ARCHITECTURE NOTE:
    You are running in an autonomous loop.
    1. When the user asks a question, DO NOT just do one search and answer.
    2. You must autonomously run MULTIPLE searches, cross-references, and document reads.
    3. Keep calling the ES tools (`es_search`, `es_count`, `es_read`, `es_read_batch`, `es_list`) until you have a watertight case.
    4. Only when you have gathered all evidence, output your final answer as text.
    5. Formulate links using the markdown provided by the tool output.
    6. Every tool call MUST include an `intent` argument formatted exactly as `<intent>...</intent>`.
    """


def create_chat_session(api_key_value: str, base_prompt: str, max_remote_calls: int):
    if genai is None or types is None:
        raise RuntimeError("google-genai is not installed. Run: pip install google-genai")

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

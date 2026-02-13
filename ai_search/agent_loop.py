# © Dan Neidle and Tax Policy Associates 2026
import math
import re
from typing import Any

from ai_search.citations import extract_structured_citations
from ai_search.config import CONTROL_CHARS_RE
from ai_search.config import DEEP_SWEEP_COUNT_THRESHOLD
from ai_search.config import DEEP_SWEEP_LIMIT_MIN
from ai_search.config import DEEP_SWEEP_MAX_BATCH_DOCS
from ai_search.config import DEEP_SWEEP_MIN_BATCH_DOCS
from ai_search.config import DEEP_SWEEP_SMALL_BATCH_RETRIES
from ai_search.config import DEEP_SWEEP_TARGET_FRACTION
from ai_search.config import MAX_QUOTE_VALIDATION_FAILURES
from ai_search.config import MIN_FULL_DOC_READS
from ai_search.es_client import fetch_document_content_for_source, index_documents_from_tool_result
from ai_search.tooling import (
    TOOL_HANDLERS,
    _normalize_tool_result,
    bates_from_tool_documents,
    bates_from_text,
    bates_from_tool_result,
    invoke_tool,
    read_bates_from_tool_call,
    render_steps_markdown,
    summarize_tool_output_for_ui,
    types,
    unique_preserve_order,
    validate_intent_block,
)
class QuoteValidationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        draft_text: str = "",
        tool_log: list[dict[str, Any]] | None = None,
        response: Any = None,
        loop_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.draft_text = draft_text
        self.tool_log = tool_log or []
        self.response = response
        self.loop_count = loop_count


SWEEP_RATIONALE_RE = re.compile(r"(?im)^\s*sweep rationale\s*:\s*(.+)$")


def _recommended_sweep_target(total_docs: int) -> int:
    if total_docs <= 0:
        return DEEP_SWEEP_MIN_BATCH_DOCS
    target = int(math.ceil(total_docs * DEEP_SWEEP_TARGET_FRACTION))
    target = max(DEEP_SWEEP_MIN_BATCH_DOCS, target)
    target = min(DEEP_SWEEP_MAX_BATCH_DOCS, target)
    return target


def _quote_matches_source_text(quote_text: str, source_text: str) -> bool:
    quote_clean = CONTROL_CHARS_RE.sub("", str(quote_text or "")).strip()
    source_clean = CONTROL_CHARS_RE.sub("", str(source_text or ""))
    if not quote_clean or not source_clean:
        return False

    if quote_clean in source_clean:
        return True

    quote_compact = " ".join(quote_clean.split())
    if not quote_compact:
        return False
    pattern = re.sub(r"\\\s+", r"\\s+", re.escape(quote_compact))
    return re.search(pattern, source_clean, flags=re.IGNORECASE) is not None


def _validate_structured_quote_snippets(
    text: str,
    source_cache: dict[str, str],
) -> tuple[bool, list[str]]:
    citations = extract_structured_citations(text)
    if not citations:
        return True, []

    failures: list[str] = []
    for idx, citation in enumerate(citations, start=1):
        source_doc_id = str(citation.get("source_doc_id", "")).strip()
        quote_snippet = str(citation.get("exact_quote_snippet", "")).strip()
        if not source_doc_id or not quote_snippet:
            continue

        if source_doc_id not in source_cache:
            source_cache[source_doc_id] = fetch_document_content_for_source(source_doc_id)
        source_text = source_cache.get(source_doc_id, "")
        matched = _quote_matches_source_text(quote_snippet, source_text)
        quote_preview = " ".join(quote_snippet.split())
        if len(quote_preview) > 140:
            quote_preview = quote_preview[:137].rstrip() + "..."

        if not matched:
            failures.append(
                f"- source `{source_doc_id}` quote not found: \"{quote_preview}\""
            )

    return len(failures) == 0, failures


def run_autonomous_loop(
    prompt: str,
    chat_session: Any,
    max_loops: int,
    status_container: Any,
    steps_placeholder: Any,
) -> tuple[str, list[dict[str, Any]], Any, int]:
    tool_log = []
    loop_count = 0
    read_bates: set[str] = set()
    discovered_bates: list[str] = []
    enforcement_rounds = 0
    deep_sweep_enforcement_rounds = 0
    quote_validation_failures = 0
    source_text_cache: dict[str, str] = {}
    deep_sweep_required = False
    deep_sweep_reasons: list[str] = []
    deep_sweep_total_observed = 0
    deep_sweep_batch_reads: set[str] = set()
    deep_sweep_waived = False
    response = chat_session.send_message(prompt)

    while True:
        while response.function_calls and loop_count < max_loops:
            function_response_parts = []
            for fn in response.function_calls:
                tool_name = str(getattr(fn, "name", ""))
                if tool_name not in TOOL_HANDLERS:
                    continue
                args = getattr(fn, "args", None)
                safe_args = dict(args) if isinstance(args, dict) else {}

                loop_count += 1
                status_container.update(label=f"Investigating... (step {loop_count})", expanded=True)
                intent_block, _, intent_ok, intent_error = validate_intent_block(safe_args.get("intent"))
                if not intent_ok:
                    tool_result = _normalize_tool_result(
                        {"result": f"Tool Execution Error: {intent_error}", "documents": []}
                    )
                else:
                    safe_args["intent"] = intent_block
                    tool_result = invoke_tool(tool_name, safe_args)
                tool_output = str(tool_result.get("result", ""))
                output_preview, output_truncated = summarize_tool_output_for_ui(
                    tool_output, max_lines=12, max_chars=1400
                )
                tool_log.append(
                    {
                        "tool": tool_name,
                        "args": safe_args,
                        "intent": intent_block,
                        "output": tool_output,
                        "output_preview": output_preview,
                        "output_truncated_for_ui": output_truncated,
                    }
                )
                index_documents_from_tool_result(tool_result)

                read_bates_cmd = read_bates_from_tool_call(tool_name, safe_args)
                if read_bates_cmd:
                    if tool_result.get("documents"):
                        read_bates.add(read_bates_cmd)
                elif tool_name == "es_read_batch":
                    batch_bates = bates_from_tool_documents(tool_result)
                    for bates in batch_bates:
                        read_bates.add(bates)
                        deep_sweep_batch_reads.add(bates)
                elif tool_name == "es_count":
                    try:
                        count_val = int(tool_result.get("count", 0))
                    except Exception:
                        count_val = 0
                    if count_val > deep_sweep_total_observed:
                        deep_sweep_total_observed = count_val
                    if count_val > DEEP_SWEEP_COUNT_THRESHOLD:
                        deep_sweep_required = True
                        deep_sweep_reasons.append(
                            f"es_count reported {count_val} matches (> {DEEP_SWEEP_COUNT_THRESHOLD})."
                        )
                elif tool_name == "es_search":
                    total_obj = tool_result.get("total", {})
                    try:
                        total_val = int(total_obj.get("value", 0)) if isinstance(total_obj, dict) else 0
                    except Exception:
                        total_val = 0
                    if total_val > deep_sweep_total_observed:
                        deep_sweep_total_observed = total_val
                    docs = tool_result.get("documents", [])
                    returned = len(docs) if isinstance(docs, list) else 0
                    try:
                        requested_limit = int(safe_args.get("limit", returned or 0))
                    except Exception:
                        requested_limit = returned
                    if total_val > DEEP_SWEEP_COUNT_THRESHOLD and returned > 0:
                        deep_sweep_required = True
                        if requested_limit < DEEP_SWEEP_LIMIT_MIN or returned < total_val:
                            deep_sweep_reasons.append(
                                f"es_search returned {returned} of {total_val} with limit={requested_limit}."
                            )
                discovered_bates.extend(bates_from_tool_result(tool_result))

                steps_placeholder.markdown(render_steps_markdown(tool_log))

                function_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=tool_result,
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
                tool_name = "es_read"
                tool_args = {
                    "bates": bates,
                    "intent": f"<intent>Mandatory full-document verification read for {bates}</intent>",
                }
                status_container.update(label=f"Investigating... (step {loop_count})", expanded=True)
                intent_block, _, intent_ok, intent_error = validate_intent_block(tool_args.get("intent"))
                if not intent_ok:
                    tool_result = _normalize_tool_result(
                        {"result": f"Tool Execution Error: {intent_error}", "documents": []}
                    )
                else:
                    tool_args["intent"] = intent_block
                    tool_result = invoke_tool(tool_name, tool_args)
                tool_output = str(tool_result.get("result", ""))
                output_preview, output_truncated = summarize_tool_output_for_ui(
                    tool_output, max_lines=12, max_chars=1400
                )
                tool_log.append(
                    {
                        "tool": tool_name,
                        "args": tool_args,
                        "intent": intent_block,
                        "output": tool_output,
                        "output_preview": output_preview,
                        "output_truncated_for_ui": output_truncated,
                    }
                )
                index_documents_from_tool_result(tool_result)
                if tool_result.get("documents"):
                    read_bates.add(bates)
                discovered_bates.extend(bates_from_tool_result(tool_result))
                forced_read_blocks.append(f"[READ {bates}]\n{tool_output}")

                steps_placeholder.markdown(render_steps_markdown(tool_log))

            if forced_read_blocks:
                followup = (
                    "You must now produce the final answer using the full-document reads below. "
                    "Only cite documents that were read in full.\n\n"
                    + "\n\n".join(forced_read_blocks)
                )
                response = chat_session.send_message(followup)
                continue

        if not deep_sweep_waived:
            rationale_match = SWEEP_RATIONALE_RE.search(final_text or "")
            if rationale_match and rationale_match.group(1).strip():
                deep_sweep_waived = True

        sweep_total = deep_sweep_total_observed
        batch_read_count = len(deep_sweep_batch_reads)
        sweep_target = _recommended_sweep_target(sweep_total)

        if (
            deep_sweep_required
            and not deep_sweep_waived
            and sweep_total > DEEP_SWEEP_COUNT_THRESHOLD
            and batch_read_count < sweep_target
            and deep_sweep_enforcement_rounds < DEEP_SWEEP_SMALL_BATCH_RETRIES
            and loop_count < max_loops
        ):
            deep_sweep_enforcement_rounds += 1
            reason_text = "\n".join(unique_preserve_order(deep_sweep_reasons)) or (
                "High-volume result set detected."
            )
            correction = (
                f"The search came back with {sweep_total} documents, but you only did a batch for "
                f"{batch_read_count}. You should seriously consider doing a much larger batch unless "
                "you have good reason to. Please either proceed with a large batch or provide a "
                "reasoned explanation why you aren't, and then move into the next step.\n"
                f"{reason_text}\n"
                f"Recommended sweep target for this case: at least {sweep_target} documents in batch "
                f"(use es_search limit={DEEP_SWEEP_LIMIT_MIN}+ and then es_read_batch).\n"
                "If you choose not to increase the batch, include a line in your response starting "
                "with: Sweep rationale: ..."
            )
            response = chat_session.send_message(correction)
            continue

        quotes_ok, quote_failures = _validate_structured_quote_snippets(
            final_text, source_text_cache
        )
        if not quotes_ok:
            quote_validation_failures += 1
            if quote_validation_failures >= MAX_QUOTE_VALIDATION_FAILURES:
                raise QuoteValidationError(
                    "Quote verification failed after 3 attempts. Showing the latest draft as unverified.",
                    draft_text=final_text,
                    tool_log=tool_log,
                    response=response,
                    loop_count=loop_count,
                )
            correction = (
                "Quote verification failed. At least one exact quote snippet does not appear in the "
                "cited source document text.\n"
                f"Attempt {quote_validation_failures} of {MAX_QUOTE_VALIDATION_FAILURES - 1} retries.\n"
                "Fix the quoted snippets and regenerate the answer.\n\n"
                "Failed checks:\n"
                + "\n".join(quote_failures)
            )
            response = chat_session.send_message(correction)
            continue

        break

    return final_text, tool_log, response, loop_count

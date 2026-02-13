# © Dan Neidle and Tax Policy Associates 2026
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

import streamlit as st

from ai_search.config import (
    BATES_RE,
    CONTROL_CHARS_RE,
    DEFAULT_HIGHLIGHT_FRAGMENT_SIZE,
    DEFAULT_HIGHLIGHT_FRAGMENTS,
    DEFAULT_LIMIT,
    DEEP_SWEEP_LIMIT_MIN,
    DEEP_SWEEP_RESULT_THRESHOLD,
    ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT,
    ES_READ_MAX_CHARS_MAX,
    ES_READ_MAX_CHARS_MIN,
    ES_SEARCH_FRAGMENTS_MAX,
    ES_SEARCH_FRAGMENTS_MIN,
    ES_SEARCH_FRAGMENT_SIZE_MAX,
    ES_SEARCH_FRAGMENT_SIZE_MIN,
    ES_SEARCH_LIMIT_MAX,
    ES_SEARCH_LIMIT_MIN,
    ES_INDEX,
    ES_URL,
    LIST_PAGE_SIZE,
    SIST2_URL,
    SOURCE_DOC_ID_RE,
    DATA_DIR,
)


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class ElasticsearchArchiveClient:
    """Read-only Elasticsearch adapter used by the chat agent tools.

    This class ports query construction and result-shaping behavior from `ep.py`
    into in-process methods so the app avoids subprocess execution and shell-like
    command strings.
    """

    def __init__(
        self,
        *,
        es_url: str,
        es_index: str,
        sist2_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.es_url = es_url.rstrip("/")
        self.es_index = es_index.strip("/")
        self.sist2_url = sist2_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _sanitize_text(self, text: Any) -> str:
        if text is None:
            return ""
        return CONTROL_CHARS_RE.sub("", str(text))

    def _es_endpoint(self, path: str) -> str:
        return f"{self.es_url}/{self.es_index}{path}"

    def _request_json(self, url: str, *, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"Elasticsearch HTTP {e.code}: {detail[:500]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Error connecting to Elasticsearch at {self.es_url}: {e}") from e

    def _search(self, body: dict[str, Any], params: str = "") -> dict[str, Any]:
        return self._request_json(
            self._es_endpoint(f"/_search{params}"),
            method="POST",
            body=body,
        )

    def _count(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json(
            self._es_endpoint("/_count"),
            method="POST",
            body=body,
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            root = self._request_json(self.es_url, method="GET")
            cluster = self._sanitize_text(root.get("cluster_name", "unknown-cluster"))
            return True, f"Elasticsearch reachable: {self.es_url} ({cluster}) / index {self.es_index}"
        except Exception as e:
            return False, f"Elasticsearch unavailable: {e}"

    def doc_link(self, es_id: str) -> str:
        return f"{self.sist2_url}/f/{es_id}"

    def normalize_bates(self, value: str) -> str:
        base = os.path.basename(str(value).strip())
        stem, ext = os.path.splitext(base)
        if ext.lower() == ".pdf":
            base = stem
        return base.upper()

    def _content_hash(self, text: str) -> str:
        normalized = "".join(text[:500].lower().split())
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def _build_content_query(self, terms: list[str], fuzzy: bool = False) -> dict[str, Any]:
        query_text = " ".join(terms)
        if fuzzy:
            return {"match": {"content": {"query": query_text, "fuzziness": "AUTO"}}}
        return {"match": {"content": query_text}}

    def _build_exclude_filter(self, exclude: list[str] | None) -> list[dict[str, Any]]:
        if not exclude:
            return []

        filters: list[dict[str, Any]] = []
        for raw_term in exclude:
            term = self._sanitize_text(raw_term).strip()
            if not term:
                continue

            normalized = self.normalize_bates(term)
            if BATES_RE.fullmatch(normalized):
                filters.append({"term": {"name.keyword": normalized}})

            # Also exclude docs where the phrase appears in file name or OCR content.
            filters.append({"match_phrase": {"name": term}})
            filters.append({"match_phrase": {"content": term}})

        return filters

    def _build_list_query_text(self, raw_query: str, fuzzy: bool = False) -> str:
        query_text = raw_query.strip()
        if not fuzzy:
            return query_text

        tokens = re.findall(r'"[^"]+"|\S+', query_text)
        fuzzy_tokens: list[str] = []
        for token in tokens:
            if token.startswith('"') and token.endswith('"'):
                fuzzy_tokens.append(token)
                continue
            upper = token.upper()
            if upper in {"AND", "OR", "NOT"}:
                fuzzy_tokens.append(token)
                continue
            fuzzy_tokens.append(f"{token}~1")
        return " ".join(fuzzy_tokens)

    def _build_list_query(self, raw_query: str, fuzzy: bool = False) -> dict[str, Any]:
        return {
            "simple_query_string": {
                "query": self._build_list_query_text(raw_query, fuzzy=fuzzy),
                "fields": ["content"],
                "default_operator": "and",
            }
        }

    def _summarize_hit(self, hit: dict[str, Any]) -> dict[str, Any]:
        src = hit.get("_source", {})
        doc_id = self._sanitize_text(hit.get("_id", ""))
        name = self._sanitize_text(src.get("name", "unknown"))
        return {
            "doc_id": doc_id,
            "name": name,
            "pages": src.get("pages", "?"),
            "size": src.get("size", 0),
            "link": self.doc_link(doc_id) if doc_id else "",
        }

    def _format_search_results(self, hits: list[dict[str, Any]], total: dict[str, Any], limit: int) -> str:
        total_value = int(total.get("value", 0))
        relation = self._sanitize_text(total.get("relation", "eq"))
        prefix = ">" if relation == "gte" else ""

        if not hits:
            return "No results found."

        seen_hashes: dict[str, str] = {}
        dupes: set[str] = set()
        for hit in hits:
            src = hit.get("_source", {})
            content = str(src.get("content", ""))
            if not content:
                continue
            digest = self._content_hash(content)
            current_id = str(hit.get("_id", ""))
            if digest in seen_hashes:
                dupes.add(current_id)
                dupes.add(seen_hashes[digest])
            else:
                seen_hashes[digest] = current_id

        lines = [f"[{len(hits)} of {prefix}{total_value} results]"]
        if total_value > DEEP_SWEEP_RESULT_THRESHOLD and len(hits) < total_value and limit < DEEP_SWEEP_LIMIT_MIN:
            lines.append(
                "[PARTIAL VIEW: high-volume result set. Re-run es_search with limit=100 or limit=200, "
                "collect Bates IDs, then call es_read_batch.]"
            )
        lines.append("")
        for i, hit in enumerate(hits):
            src = hit.get("_source", {})
            doc_id = self._sanitize_text(hit.get("_id", ""))
            name = self._sanitize_text(src.get("name", "unknown"))
            pages = src.get("pages", "?")
            link = self.doc_link(doc_id) if doc_id else ""
            dupe_marker = " [NEAR-DUPLICATE]" if doc_id in dupes else ""
            lines.append(f"{name} ({pages} pages) {link}{dupe_marker}".rstrip())

            highlight = hit.get("highlight", {})
            content_fragments = highlight.get("content", []) if isinstance(highlight, dict) else []
            for fragment in content_fragments:
                clean = self._sanitize_text(str(fragment)).replace("<em>", "**").replace("</em>", "**")
                lines.append(f"  > {clean}")

            if i < len(hits) - 1:
                lines.append("")

        return "\n".join(lines)

    def search(
        self,
        *,
        terms: list[str],
        limit: int = DEFAULT_LIMIT,
        fuzzy: bool = False,
        cooccur: bool = False,
        exclude: list[str] | None = None,
        min_pages: int | None = None,
        max_pages: int | None = None,
        fragment_size: int = DEFAULT_HIGHLIGHT_FRAGMENT_SIZE,
        fragments: int = DEFAULT_HIGHLIGHT_FRAGMENTS,
    ) -> dict[str, Any]:
        if not terms:
            return {"result": "Tool Execution Error: terms cannot be empty.", "documents": []}

        limit = max(ES_SEARCH_LIMIT_MIN, min(int(limit), ES_SEARCH_LIMIT_MAX))
        fragment_size = max(ES_SEARCH_FRAGMENT_SIZE_MIN, min(int(fragment_size), ES_SEARCH_FRAGMENT_SIZE_MAX))
        fragments = max(ES_SEARCH_FRAGMENTS_MIN, min(int(fragments), ES_SEARCH_FRAGMENTS_MAX))

        must_clauses: list[dict[str, Any]] = []
        if cooccur:
            for term in terms:
                if fuzzy:
                    must_clauses.append({"match": {"content": {"query": term, "fuzziness": "AUTO"}}})
                else:
                    must_clauses.append({"match": {"content": term}})
        else:
            must_clauses.append(self._build_content_query(terms, fuzzy=fuzzy))

        exclude_terms = [self._sanitize_text(str(x)).strip() for x in (exclude or []) if str(x).strip()]
        must_not = self._build_exclude_filter(exclude_terms)

        filters: list[dict[str, Any]] = []
        if min_pages is not None:
            filters.append({"range": {"pages": {"gte": int(min_pages)}}})
        if max_pages is not None:
            filters.append({"range": {"pages": {"lte": int(max_pages)}}})

        body = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "must_not": must_not,
                    "filter": filters,
                }
            },
            "size": limit,
            "_source": ["name", "pages", "content", "size"],
            "highlight": {
                "fields": {
                    "content": {
                        "fragment_size": fragment_size,
                        "number_of_fragments": fragments,
                    }
                }
            },
        }
        result = self._search(body)
        hits = result.get("hits", {}).get("hits", [])
        total = result.get("hits", {}).get("total", {"value": len(hits), "relation": "eq"})
        return {
            "result": self._format_search_results(hits, total, limit),
            "documents": [self._summarize_hit(hit) for hit in hits],
            "total": total,
        }

    def count(self, *, terms: list[str], fuzzy: bool = False, cooccur: bool = False) -> dict[str, Any]:
        if not terms:
            return {"result": "Tool Execution Error: terms cannot be empty.", "count": 0, "documents": []}

        if len(terms) > 1 and cooccur:
            must_clauses: list[dict[str, Any]] = []
            for term in terms:
                if fuzzy:
                    must_clauses.append({"match": {"content": {"query": term, "fuzziness": "AUTO"}}})
                else:
                    must_clauses.append({"match": {"content": term}})
            query: dict[str, Any] = {"bool": {"must": must_clauses}}
        elif fuzzy:
            query = {"match": {"content": {"query": " ".join(terms), "fuzziness": "AUTO"}}}
        else:
            query = {"match": {"content": " ".join(terms)}}

        result = self._count({"query": query})
        count = int(result.get("count", 0))
        terms_display = " + ".join(terms) if cooccur else " ".join(terms)
        return {
            "result": f"{count} documents matching: {terms_display}",
            "count": count,
            "documents": [],
        }

    def read(self, *, bates: str, max_chars: int | None = None) -> dict[str, Any]:
        target = self.normalize_bates(bates)
        if not BATES_RE.fullmatch(target):
            return {
                "result": f"Tool Execution Error: invalid Bates number '{self._sanitize_text(bates)}'.",
                "documents": [],
            }

        body = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"name.keyword": target}},
                        {"match_phrase": {"name": target}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "size": 10,
            "_source": ["name", "pages", "content", "size"],
        }

        result = self._search(body)
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return {
                "result": f"No document found with Bates number: {target}",
                "documents": [],
            }

        exact_hits = [
            h for h in hits
            if self.normalize_bates(str(h.get("_source", {}).get("name", ""))) == target
        ]
        if not exact_hits:
            return {
                "result": f"No exact document found with Bates number: {target}",
                "documents": [],
            }

        hit = exact_hits[0]
        src = hit.get("_source", {})
        name = self._sanitize_text(src.get("name", "unknown"))
        pages = src.get("pages", "?")
        size = int(src.get("size", 0) or 0)
        doc_id = self._sanitize_text(hit.get("_id", ""))
        content = self._sanitize_text(src.get("content", ""))
        max_chars_int = int(max_chars) if max_chars is not None else None
        if max_chars_int is not None:
            max_chars_int = max(ES_READ_MAX_CHARS_MIN, min(max_chars_int, ES_READ_MAX_CHARS_MAX))

        lines = [f"{name} ({pages} pages, {size:,} bytes) {self.doc_link(doc_id)}", "=" * 80]
        if max_chars_int is not None and len(content) > max_chars_int:
            lines.append(content[:max_chars_int])
            lines.append(
                f"\n[... truncated at {max_chars_int} chars, full doc is {len(content)} chars ...]"
            )
        else:
            lines.append(content)

        return {
            "result": "\n".join(lines),
            "documents": [self._summarize_hit(hit)],
            "bates": target,
        }

    def read_batch(
        self,
        *,
        bates_list: list[str],
        max_chars_total: int = ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT,
    ) -> dict[str, Any]:
        raw_items = [
            str(raw).strip()
            for raw in bates_list
            if str(raw).strip()
        ]
        if not raw_items:
            return {"result": "No valid document identifiers provided.", "documents": []}

        request_specs: list[dict[str, str]] = []
        seen_specs: set[tuple[str, str]] = set()
        for raw in raw_items:
            base = os.path.basename(raw)
            lower_raw = raw.lower()
            lower_base = base.lower()

            if SOURCE_DOC_ID_RE.fullmatch(lower_raw):
                spec = ("doc_id", lower_raw)
                display = raw
            elif SOURCE_DOC_ID_RE.fullmatch(lower_base):
                spec = ("doc_id", lower_base)
                display = base
            else:
                norm = self.normalize_bates(base)
                key = norm if norm else base
                spec = ("name", key)
                display = key

            if spec in seen_specs:
                continue
            seen_specs.add(spec)
            request_specs.append(
                {
                    "kind": spec[0],
                    "key": spec[1],
                    "display": display,
                }
            )

        if not request_specs:
            return {"result": "No valid document identifiers provided.", "documents": []}

        try:
            max_chars_total_int = int(max_chars_total)
        except Exception:
            max_chars_total_int = ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT
        if max_chars_total_int <= 0:
            max_chars_total_int = ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT

        doc_id_targets = [
            spec["key"]
            for spec in request_specs
            if spec["kind"] == "doc_id"
        ]
        name_targets = [
            spec["key"]
            for spec in request_specs
            if spec["kind"] == "name"
        ]

        should_clauses: list[dict[str, Any]] = []
        if doc_id_targets:
            should_clauses.append({"ids": {"values": doc_id_targets}})

        name_terms: list[str] = []
        for target in name_targets:
            name_terms.extend(
                [
                    target,
                    f"{target}.pdf",
                    target.lower(),
                    f"{target.lower()}.pdf",
                ]
            )
        name_terms = _unique_preserve_order(name_terms)
        for term in name_terms:
            should_clauses.append({"term": {"name.keyword": term}})
            should_clauses.append({"match_phrase": {"name": term}})

        if not should_clauses:
            return {"result": "No valid document identifiers provided.", "documents": []}

        body = {
            "query": {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1,
                }
            },
            "size": min(max(len(request_specs) * 3, 100), 5000),
            "_source": ["name", "pages", "content", "size"],
        }
        result = self._search(body)
        hits = result.get("hits", {}).get("hits", [])

        id_hit_map: dict[str, dict[str, Any]] = {}
        for hit in hits:
            hit_id = self._sanitize_text(hit.get("_id", "")).lower()
            if hit_id and hit_id not in id_hit_map:
                id_hit_map[hit_id] = hit

        hit_map: dict[str, dict[str, Any]] = {}
        for hit in hits:
            src = hit.get("_source", {})
            name = self._sanitize_text(src.get("name", ""))
            base = os.path.basename(name)
            normalized = self.normalize_bates(base)
            keys = _unique_preserve_order(
                [
                    normalized,
                    base,
                    base.upper(),
                    base.lower(),
                    os.path.splitext(base)[0],
                    os.path.splitext(base)[0].upper(),
                    os.path.splitext(base)[0].lower(),
                ]
            )
            for key in keys:
                if key and key not in hit_map:
                    hit_map[key] = hit

        output_lines: list[str] = []
        documents_meta: list[dict[str, Any]] = []
        emitted_doc_ids: set[str] = set()
        total_chars = 0

        for spec in request_specs:
            kind = spec["kind"]
            key = spec["key"]
            display = spec["display"]

            hit: dict[str, Any] | None
            if kind == "doc_id":
                hit = id_hit_map.get(key.lower())
            else:
                name_lookup_keys = _unique_preserve_order(
                    [
                        key,
                        key.upper(),
                        key.lower(),
                        f"{key}.pdf",
                        f"{key.upper()}.pdf",
                        f"{key.lower()}.pdf",
                    ]
                )
                hit = None
                for lookup in name_lookup_keys:
                    if lookup in hit_map:
                        hit = hit_map[lookup]
                        break

            if hit is None:
                output_lines.append(f"--- DOCUMENT {display}: NOT FOUND ---")
                continue

            src = hit.get("_source", {})
            doc_id = self._sanitize_text(hit.get("_id", ""))
            if doc_id and doc_id in emitted_doc_ids:
                continue

            name = self._sanitize_text(src.get("name", "unknown"))
            display_name = self.normalize_bates(os.path.basename(name)) or display
            content = self._sanitize_text(src.get("content", ""))
            pages = src.get("pages", "?")

            remaining = max_chars_total_int - total_chars
            if remaining <= 0:
                output_lines.append(f"[STOP: Batch limit of {max_chars_total_int} chars reached.]")
                break

            content_chunk = content if len(content) <= remaining else content[:remaining]
            truncated = len(content_chunk) < len(content)

            output_lines.append(f"--- START DOCUMENT {display_name} ({pages} pages) ---")
            output_lines.append(content_chunk)
            output_lines.append(f"--- END DOCUMENT {display_name} ---\n")

            total_chars += len(content_chunk)
            documents_meta.append(self._summarize_hit(hit))
            if doc_id:
                emitted_doc_ids.add(doc_id)

            if truncated or total_chars >= max_chars_total_int:
                output_lines.append(f"[STOP: Batch limit of {max_chars_total_int} chars reached.]")
                break

        return {
            "result": "\n".join(output_lines),
            "documents": documents_meta,
            "count": len(documents_meta),
            "requested": len(request_specs),
        }

    def get_document_content(self, *, source_doc_id: str) -> str:
        source = self._sanitize_text(source_doc_id).strip()
        if not source:
            return ""

        source_upper = source.upper()
        if BATES_RE.fullmatch(source_upper):
            body = {
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"name.keyword": source_upper}},
                            {"match_phrase": {"name": source_upper}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "size": 10,
                "_source": ["name", "content"],
            }
            result = self._search(body)
            hits = result.get("hits", {}).get("hits", [])
            exact_hits = [
                h for h in hits
                if self.normalize_bates(str(h.get("_source", {}).get("name", ""))) == source_upper
            ]
            if not exact_hits:
                return ""
            src = exact_hits[0].get("_source", {})
            return self._sanitize_text(src.get("content", ""))

        source_lower = source.lower()
        if SOURCE_DOC_ID_RE.fullmatch(source_lower):
            body = {
                "query": {"ids": {"values": [source_lower]}},
                "size": 1,
                "_source": ["content"],
            }
            result = self._search(body)
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                return ""
            src = hits[0].get("_source", {})
            return self._sanitize_text(src.get("content", ""))

        return ""

    def list_documents(self, *, query: str, fuzzy: bool = False) -> dict[str, Any]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return {"result": "Tool Execution Error: query cannot be empty.", "documents": []}

        names: list[str] = []
        seen_names: set[str] = set()
        seen_hashes: set[str] = set()
        documents: list[dict[str, Any]] = []
        search_after: Any = None
        query_body = self._build_list_query(raw_query, fuzzy=fuzzy)

        while True:
            body: dict[str, Any] = {
                "query": query_body,
                "size": LIST_PAGE_SIZE,
                "_source": ["name", "content", "pages", "size"],
                "sort": [{"name": {"order": "asc", "missing": "_last"}}],
                "track_total_hits": True,
            }
            if search_after is not None:
                body["search_after"] = search_after

            result = self._search(body)
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                src = hit.get("_source", {})
                name = self._sanitize_text(os.path.basename(str(src.get("name", ""))))
                if not name:
                    continue
                content = str(src.get("content", ""))
                if content:
                    digest = self._content_hash(content)
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                if name in seen_names:
                    continue
                seen_names.add(name)
                names.append(name)
                documents.append(self._summarize_hit(hit))

            search_after = hits[-1].get("sort")
            if not search_after:
                break

        text = "\n".join(names) if names else "No results found."
        return {"result": text, "documents": documents}


_ES_CLIENT: ElasticsearchArchiveClient | None = None


def get_es_client() -> ElasticsearchArchiveClient:
    global _ES_CLIENT
    if _ES_CLIENT is None:
        _ES_CLIENT = ElasticsearchArchiveClient(
            es_url=ES_URL,
            es_index=ES_INDEX,
            sist2_url=SIST2_URL,
        )
    return _ES_CLIENT


def fetch_document_content_for_source(source_doc_id: str) -> str:
    return get_es_client().get_document_content(source_doc_id=source_doc_id)


def index_documents_from_tool_result(result: Any) -> None:
    if not isinstance(result, dict):
        return
    documents = result.get("documents")
    if not isinstance(documents, list):
        return
    mapping = st.session_state.doc_id_to_source_path
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("doc_id", "")).strip()
        name = os.path.basename(str(doc.get("name", "")).strip())
        if not doc_id or not name:
            continue
        source_path = os.path.join(DATA_DIR, name)
        if os.path.isfile(source_path):
            mapping[doc_id] = source_path

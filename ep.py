#!/usr/bin/env python3
"""
ep - Epstein Documents Search CLI

A command-line tool for searching the Epstein case files indexed in Elasticsearch via sist2.

Usage:
    ep search <terms>...          Search for terms in document content
    ep count <terms>...           Count matching documents
    ep read <bates_number>        Read full document by Bates number
    ep cooccur <term1> <term2>    Find documents where both terms appear
    ep notes                      View saved research notes
    ep save <note> <bates>        Save a research finding

See ep <command> --help for detailed options.

Security notes:
- This tool sends search queries and terms to Elasticsearch at EP_ES_URL.
- Document links are built from EP_SIST2_URL.
- Notes are stored in plaintext JSONL at EP_NOTES_FILE.
- Terminal output is sanitized to strip ANSI/control escape sequences.
"""

import argparse
import json
import os
import re
import sys
import hashlib
import urllib.request
import urllib.error
from datetime import datetime

ES_URL = os.environ.get("EP_ES_URL", "http://localhost:9200")
ES_INDEX = os.environ.get("EP_ES_INDEX", "sist2")
SIST2_URL = os.environ.get("EP_SIST2_URL", "http://localhost:1997")
NOTES_FILE = os.environ.get("EP_NOTES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.jsonl"))

DEFAULT_HIGHLIGHT_FRAGMENT_SIZE = 300
DEFAULT_HIGHLIGHT_FRAGMENTS = 3
DEFAULT_LIMIT = 10

ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1B[@-Z\\-_]|\x1B\[[0-?]*[ -/]*[@-~]|\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def es_query(body, params=""):
    """Execute an ES query and return parsed JSON."""
    url = f"{ES_URL}/{ES_INDEX}/_search{params}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error connecting to Elasticsearch at {ES_URL}: {e}", file=sys.stderr)
        sys.exit(1)


def es_count(body):
    """Execute an ES count query."""
    url = f"{ES_URL}/{ES_INDEX}/_count"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error connecting to Elasticsearch at {ES_URL}: {e}", file=sys.stderr)
        sys.exit(1)


def doc_link(es_id):
    """Construct a clickable sist2 document link."""
    return f"{SIST2_URL}/f/{es_id}"


def sanitize_terminal(text):
    """Strip ANSI/control escape sequences from terminal-bound text."""
    if text is None:
        return ""
    clean = ANSI_ESCAPE_RE.sub("", str(text))
    return CONTROL_CHARS_RE.sub("", clean)


def normalize_bates(value):
    """Normalize potential Bates values for exact matching."""
    base = os.path.basename(str(value).strip())
    stem, ext = os.path.splitext(base)
    if ext.lower() == ".pdf":
        base = stem
    return base.upper()


def content_hash(text):
    """Hash first 500 chars of content for near-duplicate detection."""
    normalized = "".join(text[:500].lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def format_results(hits, total, show_content=False, highlight=True):
    """Format ES search results for terminal output."""
    total_value = total["value"]
    relation = total["relation"]
    prefix = ">" if relation == "gte" else ""
    
    if not hits:
        print(f"No results found.")
        return

    # Detect near-duplicates
    seen_hashes = {}
    dupes = set()
    for hit in hits:
        src = hit.get("_source", {})
        c = src.get("content", "")
        if c:
            h = content_hash(c)
            if h in seen_hashes:
                dupes.add(hit["_id"])
                dupes.add(seen_hashes[h])
            else:
                seen_hashes[h] = hit["_id"]

    print(f"[{len(hits)} of {prefix}{total_value} results]\n")

    for i, hit in enumerate(hits):
        src = hit.get("_source", {})
        name = sanitize_terminal(src.get("name", "unknown"))
        pages = src.get("pages", "?")
        es_id = hit["_id"]
        link = sanitize_terminal(doc_link(es_id))
        dupe_marker = " [NEAR-DUPLICATE]" if es_id in dupes else ""

        print(f"{name} ({pages} pages) {link}{dupe_marker}")

        if highlight and "highlight" in hit:
            for fragment in hit["highlight"].get("content", []):
                # Clean up the fragment, preserve ES highlight tags as bold markers
                clean = fragment.replace("<em>", "\033[1m").replace("</em>", "\033[0m")
                print(f"  > {sanitize_terminal(clean)}")

        if show_content:
            content = src.get("content", "")
            if content:
                # Truncate very long content
                if len(content) > 5000:
                    print(f"\n{sanitize_terminal(content[:5000])}\n\n[... truncated at 5000 chars, full doc is {len(content)} chars ...]")
                else:
                    print(f"\n{sanitize_terminal(content)}")

        if i < len(hits) - 1:
            print()


def build_content_query(terms, fuzzy=False):
    """Build a match query for content field."""
    query_text = " ".join(terms)
    if fuzzy:
        return {"match": {"content": {"query": query_text, "fuzziness": "AUTO"}}}
    else:
        return {"match": {"content": query_text}}


def build_exclude_filter(exclude):
    """Build must_not clauses for excluding Bates numbers."""
    if not exclude:
        return []
    return [{"terms": {"name": exclude}}]


def cmd_search(args):
    """Search for terms in document content."""
    must_clauses = []

    if args.cooccur:
        # Each term gets its own match clause for co-occurrence
        for term in args.terms:
            if args.fuzzy:
                must_clauses.append({"match": {"content": {"query": term, "fuzziness": "AUTO"}}})
            else:
                must_clauses.append({"match": {"content": term}})
    else:
        must_clauses.append(build_content_query(args.terms, args.fuzzy))

    must_not = build_exclude_filter(args.exclude)

    filters = []
    if args.min_pages:
        filters.append({"range": {"pages": {"gte": args.min_pages}}})
    if args.max_pages:
        filters.append({"range": {"pages": {"lte": args.max_pages}}})

    query = {
        "bool": {
            "must": must_clauses,
            "must_not": must_not,
            "filter": filters,
        }
    }

    body = {
        "query": query,
        "size": args.limit,
        "_source": ["name", "pages", "content"],
        "highlight": {
            "fields": {
                "content": {
                    "fragment_size": args.fragment_size,
                    "number_of_fragments": args.fragments,
                }
            }
        },
    }

    result = es_query(body)
    format_results(
        result["hits"]["hits"],
        result["hits"]["total"],
        highlight=True,
    )


def cmd_count(args):
    """Count documents matching terms."""
    if len(args.terms) > 1 and args.cooccur:
        must_clauses = []
        for term in args.terms:
            if args.fuzzy:
                must_clauses.append({"match": {"content": {"query": term, "fuzziness": "AUTO"}}})
            else:
                must_clauses.append({"match": {"content": term}})
        query = {"bool": {"must": must_clauses}}
    elif args.fuzzy:
        query = {"match": {"content": {"query": " ".join(args.terms), "fuzziness": "AUTO"}}}
    else:
        query = {"match": {"content": " ".join(args.terms)}}

    result = es_count({"query": query})
    terms_display = " + ".join(args.terms) if args.cooccur else " ".join(args.terms)
    print(f"{result['count']} documents matching: {terms_display}")


def cmd_read(args):
    """Read full document by Bates number."""
    body = {
        "query": {
            "bool": {
                "should": [
                    {"term": {"name.keyword": args.bates}},
                    {"match_phrase": {"name": args.bates}},
                ],
                "minimum_should_match": 1,
            }
        },
        "size": 10,
        "_source": ["name", "pages", "content", "size"],
    }

    result = es_query(body)
    hits = result["hits"]["hits"]

    if not hits:
        print(f"No document found with Bates number: {args.bates}", file=sys.stderr)
        sys.exit(1)

    target = normalize_bates(args.bates)
    exact_hits = [h for h in hits if normalize_bates(h.get("_source", {}).get("name", "")) == target]

    if not exact_hits:
        print(f"No exact document found with Bates number: {args.bates}", file=sys.stderr)
        sys.exit(1)

    hit = exact_hits[0]
    src = hit["_source"]
    name = sanitize_terminal(src.get("name", "unknown"))
    pages = src.get("pages", "?")
    size = src.get("size", 0)
    es_id = hit["_id"]
    content = src.get("content", "")

    print(f"{name} ({pages} pages, {size:,} bytes) {sanitize_terminal(doc_link(es_id))}")
    print(f"{'=' * 80}")

    if args.max_chars and len(content) > args.max_chars:
        print(sanitize_terminal(content[:args.max_chars]))
        print(f"\n[... truncated at {args.max_chars} chars, full doc is {len(content)} chars ...]")
    else:
        print(sanitize_terminal(content))


def cmd_cooccur(args):
    """Find documents where multiple terms co-occur."""
    # Delegate to search with cooccur flag
    args.cooccur = True
    args.fuzzy = args.fuzzy if hasattr(args, "fuzzy") else False
    args.exclude = args.exclude if hasattr(args, "exclude") else None
    args.min_pages = args.min_pages if hasattr(args, "min_pages") else None
    args.max_pages = args.max_pages if hasattr(args, "max_pages") else None
    args.limit = args.limit if hasattr(args, "limit") else DEFAULT_LIMIT
    args.fragment_size = args.fragment_size if hasattr(args, "fragment_size") else DEFAULT_HIGHLIGHT_FRAGMENT_SIZE
    args.fragments = args.fragments if hasattr(args, "fragments") else DEFAULT_HIGHLIGHT_FRAGMENTS
    cmd_search(args)


def cmd_save(args):
    """Save a research finding."""
    note = {
        "timestamp": datetime.now().isoformat(),
        "text": args.note,
        "bates": args.bates,
        "tags": args.tag or [],
    }

    with open(NOTES_FILE, "a") as f:
        f.write(json.dumps(note) + "\n")

    tags_display = f" [{', '.join(sanitize_terminal(t) for t in args.tag)}]" if args.tag else ""
    print(f"Saved: {sanitize_terminal(args.note)} → {sanitize_terminal(args.bates)}{tags_display}")


def cmd_notes(args):
    """View saved research notes."""
    if not os.path.exists(NOTES_FILE):
        print("No notes saved yet.")
        return

    notes = []
    with open(NOTES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                notes.append(json.loads(line))

    if args.tag:
        notes = [n for n in notes if any(t in n.get("tags", []) for t in args.tag)]

    if args.search:
        search_lower = args.search.lower()
        notes = [n for n in notes if search_lower in n.get("text", "").lower() or search_lower in n.get("bates", "").lower()]

    if not notes:
        if args.tag:
            print(f"No notes found with tags: {', '.join(args.tag)}")
        elif args.search:
            print(f"No notes matching: {args.search}")
        else:
            print("No notes saved yet.")
        return

    # Show unique tags summary
    if not args.tag and not args.search:
        all_tags = set()
        for n in notes:
            all_tags.update(n.get("tags", []))
        if all_tags:
            print(f"Tags: {', '.join(sorted(all_tags))}")
            print()

    for n in notes:
        ts = n.get("timestamp", "")[:16]
        tags_display = f" [{', '.join(sanitize_terminal(t) for t in n.get('tags', []))}]" if n.get("tags") else ""
        bates = sanitize_terminal(n.get("bates", ""))
        bates_display = f" → {bates}" if bates else ""
        print(f"[{ts}]{tags_display}{bates_display}")
        print(f"  {sanitize_terminal(n.get('text', ''))}")
        print()


def cmd_tags(args):
    """List all tags used in notes."""
    if not os.path.exists(NOTES_FILE):
        print("No notes saved yet.")
        return

    tag_counts = {}
    with open(NOTES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                note = json.loads(line)
                for tag in note.get("tags", []):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if not tag_counts:
        print("No tags found.")
        return

    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {sanitize_terminal(tag)} ({count})")


def main():
    parser = argparse.ArgumentParser(
        prog="ep",
        description="Search the Epstein case files indexed in Elasticsearch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p_search = subparsers.add_parser("search", aliases=["s"], help="Search document content")
    p_search.add_argument("terms", nargs="+", help="Search terms")
    p_search.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT, help=f"Max results (default: {DEFAULT_LIMIT})")
    p_search.add_argument("-f", "--fuzzy", action="store_true", help="Enable fuzzy matching for OCR errors")
    p_search.add_argument("-c", "--cooccur", action="store_true", help="Require all terms to co-occur (AND instead of OR)")
    p_search.add_argument("-x", "--exclude", nargs="+", metavar="BATES", help="Exclude these Bates numbers")
    p_search.add_argument("--min-pages", type=int, help="Minimum page count")
    p_search.add_argument("--max-pages", type=int, help="Maximum page count")
    p_search.add_argument("--fragment-size", type=int, default=DEFAULT_HIGHLIGHT_FRAGMENT_SIZE, help="Highlight fragment size")
    p_search.add_argument("--fragments", type=int, default=DEFAULT_HIGHLIGHT_FRAGMENTS, help="Number of highlight fragments")
    p_search.set_defaults(func=cmd_search)

    # count
    p_count = subparsers.add_parser("count", aliases=["c"], help="Count matching documents")
    p_count.add_argument("terms", nargs="+", help="Search terms")
    p_count.add_argument("-f", "--fuzzy", action="store_true", help="Enable fuzzy matching")
    p_count.add_argument("-c", "--cooccur", action="store_true", help="Require all terms to co-occur")
    p_count.set_defaults(func=cmd_count)

    # read
    p_read = subparsers.add_parser("read", aliases=["r"], help="Read full document by Bates number")
    p_read.add_argument("bates", help="Bates number (e.g. EFTA02290848)")
    p_read.add_argument("--max-chars", type=int, help="Truncate content at this many characters")
    p_read.set_defaults(func=cmd_read)

    # cooccur (convenience alias)
    p_cooccur = subparsers.add_parser("cooccur", aliases=["co"], help="Find documents where all terms co-occur")
    p_cooccur.add_argument("terms", nargs="+", help="Terms that must all appear")
    p_cooccur.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT, help=f"Max results (default: {DEFAULT_LIMIT})")
    p_cooccur.add_argument("-f", "--fuzzy", action="store_true", help="Enable fuzzy matching")
    p_cooccur.add_argument("-x", "--exclude", nargs="+", metavar="BATES", help="Exclude these Bates numbers")
    p_cooccur.add_argument("--min-pages", type=int, help="Minimum page count")
    p_cooccur.add_argument("--max-pages", type=int, help="Maximum page count")
    p_cooccur.add_argument("--fragment-size", type=int, default=DEFAULT_HIGHLIGHT_FRAGMENT_SIZE)
    p_cooccur.add_argument("--fragments", type=int, default=DEFAULT_HIGHLIGHT_FRAGMENTS)
    p_cooccur.set_defaults(func=cmd_cooccur)

    # save
    p_save = subparsers.add_parser("save", help="Save a research finding")
    p_save.add_argument("note", help="Description of the finding")
    p_save.add_argument("bates", nargs="?", default="", help="Related Bates number")
    p_save.add_argument("-t", "--tag", action="append", help="Tag(s) for this note (repeatable)")
    p_save.set_defaults(func=cmd_save)

    # notes
    p_notes = subparsers.add_parser("notes", aliases=["n"], help="View saved research notes")
    p_notes.add_argument("-t", "--tag", action="append", help="Filter by tag(s)")
    p_notes.add_argument("-s", "--search", help="Search notes text")
    p_notes.set_defaults(func=cmd_notes)

    # tags
    p_tags = subparsers.add_parser("tags", help="List all tags and their counts")
    p_tags.set_defaults(func=cmd_tags)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()

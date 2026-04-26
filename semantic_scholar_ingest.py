#!/usr/bin/env python3
"""
Semantic Scholar API ingest for Node_Temp_Node corpus.

Reads S2_API_KEY from environment only — never from files or arguments.
Writes to training/corpus/semantic_scholar_feed.jsonl.
Logs to metadata/ingest_logs/semantic_scholar.log.
Adds candidates to runtime/validation_queue.jsonl.
Does NOT auto-promote. Records remain UNVERIFIED until truth gate promotes.

Usage:
  python3 semantic_scholar_ingest.py              # full pass over all topics
  python3 semantic_scholar_ingest.py --once       # one topic then exit
  python3 semantic_scholar_ingest.py --dry-run    # show what would be fetched
  python3 semantic_scholar_ingest.py --status     # show ingest state
  python3 semantic_scholar_ingest.py --query "quantum error correction"
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / "Node_Temp_Node"
CORPUS_FILE = BASE / "training/corpus/semantic_scholar_feed.jsonl"
LOG_FILE = BASE / "metadata/ingest_logs/semantic_scholar.log"
STATE_FILE = BASE / "metadata/ingest_logs/semantic_scholar_state.json"
VALIDATION_QUEUE = BASE / "runtime/validation_queue.jsonl"
CONFIG_PATH = BASE / "config/source_registry.json"

S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = "paperId,title,abstract,year,authors,citationCount,externalIds,url,openAccessPdf"

DEFAULT_TOPICS = [
    "quantum error correction",
    "variational quantum eigensolver",
    "quantum machine learning",
    "noisy intermediate-scale quantum",
    "quantum circuit optimization",
    "topological quantum computing",
    "quantum advantage",
    "quantum entanglement",
    "quantum cryptography",
    "quantum simulation",
]

DEFAULT_LIMIT_PER_QUERY = 10
DEFAULT_RPS = 0.5
BACKOFF_429_INITIAL = 30
BACKOFF_429_MAX = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_write(msg: str, level: str = "INFO") -> None:
    line = f"{utc_now()} [{level}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            ss = data.get("sources", {}).get("semantic_scholar", {})
            rate = ss.get("rate_limit", {})
            return {
                "rps": rate.get("requests_per_second", DEFAULT_RPS),
                "limit_per_query": rate.get("limit_per_query", DEFAULT_LIMIT_PER_QUERY),
                "backoff_initial": rate.get("backoff_429_initial", BACKOFF_429_INITIAL),
                "backoff_max": rate.get("backoff_429_max", BACKOFF_429_MAX),
            }
        except Exception:
            pass
    return {
        "rps": DEFAULT_RPS,
        "limit_per_query": DEFAULT_LIMIT_PER_QUERY,
        "backoff_initial": BACKOFF_429_INITIAL,
        "backoff_max": BACKOFF_429_MAX,
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"topic_offsets": {}, "total_ingested": 0, "total_429s": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_seen_ids() -> set:
    seen: set = set()
    for path in (CORPUS_FILE, VALIDATION_QUEUE):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    pid = r.get("paper_id") or r.get("id")
                    if pid:
                        seen.add(pid)
                except Exception:
                    pass
    return seen


def append_corpus(record: dict) -> None:
    CORPUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def append_queue(entry: dict) -> None:
    VALIDATION_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with VALIDATION_QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def api_get(
    url: str, params: dict, api_key: str, timeout: int = 30
) -> tuple[dict | None, int, str | None]:
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url)
    req.add_header("x-api-key", api_key)
    req.add_header("User-Agent", "Node_Temp_Node/1.0 (kestrel-node research)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), 200, None
    except urllib.error.HTTPError as exc:
        return None, exc.code, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, 0, f"URL error: {exc.reason}"
    except Exception as exc:
        return None, 0, str(exc)[:200]


def build_record(paper: dict, topic: str) -> dict:
    pid = paper.get("paperId", "")
    title = (paper.get("title") or "").strip()
    abstract = (paper.get("abstract") or "").strip()
    year = paper.get("year")
    authors = [a.get("name", "") for a in (paper.get("authors") or [])]
    citation_count = paper.get("citationCount") or 0
    ext_ids = paper.get("externalIds") or {}
    arxiv_id = ext_ids.get("ArXiv", "")
    doi = ext_ids.get("DOI", "")
    s2_url = paper.get("url") or f"https://www.semanticscholar.org/paper/{pid}"
    oap = paper.get("openAccessPdf") or {}
    pdf_url = oap.get("url", "") if isinstance(oap, dict) else ""

    content = f"{title}: {abstract[:600]}" if abstract else title
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    return {
        "topic": topic,
        "type": "paper",
        "content": content,
        "state": "UNVERIFIED",
        "source": "semantic_scholar",
        "source_url": s2_url,
        "paper_id": pid,
        "title": title,
        "abstract": abstract[:1000],
        "year": year,
        "authors": authors,
        "citation_count": citation_count,
        "arxiv_id": arxiv_id,
        "doi": doi,
        "pdf_url": pdf_url,
        "ingested_at": utc_now(),
        "content_hash": content_hash,
    }


def build_queue_entry(record: dict) -> dict:
    return {
        "id": record["paper_id"],
        "source": "semantic_scholar",
        "source_url": record["source_url"],
        "content": record["content"],
        "topic": record["topic"],
        "arxiv_id": record.get("arxiv_id", ""),
        "validation_status": "pending",
        "added_at": utc_now(),
    }


def ingest_topic(
    topic: str,
    api_key: str,
    offset: int,
    limit: int,
    seen_ids: set,
    dry_run: bool,
) -> tuple[int, int, int | str | None]:
    """Fetch one page for a topic. Returns (new_count, next_offset, error).
    error is 429 (int) on rate-limit, a string on other failures, or None on success."""
    params = {"query": topic, "offset": offset, "limit": limit, "fields": S2_FIELDS}
    log_write(f'Fetching query="{topic}" offset={offset} limit={limit}')

    data, status_code, err_msg = api_get(S2_SEARCH_URL, params, api_key)

    if status_code == 429:
        return 0, offset, 429

    if err_msg is not None:
        log_write(f"Fetch error: {err_msg}", "ERROR")
        return 0, offset, err_msg

    papers = data.get("data") or []
    total_available = data.get("total", 0)
    next_offset = data.get("next", offset + len(papers))

    new_count = 0
    for paper in papers:
        pid = paper.get("paperId", "")
        if not pid or pid in seen_ids:
            continue
        record = build_record(paper, topic)
        if dry_run:
            log_write(f'[dry-run] would ingest paper_id={pid} title="{paper.get("title", "")[:60]}"')
        else:
            append_corpus(record)
            append_queue(build_queue_entry(record))
            seen_ids.add(pid)
            new_count += 1
            log_write(f'New: paper_id={pid} title="{paper.get("title", "")[:60]}"')

    log_write(
        f'Done: query="{topic}" fetched={len(papers)} new={new_count} '
        f'total_available={total_available}'
    )
    return new_count, next_offset, None


def run(
    once: bool,
    dry_run: bool,
    status_only: bool,
    query: str | None,
    limit: int | None,
) -> int:
    api_key = os.environ.get("S2_API_KEY", "").strip()
    if not api_key:
        print("S2_API_KEY not set", file=sys.stderr)
        return 1

    cfg = load_config()
    state = load_state()

    if status_only:
        seen_ids = load_seen_ids()
        print(json.dumps({
            "total_ingested": state.get("total_ingested", 0),
            "total_429s": state.get("total_429s", 0),
            "corpus_records": len(seen_ids),
            "corpus_file": str(CORPUS_FILE),
            "corpus_exists": CORPUS_FILE.exists(),
            "api_key_configured": True,
        }, indent=2))
        return 0

    seen_ids = load_seen_ids()
    topics = [query] if query else DEFAULT_TOPICS
    effective_limit = limit or cfg["limit_per_query"]
    min_interval = 1.0 / cfg["rps"]
    backoff = cfg["backoff_initial"]
    total_new = 0

    log_write(
        f"Starting Semantic Scholar ingest | topics={len(topics)} "
        f"limit_per_query={effective_limit} rps={cfg['rps']}"
    )

    for topic in topics:
        offset = state["topic_offsets"].get(topic, 0)
        new_count, next_offset, err = ingest_topic(
            topic, api_key, offset, effective_limit, seen_ids, dry_run
        )

        if err == 429:
            state["total_429s"] = state.get("total_429s", 0) + 1
            log_write(f'429 rate-limited on query="{topic}", backing off {backoff}s', "WARN")
            if not dry_run:
                save_state(state)
            if once:
                return 0
            time.sleep(backoff)
            backoff = min(backoff * 2, cfg["backoff_max"])
            continue

        if err is not None:
            if once:
                return 1
            time.sleep(min_interval)
            continue

        backoff = cfg["backoff_initial"]

        if not dry_run:
            state["topic_offsets"][topic] = next_offset
            state["total_ingested"] = state.get("total_ingested", 0) + new_count
            save_state(state)

        total_new += new_count

        if once:
            break

        time.sleep(min_interval)

    log_write(f"Ingest pass complete | total_new={total_new}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Semantic Scholar API ingest")
    parser.add_argument("--once", action="store_true", help="Ingest one topic then exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without writing")
    parser.add_argument("--status", action="store_true", help="Show current ingest state and exit")
    parser.add_argument("--query", help="Override with single topic query")
    parser.add_argument("--limit", type=int, help="Papers per query (default: from config)")
    args = parser.parse_args()
    return run(
        once=args.once,
        dry_run=args.dry_run,
        status_only=args.status,
        query=args.query,
        limit=args.limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())

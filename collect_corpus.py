#!/usr/bin/env python3
"""Build a strict date-bounded corpus of u/danl999 in r/castaneda."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "https://arctic-shift.photon-reddit.com"
AUTHOR = "danl999"
SUBREDDIT = "castaneda"
START_ISO = "2025-07-13T00:00:00Z"
END_ISO = "2026-07-13T00:00:00Z"  # exclusive
OUTPUT = Path("danl999_r_castaneda_corpus_2025-07-13_2026-07-12.txt")
STATS = Path("corpus_stats.json")
USER_AGENT = "danl999-corpus-research/1.0 (noncommercial frequency-analysis corpus)"

START_TS = int(datetime.fromisoformat(START_ISO.replace("Z", "+00:00")).timestamp())
END_TS = int(datetime.fromisoformat(END_ISO.replace("Z", "+00:00")).timestamp())
KNOWN_GAPS: list[str] = []
REQUEST_COUNT = 0
REMOVED_LINK_DUMP_LINES = 0
MARKED_QUOTE_LINES = 0


def request_json(path: str, params: dict[str, Any], retries: int = 6) -> dict[str, Any]:
    global REQUEST_COUNT
    url = BASE + path + "?" + urllib.parse.urlencode(params, doseq=True)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as response:
                payload = response.read()
            REQUEST_COUNT += 1
            obj = json.loads(payload.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError(f"Unexpected JSON root: {type(obj).__name__}")
            if obj.get("error") or obj.get("detail") == "Query timed out":
                raise RuntimeError(str(obj.get("error") or obj.get("detail")))
            time.sleep(0.12)
            return obj
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            last_error = exc
            wait = min(30.0, 1.5 ** attempt)
            print(f"WARN request failed (attempt {attempt + 1}/{retries}): {url} :: {exc}; sleep {wait:.1f}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Request failed after {retries} attempts: {url}: {last_error}")


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_interval(kind: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    assert kind in {"posts", "comments"}
    params = {
        "author": AUTHOR,
        "subreddit": SUBREDDIT,
        "after": start_ts - 1,
        "before": end_ts + 1,
        "sort": "asc",
        "limit": "auto",
    }
    try:
        obj = request_json(f"/api/{kind}/search", params)
        data = obj.get("data", [])
        if not isinstance(data, list):
            raise ValueError("Missing list in data")
    except Exception as exc:
        span = end_ts - start_ts
        if span > 60:
            mid = start_ts + span // 2
            print(f"SPLIT {kind} after error: {iso(start_ts)} .. {iso(end_ts)}", flush=True)
            return fetch_interval(kind, start_ts, mid) + fetch_interval(kind, mid, end_ts)
        msg = f"{kind}: failed interval {iso(start_ts)} .. {iso(end_ts)}: {exc}"
        KNOWN_GAPS.append(msg)
        print("GAP " + msg, flush=True)
        return []

    in_range = [x for x in data if start_ts <= int(x.get("created_utc", -1)) < end_ts]
    span = end_ts - start_ts
    # limit=auto can return as few as 100 rows. Any response of 100+ is split
    # until every accepted slice has fewer than 100 rows.
    if len(data) >= 100 and span > 60:
        mid = start_ts + span // 2
        print(f"SPLIT {kind}: {iso(start_ts)} .. {iso(end_ts)} returned {len(data)}", flush=True)
        return fetch_interval(kind, start_ts, mid) + fetch_interval(kind, mid, end_ts)
    if len(data) >= 100 and span <= 60:
        KNOWN_GAPS.append(
            f"{kind}: possible truncation in <=60-second interval {iso(start_ts)} .. {iso(end_ts)} ({len(data)} rows)"
        )
    return in_range


def month_bounds(start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    start_dt = datetime.fromtimestamp(start_ts, timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, timezone.utc)
    bounds = [start_dt]
    y, m = start_dt.year, start_dt.month
    if start_dt.day != 1 or start_dt.hour or start_dt.minute or start_dt.second:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        cur = datetime(y, m, 1, tzinfo=timezone.utc)
    else:
        cur = start_dt
    while cur < end_dt:
        if cur > bounds[-1]:
            bounds.append(cur)
        cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc) if cur.month == 12 else datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
    if bounds[-1] != end_dt:
        bounds.append(end_dt)
    return [(int(a.timestamp()), int(b.timestamp())) for a, b in zip(bounds, bounds[1:])]


def collect(kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a, b in month_bounds(START_TS, END_TS):
        print(f"FETCH {kind}: {iso(a)} .. {iso(b)}", flush=True)
        rows.extend(fetch_interval(kind, a, b))
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = str(row.get("id", "")).removeprefix("t1_").removeprefix("t3_")
        if not rid:
            continue
        if str(row.get("author", "")).casefold() != AUTHOR.casefold():
            continue
        if str(row.get("subreddit", "")).casefold() != SUBREDDIT.casefold():
            continue
        ts = int(row.get("created_utc", -1))
        if START_TS <= ts < END_TS:
            dedup[rid] = row
    result = sorted(dedup.values(), key=lambda x: (int(x.get("created_utc", 0)), str(x.get("id", ""))))
    print(f"COLLECTED {kind}: {len(result)} unique rows", flush=True)
    return result


def normalize_id(value: Any) -> str:
    return str(value or "").removeprefix("t1_").removeprefix("t3_")


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title.replace("|", "¦").replace("===", "—")


def resolve_thread_titles(comments: list[dict[str, Any]], posts: list[dict[str, Any]]) -> dict[str, str]:
    title_map: dict[str, str] = {}
    for p in posts:
        pid = normalize_id(p.get("id"))
        title = clean_title(str(p.get("title") or ""))
        if pid and title:
            title_map[pid] = title
    missing = sorted({normalize_id(c.get("link_id")) for c in comments if normalize_id(c.get("link_id")) not in title_map})
    missing = [x for x in missing if x]
    for i in range(0, len(missing), 400):
        batch = missing[i:i + 400]
        try:
            obj = request_json("/api/posts/ids", {"ids": ",".join(batch), "fields": "id,title"})
            data = obj.get("data", [])
            if not isinstance(data, list):
                raise ValueError("Missing list in data")
            for p in data:
                pid = normalize_id(p.get("id"))
                title = clean_title(str(p.get("title") or ""))
                if pid and title:
                    title_map[pid] = title
        except Exception as exc:
            KNOWN_GAPS.append(f"thread-title lookup failed for batch {i // 400 + 1}: {exc}")
    return title_map


_URL_ONLY_RE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:https?://\S+|\[[^\]]*\]\(https?://[^)]+\))[\s.,;:!?-]*$",
    re.IGNORECASE,
)


def clean_author_text(text: str) -> str:
    global REMOVED_LINK_DUMP_LINES, MARKED_QUOTE_LINES
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u200b", "")
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _URL_ONLY_RE.match(lines[i] or ""):
            j = i
            block: list[str] = []
            while j < len(lines) and (_URL_ONLY_RE.match(lines[j] or "") or not lines[j].strip()):
                if _URL_ONLY_RE.match(lines[j] or ""):
                    block.append(lines[j])
                j += 1
            if len(block) >= 3:
                REMOVED_LINK_DUMP_LINES += len(block)
                if out and out[-1].strip():
                    out.append("")
                i = j
                continue
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith(">"):
            prefix_len = len(line) - len(stripped)
            line = line[:prefix_len] + "QUOTE: " + stripped[1:].lstrip()
            MARKED_QUOTE_LINES += 1
        out.append(line.rstrip())
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"(?u)\b[\w’'-]+\b", text))


def date_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    comments = collect("comments")
    posts = collect("posts")
    titles = resolve_thread_titles(comments, posts)
    units: list[dict[str, Any]] = []
    skipped = Counter()

    for p in posts:
        raw = p.get("selftext")
        if raw is None:
            skipped["post_missing_selftext_field"] += 1
            continue
        raw = str(raw)
        if raw in {"[deleted]", "[removed]"}:
            skipped["post_removed_or_deleted"] += 1
            continue
        body = clean_author_text(raw)
        if not body:
            skipped["post_empty_selftext"] += 1
            continue
        units.append({"created_utc": int(p["created_utc"]), "type": "post", "title": clean_title(str(p.get("title") or "[untitled]")), "body": body, "id": normalize_id(p.get("id"))})

    for c in comments:
        raw = c.get("body")
        if raw is None:
            skipped["comment_missing_body_field"] += 1
            continue
        raw = str(raw)
        if raw in {"[deleted]", "[removed]"}:
            skipped["comment_removed_or_deleted"] += 1
            continue
        body = clean_author_text(raw)
        if not body:
            skipped["comment_empty_after_cleaning"] += 1
            continue
        link_id = normalize_id(c.get("link_id"))
        thread_title = titles.get(link_id) or f"[thread title unavailable: {link_id or 'unknown'}]"
        if link_id not in titles:
            skipped["comment_missing_thread_title"] += 1
        units.append({"created_utc": int(c["created_utc"]), "type": "comment", "title": clean_title(thread_title), "body": body, "id": normalize_id(c.get("id"))})

    units.sort(key=lambda x: (x["created_utc"], 0 if x["type"] == "post" else 1, x["id"]))
    post_units = sum(u["type"] == "post" for u in units)
    comment_units = sum(u["type"] == "comment" for u in units)
    total_words = sum(word_count(u["body"]) for u in units)
    gaps = KNOWN_GAPS or ["No API intervals failed or were flagged as possibly truncated."]

    header = [
        "# CORPUS METADATA — NOT PART OF AUTHOR TEXT",
        "# Автор: u/danl999",
        "# Сабреддит: r/castaneda",
        "# Метод сбора: Arctic Shift API; author=danl999 + subreddit=castaneda; рекурсивное дробление интервалов при потенциальном достижении лимита; дедупликация по Reddit ID.",
        "# Критерий отбора: вариант A — всё подряд за непрерывный 12-месячный период, без смыслового фильтра.",
        f"# Покрытый период (UTC): {START_ISO} inclusive — {END_ISO} exclusive (то есть 2025-07-13 — 2026-07-12 целиком).",
        f"# Получено API: posts={len(posts)}, comments={len(comments)}.",
        f"# Включено текстовых единиц: posts={post_units}, comments={comment_units}, total={len(units)}.",
        f"# Слов авторского текста после формальной очистки: {total_words}.",
        f"# Явных Markdown-строк цитирования помечено префиксом QUOTE:: {MARKED_QUOTE_LINES}.",
        f"# Удалено строк из блоков-ссылок (3+ самостоятельных URL/Markdown-ссылки подряд): {REMOVED_LINK_DUMP_LINES}.",
        "# Цитаты: помечены только явные Markdown blockquote-строки, начинавшиеся с >. Встроенные цитаты без разметки автоматически не атрибутировались, чтобы не вносить смысловой фильтр.",
        "# Заголовки комментариев: восстановлены по link_id через Arctic Shift /api/posts/ids; при невозможности стоит [thread title unavailable: ID].",
        "# Пустые selftext у ссылочных/графических постов не включены: в них нет авторского текста для частотного анализа.",
        "# Известные пропуски/оговорки:",
    ]
    header.extend(f"# - {g}" for g in gaps)
    if skipped:
        header.append("# Формально исключённые/неполные единицы:")
        header.extend(f"# - {k}: {v}" for k, v in sorted(skipped.items()))
    header.extend([f"# API-запросов выполнено: {REQUEST_COUNT}.", f"# Сформировано: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}.", "# END CORPUS METADATA", ""])

    chunks = ["\n".join(header)]
    for u in units:
        prefix = "" if u["type"] == "post" else "re: "
        chunks.append(f"=== {date_utc(u['created_utc'])} | {u['type']} | {prefix}{u['title']} ===\n{u['body']}\n")
    OUTPUT.write_text("\n".join(chunks), encoding="utf-8", newline="\n")

    stats = {
        "author": AUTHOR, "subreddit": SUBREDDIT, "criterion": "A",
        "period_start_utc_inclusive": START_ISO, "period_end_utc_exclusive": END_ISO,
        "api_posts": len(posts), "api_comments": len(comments),
        "included_posts": post_units, "included_comments": comment_units, "included_units": len(units),
        "author_text_words": total_words, "quote_lines_marked": MARKED_QUOTE_LINES,
        "link_dump_lines_removed": REMOVED_LINK_DUMP_LINES, "request_count": REQUEST_COUNT,
        "known_gaps": KNOWN_GAPS, "skipped": dict(skipped), "output": str(OUTPUT),
    }
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    if total_words < 60000:
        print("WARN corpus is below requested 60,000-word minimum", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

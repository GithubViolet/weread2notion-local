"""
Personal Library Dashboard for WeRead2Notion

Fully automated management of a "个人图书馆" (Personal Library) in Notion:
- Books database (书籍库) with reading status, progress, ratings
- Notes database (读书笔记库) collecting all highlights, notes, and reviews
- State persistence via sync_state.json so databases are reused across runs

All databases are created via the Notion API -- no manual Notion setup needed.

Usage:
    Called automatically by cli.sync() after book sync completes.
    Can also be used directly via ensure_library(parent_page_id).
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

NOTION_API = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"

LIBRARY_TITLE = "个人图书馆"
BOOKS_DB_TITLE = "书籍库"
NOTES_DB_TITLE = "读书笔记库"
BOOKMARK_TYPE = "划线"
NOTE_TYPE = "笔记"
REVIEW_TYPE = "点评"

# Notion allows 3 req/s; 0.4s gives a safe margin
RATE_LIMIT_DELAY = 0.4


# ── Internal Helpers ───────────────────────────────────────────────────────


def _get_token():
    """Get Notion token from environment."""
    return os.environ.get("NOTION_TOKEN", "")


def _get_proxies():
    """Return proxy config for requests library.

    * When NO_PROXY=* is set the caller wants to bypass all proxies,
      so we return explicit None mappings.
    * When HTTP_PROXY / HTTPS_PROXY are set, return them explicitly so
      ``requests`` doesn't have issues with TLS proxy connections.
    * Otherwise return None so ``requests`` uses its default behaviour.
    """
    if os.environ.get("NO_PROXY") == "*" or os.environ.get("no_proxy") == "*":
        return {"http": None, "https": None}
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if http_proxy or https_proxy:
        return {
            "http": http_proxy or None,
            "https": https_proxy or None,
        }
    return None


def _headers():
    """Build standard Notion API request headers."""
    return {
        "Authorization": "Bearer " + _get_token(),
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def _api(method, path, body=None, params=None, _retries=0):
    """Make a Notion API call with retry logic.

    * Connection errors / timeouts -- up to 3 retries with increasing delay.
    * HTTP 429 (rate limited)      -- honour Retry-After header, then retry.
    * All other HTTP errors         -- raised immediately via raise_for_status.
    """
    url = NOTION_API + path
    proxies = _get_proxies()
    hdrs = _headers()

    try:
        if method == "GET":
            r = requests.get(
                url, headers=hdrs, params=params, proxies=proxies, timeout=30
            )
        elif method == "POST":
            r = requests.post(
                url, headers=hdrs, json=body or {}, proxies=proxies, timeout=30
            )
        elif method == "PATCH":
            r = requests.patch(
                url, headers=hdrs, json=body or {}, proxies=proxies, timeout=30
            )
        elif method == "DELETE":
            r = requests.delete(
                url, headers=hdrs, proxies=proxies, timeout=30
            )
        else:
            raise ValueError("Unsupported HTTP method: " + method)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        if _retries < 3:
            wait = 3 * (_retries + 1)
            print("  Connection error, retrying in {}s...".format(wait))
            time.sleep(wait)
            return _api(method, path, body, params, _retries + 1)
        raise

    if r.status_code == 429:
        retry_after = float(r.headers.get("Retry-After", 1))
        print("  Rate limited, waiting {}s...".format(retry_after))
        time.sleep(retry_after)
        return _api(method, path, body, params, _retries)

    r.raise_for_status()
    return r.json()


# ── State Management ──────────────────────────────────────────────────────


def _get_state_path():
    """Walk up from CWD to find the .env file and return the project root's
    ``sync_state.json`` path.

    Falls back to CWD if no .env is found.
    """
    cwd = os.getcwd()
    current = cwd
    while True:
        if os.path.isfile(os.path.join(current, ".env")):
            return os.path.join(current, "sync_state.json")
        parent = os.path.dirname(current)
        if parent == current:
            # Reached filesystem root -- fall back to CWD
            return os.path.join(cwd, "sync_state.json")
        current = parent


def load_state():
    """Load persistent state from ``sync_state.json``.  Returns a dict."""
    path = _get_state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Persist *state* dict to ``sync_state.json``."""
    path = _get_state_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# ── Notion Page ID Extraction ─────────────────────────────────────────────

_NOTION_ID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"|[a-f0-9]{32}"
)


def _extract_parent_page_id():
    """Extract a Notion page / database ID from environment variables.

    Reads ``NOTION_PAGE`` (preferred), ``NOTION_DATA_SOURCE_ID``, or
    ``NOTION_DATABASE_ID``.  The value may be a bare 32-char hex ID, a UUID
    with dashes, or a full Notion URL containing such an ID.
    """
    raw = (
        os.environ.get("NOTION_PAGE")
        or os.environ.get("NOTION_DATA_SOURCE_ID")
        or os.environ.get("NOTION_DATABASE_ID")
        or ""
    )
    raw = raw.strip()
    if not raw:
        return None
    match = _NOTION_ID_RE.search(raw)
    return match.group(0) if match else None


# ── Books Database (书籍库) ───────────────────────────────────────────────


def _books_db_schema():
    """Return the Notion property schema dict for the books database."""
    return {
        "书名": {"title": {}},
        "作者": {"rich_text": {}},
        "BookId": {"rich_text": {}},
        "ISBN": {"rich_text": {}},
        "链接": {"url": {}},
        "Sort": {"number": {}},
        "评分": {"number": {}},
        "分类": {"multi_select": {"options": []}},
        "状态": {
            "status": {
                "options": [
                    {"name": "未读", "color": "default"},
                    {"name": "在读", "color": "blue"},
                    {"name": "读完", "color": "green"},
                ]
            }
        },
        "阅读进度": {"number": {"format": "percent"}},
        "阅读时长": {"number": {}},
        "日期": {"date": {}},
        "封面": {"files": {}},
    }


def create_books_database(parent_page_id):
    """Create the 书籍库 database under *parent_page_id*.  Returns the new
    database ID.
    """
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": BOOKS_DB_TITLE}}],
        "properties": _books_db_schema(),
    }
    result = _api("POST", "/databases", body)
    db_id = result["id"]
    print("[BooksDB] Created: " + db_id)
    return db_id


def get_property_schema():
    """Return a ``{name: notion_type}`` mapping for the books database.

    Used by cli.py (or other callers) to discover property types when
    building Notion API property dicts.
    """
    schema = _books_db_schema()
    return {
        name: next(iter(config.keys()))
        for name, config in schema.items()
    }


# ── Notes Database (读书笔记库) ───────────────────────────────────────────


def _notes_db_schema():
    """Return the Notion property schema dict for the notes database."""
    return {
        "笔记": {"title": {}},
        "来源书籍": {"rich_text": {}},
        "作者": {"rich_text": {}},
        "来源章节": {"rich_text": {}},
        "笔记类型": {
            "select": {
                "options": [
                    {"name": BOOKMARK_TYPE, "color": "blue"},
                    {"name": NOTE_TYPE, "color": "green"},
                    {"name": REVIEW_TYPE, "color": "orange"},
                ]
            }
        },
        "分类": {"multi_select": {"options": []}},
        "笔记内容": {"rich_text": {}},
        "原文摘要": {"rich_text": {}},
        "阅读进度": {"number": {"format": "percent"}},
        "微信链接": {"url": {}},
        "来源BookId": {"rich_text": {}},
    }


def create_notes_database(parent_page_id):
    """Create the 读书笔记库 database under *parent_page_id*.  Returns the
    new database ID.
    """
    body = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": NOTES_DB_TITLE}}],
        "properties": _notes_db_schema(),
    }
    result = _api("POST", "/databases", body)
    db_id = result["id"]
    print("[NotesDB] Created: " + db_id)
    return db_id


# ── Library Setup ─────────────────────────────────────────────────────────


def ensure_library(parent_page_id):
    """Main entry point: ensure the books database exists.

    1. Loads persisted state from ``sync_state.json``.
    2. Creates the books database if no ID is stored.
    3. Saves state after creation so the ID is reused on subsequent runs.

    Returns ``books_db_id``.
    """
    state = load_state()

    books_db_id = state.get("books_db_id")
    if not books_db_id:
        books_db_id = create_books_database(parent_page_id)
        state["books_db_id"] = books_db_id
        save_state(state)

    print("[Library] Books DB: " + books_db_id)
    return books_db_id


# ── Page Renaming ────────────────────────────────────────────────────────


def rename_page(page_id, new_title):
    """Rename a Notion page to *new_title* via the API."""
    body = {
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": new_title}}]
            }
        }
    }
    try:
        _api("PATCH", "/pages/" + page_id, body)
        print("[Page] Renamed to: " + new_title)
    except Exception as exc:
        print("[Page] Rename failed: " + str(exc))


# ── Property Helpers ──────────────────────────────────────────────────────


def build_book_properties(raw_props):
    """Build a Notion API property dict from a raw key/value mapping.

    Uses the books database schema to decide how each value is wrapped.
    Unknown property names are silently skipped.
    """
    schema = get_property_schema()
    properties = {}

    for name, prop_type in schema.items():
        value = raw_props.get(name)
        if value is None:
            continue

        if prop_type == "title":
            properties[name] = {
                "title": [{"type": "text", "text": {"content": str(value)}}]
            }
        elif prop_type == "rich_text":
            properties[name] = {
                "rich_text": [{"type": "text", "text": {"content": str(value)}}]
            }
        elif prop_type == "number":
            properties[name] = {"number": value}
        elif prop_type == "url":
            properties[name] = {"url": str(value)}
        elif prop_type == "multi_select":
            if isinstance(value, (list, tuple)):
                properties[name] = {
                    "multi_select": [{"name": v} for v in value if v]
                }
            else:
                properties[name] = {
                    "multi_select": [{"name": str(value)}]
                }
        elif prop_type == "status":
            properties[name] = {"status": {"name": str(value)}}
        elif prop_type == "date":
            if isinstance(value, str):
                properties[name] = {"date": {"start": value}}
            elif isinstance(value, dict):
                properties[name] = {"date": value}
        elif prop_type == "files":
            if isinstance(value, str) and value.startswith("http"):
                properties[name] = {
                    "files": [{"type": "external", "name": "Cover", "external": {"url": value}}]
                }

    return properties


# ── Book Operations ───────────────────────────────────────────────────────


def delete_old_book_pages(books_db_id, book_id):
    """Delete all pages in the books database matching *book_id*.

    Returns the number of pages deleted.
    """
    body = {
        "filter": {
            "property": "BookId",
            "rich_text": {"equals": str(book_id)},
        },
        "page_size": 100,
    }
    try:
        result = _api("POST", "/databases/" + books_db_id + "/query", body)
    except Exception as exc:
        print("[BooksDB] Query failed: " + str(exc))
        return 0

    count = 0
    for page in result.get("results", []):
        try:
            _api("DELETE", "/blocks/" + page["id"])
            count += 1
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as exc:
            logger.warning("Failed to delete book page %s: %s", page["id"], exc)
    return count


def query_books_db(books_db_id, filter_body=None, sort_body=None, page_size=100):
    """Query the books database with optional filter and sort.

    Follows pagination cursors and returns a dict with 'results' key
    containing the combined list of result pages.
    """
    body = {}
    if filter_body:
        body["filter"] = filter_body
    if sort_body:
        body["sorts"] = sort_body
    body["page_size"] = page_size

    all_results = []
    while True:
        data = _api("POST", "/databases/" + books_db_id + "/query", body)
        all_results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]

    return {"results": all_results}


def set_page_open_full_page(books_db_id):
    """Attempt to set the database view's page_open to 'full_page'.

    The Notion public API does not officially support view updates,
    but we try the internal-style endpoint as a best-effort.  If it
    fails we silently skip — the user can change it manually.
    """
    try:
        db_info = _api("GET", "/databases/" + books_db_id)
    except Exception:
        return

    views = db_info.get("views", [])
    if not views:
        # Also try to discover views through child blocks
        try:
            children = _api("GET", "/blocks/" + books_db_id + "/children",
                            params={"page_size": 100})
            for block in children.get("results", []):
                if block.get("type") == "collection_view":
                    views.append(block)
        except Exception:
            pass

    for view in views:
        vid = view.get("id", "")
        vformat = view.get("format", {}) or {}
        if vformat.get("page_open") == "full_page":
            continue
        new_format = dict(vformat)
        new_format["page_open"] = "full_page"
        try:
            _api("PATCH",
                 "/databases/" + books_db_id + "/views/" + vid,
                 {"format": new_format})
            print("[View] Set page_open=full_page for '{}'".format(
                view.get("name", vid)))
        except Exception:
            pass  # API might not support this — acceptable


# ── Note Operations ───────────────────────────────────────────────────────


def _delete_book_notes(notes_db_id, book_id):
    """Delete every note whose 来源BookId equals *book_id*.

    Returns the number of deleted pages.
    """
    body = {
        "filter": {
            "property": "来源BookId",
            "rich_text": {"equals": str(book_id)},
        },
        "page_size": 100,
    }
    try:
        result = _api("POST", "/databases/" + notes_db_id + "/query", body)
    except Exception as exc:
        print("[NotesDB] Query failed: " + str(exc))
        return 0

    count = 0
    for page in result.get("results", []):
        try:
            _api("DELETE", "/blocks/" + page["id"])
            count += 1
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as exc:
            logger.warning("Failed to delete note %s: %s", page["id"], exc)
    return count


def _write_note(notes_db_id, note):
    """Write a single note page to the notes database.

    Expected *note* keys: bookId, bookName, author, categories (list),
    chapterTitle, noteType, markText, abstract, wereadUrl, readingProgress.
    """
    mark_text = note.get("markText", "")
    book_name = note.get("bookName", "")
    chapter_title = note.get("chapterTitle", "")
    note_type = note.get("noteType", BOOKMARK_TYPE)

    # Build a readable title (truncate to 60 chars)
    title = mark_text[:60] + ("..." if len(mark_text) > 60 else "")
    if not title:
        title = book_name + " - " + note_type

    properties = {
        "笔记": {
            "title": [{"type": "text", "text": {"content": title}}]
        },
        "来源书籍": {
            "rich_text": [{"type": "text", "text": {"content": book_name}}]
        },
        "作者": {
            "rich_text": [
                {"type": "text", "text": {"content": note.get("author", "")}}
            ]
        },
        "来源章节": {
            "rich_text": [{"type": "text", "text": {"content": chapter_title}}]
        },
        "笔记类型": {"select": {"name": note_type}},
        "笔记内容": {
            "rich_text": [{"type": "text", "text": {"content": mark_text[:2000]}}]
        },
        "原文摘要": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": (note.get("abstract", "") or "")[:2000]},
                }
            ]
        },
        "来源BookId": {
            "rich_text": [
                {"type": "text", "text": {"content": str(note.get("bookId", ""))}}
            ]
        },
    }

    # Optional: categories
    categories = note.get("categories") or []
    if categories:
        properties["分类"] = {
            "multi_select": [{"name": c} for c in categories if c]
        }

    # Optional: reading progress
    reading_progress = note.get("readingProgress")
    if reading_progress is not None:
        properties["阅读进度"] = {"number": reading_progress}

    # Optional: WeRead link
    weread_url = note.get("wereadUrl", "")
    if weread_url:
        properties["微信链接"] = {"url": weread_url}

    body = {
        "parent": {"database_id": notes_db_id},
        "properties": properties,
    }

    result = _api("POST", "/pages", body)
    return result["id"]


def batch_insert_notes(notes_db_id, notes, book_id=None):
    """Delete old notes for *book_id* (if given), then write *notes*.

    Returns ``(success_count, fail_count)``.
    """
    if not notes:
        return 0, 0

    # Purge previous notes for this book
    if book_id:
        deleted = _delete_book_notes(notes_db_id, book_id)
        if deleted:
            print("  Deleted {} old notes for book {}".format(deleted, book_id))

    success = 0
    failed = 0
    for note in notes:
        try:
            _write_note(notes_db_id, note)
            success += 1
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as exc:
            logger.warning("Failed to insert note: %s", exc)
            failed += 1

    return success, failed


# ── Sync Notes ────────────────────────────────────────────────────────────


def sync_notes_to_db(notes_db_id, all_notes):
    """Group *all_notes* by book and write them to the notes database.

    Returns ``(total_success, total_failed)``.
    """
    if not all_notes:
        return 0, 0

    books_map = {}
    for note in all_notes:
        bid = note.get("bookId", "unknown")
        books_map.setdefault(bid, []).append(note)

    total_success = 0
    total_failed = 0
    for book_id, book_notes in books_map.items():
        book_name = book_notes[0].get("bookName", book_id)
        print("  Writing {} notes for: {}".format(len(book_notes), book_name))
        ok, fail = batch_insert_notes(notes_db_id, book_notes, book_id=book_id)
        total_success += ok
        total_failed += fail

    return total_success, total_failed


# ── Reading Statistics Page (阅读统计) ────────────────────────────────────


def _extract_prop(page, name, prop_type="text"):
    """Extract a simple value from a Notion page property."""
    prop = (page.get("properties") or {}).get(name)
    if not prop:
        return None
    ptype = prop.get("type", "")
    if ptype == "title":
        parts = prop.get("title", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else ""
    elif ptype == "rich_text":
        parts = prop.get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else ""
    elif ptype == "number":
        return prop.get("number")
    elif ptype == "status":
        s = prop.get("status") or {}
        return s.get("name", "")
    elif ptype == "date":
        d = prop.get("date") or {}
        return d.get("start", "")
    elif ptype == "multi_select":
        return [o["name"] for o in prop.get("multi_select", []) if o.get("name")]
    return None


def _format_hours(seconds):
    """Convert seconds to a readable hours string."""
    if not seconds:
        return "0"
    hours = int(seconds) / 3600
    if hours >= 100:
        return str(int(hours))
    return "{:.1f}".format(hours)


def _find_or_create_stats_page(parent_page_id):
    """Find an existing stats page or create one under *parent_page_id*.

    Returns the page ID.
    """
    # Search among existing children
    try:
        children = _api("GET", "/blocks/" + parent_page_id + "/children",
                        params={"page_size": 100})
        for block in children.get("results", []):
            if block.get("type") == "child_page":
                cp = block.get("child_page", {})
                if cp.get("title") == "阅读统计":
                    return block["id"]
    except Exception:
        pass

    # Create new
    body = {
        "parent": {"page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "\U0001f4ca"},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": "阅读统计"}}]
            }
        },
    }
    result = _api("POST", "/pages", body)
    page_id = result["id"]
    print("[Stats] Created page: " + page_id)
    return page_id


def update_reading_stats(parent_page_id, books_db_id):
    """Create or update the reading statistics page.

    Queries the books database and builds a visual summary page with
    overview stats and a recently finished books table.
    """
    if not books_db_id:
        print("[Stats] No books database, skipping")
        return None

    # Query all books
    result = query_books_db(books_db_id)
    books = result.get("results", [])

    if not books:
        print("[Stats] No books found, skipping")
        return None

    # ── Gather statistics ─────────────────────────────────────────────
    total = len(books)
    reading_time_total = 0
    finished_books = []
    reading_books = []
    unread_books = []
    ratings = []

    for page in books:
        status = _extract_prop(page, "状态", "status") or ""
        rtime = _extract_prop(page, "阅读时长", "number") or 0
        rating = _extract_prop(page, "评分", "number")
        title = _extract_prop(page, "书名", "title") or ""
        author = _extract_prop(page, "作者", "rich_text") or ""
        finish_date = _extract_prop(page, "日期", "date") or ""

        reading_time_total += rtime
        if status == "读完":
            finished_books.append({
                "title": title, "author": author,
                "rating": rating, "date": finish_date,
            })
            if rating:
                ratings.append(rating)
        elif status == "在读":
            reading_books.append(title)
        elif status == "未读":
            unread_books.append(title)

    # ── Find / create the stats page ──────────────────────────────────
    stats_page_id = _find_or_create_stats_page(parent_page_id)

    # Clear old content
    try:
        old = _api("GET", "/blocks/" + stats_page_id + "/children",
                   params={"page_size": 100})
        for block in old.get("results", []):
            try:
                _api("DELETE", "/blocks/" + block["id"])
                time.sleep(0.1)
            except Exception:
                pass
    except Exception:
        pass

    # ── Build blocks ──────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    blocks = []

    # Overview callout
    avg_rating = "{:.1f}".format(sum(ratings) / len(ratings)) if ratings else "-"
    hours = _format_hours(reading_time_total)

    overview_lines = (
        "\U0001f4da 共 {} 本书  |  "
        "\u23f1\ufe0f 总阅读 {} 小时  |  "
        "\u2b50 平均评分 {}"
    ).format(total, hours, avg_rating)

    blocks.append({
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": overview_lines}}],
            "icon": {"type": "emoji", "emoji": "\U0001f4ca"},
        },
    })

    # Status breakdown callout
    status_lines = (
        "\u2705 读完 {} 本  |  "
        "\U0001f4d6 在读 {} 本  |  "
        "\U0001f4d5 未读 {} 本"
    ).format(len(finished_books), len(reading_books), len(unread_books))

    blocks.append({
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": status_lines}}],
            "icon": {"type": "emoji", "emoji": "\U0001f4d6"},
        },
    })

    # Recently finished books table (top 30)
    finished_books.sort(key=lambda x: x["date"], reverse=True)
    display_books = finished_books[:30]

    if display_books:
        blocks.append({
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "\u6700\u8fd1\u8bfb\u5b8c"}}],
                "color": "default",
            },
        })

        rows = [
            {
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": "\u4e66\u540d"}}],
                        [{"type": "text", "text": {"content": "\u4f5c\u8005"}}],
                        [{"type": "text", "text": {"content": "\u8bc4\u5206"}}],
                        [{"type": "text", "text": {"content": "\u8bfb\u5b8c\u65e5\u671f"}}],
                    ]
                },
            }
        ]
        for b in display_books:
            r_text = "{:.1f}".format(b["rating"]) if b["rating"] else "-"
            d_text = b["date"][:10] if b["date"] else "-"
            rows.append({
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": b["title"]}}],
                        [{"type": "text", "text": {"content": b["author"]}}],
                        [{"type": "text", "text": {"content": r_text}}],
                        [{"type": "text", "text": {"content": d_text}}],
                    ]
                },
            })

        blocks.append({
            "type": "table",
            "table": {
                "table_width": 4,
                "has_column_header": True,
                "has_row_header": False,
                "children": rows,
            },
        })

        if len(finished_books) > 30:
            blocks.append({
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "\u4ec5\u5c55\u793a\u6700\u8fd1 30 \u672c\uff0c\u5171 {} \u672c\u5df2\u8bfb\u5b8c".format(
                            len(finished_books))},
                    }],
                },
            })

    # Currently reading list
    if reading_books:
        blocks.append({
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "\u6b63\u5728\u9605\u8bfb"}}],
                "color": "default",
            },
        })
        for title in reading_books[:20]:
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": title}}],
                },
            })

    # Update timestamp
    blocks.append({
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": "\u66f4\u65b0\u4e8e " + now},
                "annotations": {"italic": True, "color": "gray"},
            }],
        },
    })

    # ── Add blocks in batches of 100 ──────────────────────────────────
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i + 100]
        try:
            _api("PATCH", "/blocks/" + stats_page_id + "/children",
                 {"children": batch})
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as exc:
            print("[Stats] Failed to add blocks: " + str(exc))

    print("[Stats] Updated reading stats page")
    return stats_page_id


# ── Dashboard Entry Point (backward-compatible) ──────────────────────────


def update_dashboard(all_notes):
    """Create or update the Personal Library dashboard.

    Backward-compatible entry point called by ``cli.sync()``.
    Extracts the parent page ID from the ``NOTION_PAGE`` environment variable,
    ensures both library databases exist, and syncs all collected notes.

    Returns ``(books_db_id, notes_db_id)`` or ``(None, None)`` on failure.
    """
    if not _get_token():
        print("[Dashboard] No NOTION_TOKEN found, skipping")
        return None, None

    parent_page_id = _extract_parent_page_id()
    if not parent_page_id:
        print("[Dashboard] No NOTION_PAGE ID found, skipping")
        return None, None

    try:
        books_db_id, notes_db_id = ensure_library(parent_page_id)

        if not all_notes:
            print("[Dashboard] No notes to sync")
            return books_db_id, notes_db_id

        total_success, total_failed = sync_notes_to_db(notes_db_id, all_notes)
        print(
            "[Dashboard] Notes written: {} success, {} failed".format(
                total_success, total_failed
            )
        )

        return books_db_id, notes_db_id

    except Exception as exc:
        print("[Dashboard] Failed: " + str(exc))
        logger.exception("Dashboard update failed")
        return None, None

import argparse
import logging
import os
import re
import time
from notion_client import Client
import requests
from datetime import datetime
import hashlib
from dotenv import load_dotenv
from retrying import retry
from .blocks import (
    get_callout,
    get_date,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_title,
    get_url,
)
from .dashboard import (
    ensure_library,
    get_property_schema,
    build_book_properties,
    delete_old_book_pages,
    query_books_db,
    rename_page,
)

client = None
books_db_id = None
weread = None

load_dotenv()
WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.3"
NOTION_VERSION = "2026-03-11"
BOOKMARK_CALLOUT_ICON = "\u3030\ufe0f"
NOTE_CALLOUT_ICON = "\u270d\ufe0f"
NOTION_TOKEN_PATTERN = re.compile(r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$")
WEREAD_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{10,}$")
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


class ConfigError(Exception):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{safe}")
    else:
        print(f"\u914d\u7f6e\u9519\u8bef: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)
    if raw is None:
        if required:
            fail_config(f"\u7f3a\u5c11 {name}\uff0c\u8bf7\u5728 GitHub Actions Secrets \u4e2d\u914d\u7f6e")
        return None
    value = re.sub(r"\s+", "", raw)
    if value:
        os.environ[name] = value
        return value
    if required:
        fail_config(f"{name} \u4e3a\u7a7a\uff0c\u8bf7\u68c0\u67e5 GitHub Actions Secrets")
    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} \u683c\u5f0f\u4e0d\u6b63\u786e\uff1a{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value("WEREAD_API_KEY", required=True)
    notion_token = clean_secret_value("NOTION_TOKEN", required=True)
    notion_page = clean_secret_value("NOTION_PAGE")

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "\u5e94\u4e3a\u5fae\u4fe1\u8bfb\u4e66 Gateway API Key\uff0c\u4e0d\u80fd\u5305\u542b\u7a7a\u683c\u6216\u6362\u884c",
    )
    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "\u5e94\u4ee5 secret_ \u6216 ntn_ \u5f00\u5934\uff0c\u4e0d\u80fd\u5305\u542b\u7a7a\u683c\u6216\u6362\u884c",
    )
    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_page):
        fail_config("NOTION_PAGE \u683c\u5f0f\u4e0d\u6b63\u786e\uff1a\u8bf7\u586b\u5199 Notion \u9875\u9762\u94fe\u63a5\u6216 ID")
    if not notion_page:
        fail_config("\u7f3a\u5c11 NOTION_PAGE\uff0c\u8bf7\u914d\u7f6e")
    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
    }


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config("\u6ca1\u6709\u627e\u5230 WEREAD_API_KEY\uff0c\u8bf7\u5728 GitHub Actions Secrets \u4e2d\u914d\u7f6e")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name, **kwargs):
        payload = {
            "api_name": api_name,
            "skill_version": WEREAD_SKILL_VERSION,
            **kwargs,
        }
        response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("upgrade_info"):
            raise Exception(f"\u5fae\u4fe1\u8bfb\u4e66 skill \u9700\u8981\u5347\u7ea7: {data.get('upgrade_info')}")
        if data.get("errcode", 0) != 0:
            raise Exception(f"\u5fae\u4fe1\u8bfb\u4e66 Gateway \u8bf7\u6c42\u5931\u8d25: {api_name}, errcode={data.get('errcode')}, response={data}")
        return data


def get_range_start(item):
    note_range = item.get("range") or ""
    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None
    if chapter:
        chapter_info = chapter.get(chapter_uid) or chapter.get(str(chapter_uid))
    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )
    return (chapter_idx, get_range_start(item))


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookmark_list(bookId):
    """\u83b7\u53d6\u6211\u7684\u5212\u7ebf"""
    data = weread.request("/book/bookmarklist", bookId=bookId)
    updated = data.get("updated") or []
    return sorted(updated, key=get_note_sort_key)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_read_info(bookId):
    data = weread.request("/book/getprogress", bookId=bookId)
    book = data.get("book") or {}
    progress = to_number(book.get("progress")) or 0
    reading_progress = normalize_reading_progress(progress)
    finish_time = book.get("finishTime") or 0
    update_time = book.get("updateTime") or 0
    if finish_time or progress >= 100:
        marked_status = 4
    elif update_time or book.get("isStartReading") or progress > 0:
        marked_status = 2
    else:
        marked_status = 1
    return {
        "markedStatus": marked_status,
        "readingTime": book.get("recordReadingTime") or 0,
        "readingProgress": reading_progress,
        "finishedDate": finish_time,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0
    if value > 1:
        value = value / 100
    return round(min(max(value, 0), 1), 4)


def normalize_rating(value):
    value = value or 0
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookinfo(bookId):
    """\u83b7\u53d6\u4e66\u7684\u8be6\u60c5"""
    data = weread.request("/book/info", bookId=bookId)
    isbn = data.get("isbn", "")
    newRating = normalize_rating(data.get("newRating"))
    return (isbn, newRating)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_review_list(bookId):
    """\u83b7\u53d6\u7b14\u8bb0"""
    reviews_data = []
    hasMore = 1
    synckey = 0
    while hasMore:
        data = weread.request("/review/list/mine", bookid=bookId, synckey=synckey, count=100)
        hasMore = data.get("hasMore", 0)
        synckey = data.get("synckey", 0)
        batch = data.get("reviews") or []
        reviews_data.extend(batch)
        if not batch:
            hasMore = 0
    summary = list(filter(lambda x: (x.get("review") or {}).get("type") == 4, reviews_data))
    reviews = list(filter(lambda x: (x.get("review") or {}).get("type") == 1, reviews_data))
    reviews = list(map(lambda x: x.get("review") or {}, reviews))
    reviews = list(
        map(
            lambda x: {
                **x,
                "markText": x.pop("content", ""),
                "_callout_icon": NOTE_CALLOUT_ICON,
            },
            reviews,
        )
    )
    return summary, reviews


def check(bookId):
    """\u68c0\u67e5\u662f\u5426\u5df2\u7ecf\u63d2\u5165\u8fc7  \u5982\u679c\u5df2\u7ecf\u63d2\u5165\u4e86\u5c31\u5220\u9664"""
    delete_old_book_pages(books_db_id, bookId)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_chapter_info(bookId):
    """\u83b7\u53d6\u7ae0\u8282\u4fe1\u606f"""
    data = weread.request("/book/chapterinfo", bookId=bookId)
    chapters = data.get("chapters") or []
    return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}


def insert_to_notion(bookName, bookId, cover, sort, author, isbn, rating, categories):
    """\u63d2\u5165\u5230notion"""
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    parent = {"database_id": books_db_id}
    raw_properties = {
        "\u4e66\u540d": bookName,
        "BookId": bookId,
        "ISBN": isbn,
        "\u94fe\u63a5": "https://weread.qq.com/web/reader/" + calculate_book_str_id(bookId),
        "\u4f5c\u8005": author,
        "Sort": sort,
        "\u8bc4\u5206": rating,
        "\u5c01\u9762": cover,
    }
    if categories != None:
        raw_properties["\u5206\u7c7b"] = categories
    read_info = get_read_info(bookId=bookId)
    if read_info != None:
        markedStatus = read_info.get("markedStatus", 0)
        readingTime = read_info.get("readingTime", 0)
        readingProgress = read_info.get("readingProgress", 0)
        # Store reading time as seconds (number)
        raw_properties["\u9605\u8bfb\u65f6\u957f"] = readingTime
        raw_properties["\u9605\u8bfb\u8fdb\u5ea6"] = readingProgress
        # Map status to match original template naming
        if markedStatus == 4:
            raw_properties["\u72b6\u6001"] = "\u8bfb\u5b8c"
        elif markedStatus == 2:
            raw_properties["\u72b6\u6001"] = "\u5728\u8bfb"
        else:
            raw_properties["\u72b6\u6001"] = "\u672a\u8bfb"
        # Store finish date
        finish_time = read_info.get("finishedDate")
        if finish_time:
            raw_properties["\u65e5\u671f"] = datetime.utcfromtimestamp(finish_time).strftime("%Y-%m-%d")

    properties = build_book_properties(raw_properties)
    icon = {"type": "external", "external": {"url": cover}}
    page_cover = {"type": "external", "external": {"url": cover}}
    # Page cover is needed for Gallery view card preview (set to "Page cover").
    # If you don't want the banner inside the page, use "Hide page cover"
    # from the page's ... menu in Notion.
    response = client.pages.create(
        parent=parent, icon=icon, cover=page_cover, properties=properties
    )
    id = response["id"]
    return id


def add_children(id, children):
    results = []
    for i in range(0, len(children) // 100 + 1):
        time.sleep(0.3)
        response = client.blocks.children.append(
            block_id=id, children=children[i * 100 : (i + 1) * 100]
        )
        results.extend(response.get("results"))
    return results if len(results) == len(children) else None


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(0.3)
        id = results[key].get("id")
        client.blocks.children.append(block_id=id, children=[value])


def get_notebooklist():
    """\u83b7\u53d6\u7b14\u8bb0\u672c\u5217\u8868"""
    books = []
    hasMore = 1
    lastSort = None
    while hasMore:
        params = {"count": 100}
        if lastSort is not None:
            params["lastSort"] = lastSort
        data = weread.request("/user/notebooks", **params)
        hasMore = data.get("hasMore", 0)
        batch = data.get("books") or []
        books.extend(batch)
        if batch:
            lastSort = batch[-1].get("sort")
        else:
            hasMore = 0
    books.sort(key=lambda x: x.get("sort") or 0)
    return books


def _build_callout(item):
    """Build a callout block from a bookmark/review item, with optional
    nested quote child.  Returns the callout block dict."""
    markText = item.get("markText") or ""
    callout_icon = item.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
    callout = get_callout(markText, icon=callout_icon)
    abstract = item.get("abstract")
    if abstract:
        callout["callout"]["children"] = [get_quote(abstract)]
    return callout


def _build_chapter_table(all_chapters, bookmark_list):
    """Build a Notion table block showing ALL chapters with note/highlight counts.

    *all_chapters* is a sorted list of chapter info dicts (by chapterIdx).
    *bookmark_list* is the pre-merge list of bookmarks (no _callout_icon)
    and reviews (_callout_icon = NOTE_CALLOUT_ICON).

    Returns a table block dict, or None if there are no chapters.
    """
    if not all_chapters:
        return None

    # Count notes and bookmarks per chapterUid
    note_counts = {}
    mark_counts = {}
    for item in bookmark_list:
        uid = item.get("chapterUid", 1)
        if item.get("_callout_icon"):
            # Review / personal note
            note_counts[uid] = note_counts.get(uid, 0) + 1
        else:
            # Underline / bookmark
            mark_counts[uid] = mark_counts.get(uid, 0) + 1

    # Build header row
    header_cells = [
        [{"type": "text", "text": {"content": "\u7ae0\u8282"}}],   # 章节
        [{"type": "text", "text": {"content": "\u6807\u9898"}}],   # 标题
        [{"type": "text", "text": {"content": "\u7b14\u8bb0"}}],   # 笔记
        [{"type": "text", "text": {"content": "\u5212\u7ebf"}}],   # 划线
    ]
    header_row = {
        "type": "table_row",
        "table_row": {"cells": header_cells},
    }

    # Build data rows — one per chapter
    data_rows = []
    for ch in all_chapters:
        uid = ch.get("chapterUid")
        level = ch.get("level", 1)
        title = ch.get("title", "")
        idx = ch.get("chapterIdx", 0)

        # Count notes and marks for this chapter
        n_notes = note_counts.get(uid, 0)
        n_marks = mark_counts.get(uid, 0)

        # Skip metadata chapters: level 0, or known boilerplate titles
        # with no notes/highlights
        skip_titles = {"\u7248\u6743\u4fe1\u606f", "\u6587\u524d", "\u63d2\u56fe"}
        if level == 0:
            continue
        if title in skip_titles and n_notes == 0 and n_marks == 0:
            continue

        # Indent sub-chapters for visual hierarchy
        prefix = "  " * (level - 1)

        # Count notes and marks for this chapter
        n_notes = note_counts.get(uid, 0)
        n_marks = mark_counts.get(uid, 0)

        # Display counts — use number if > 0, dash otherwise
        notes_text = str(n_notes) if n_notes > 0 else "-"
        marks_text = str(n_marks) if n_marks > 0 else "-"

        row_cells = [
            [{"type": "text", "text": {"content": prefix + str(len(data_rows) + 1)}}],
            [{"type": "text", "text": {"content": title}}],
            [{"type": "text", "text": {"content": notes_text}}],
            [{"type": "text", "text": {"content": marks_text}}],
        ]
        data_rows.append({
            "type": "table_row",
            "table_row": {"cells": row_cells},
        })

    all_rows = [header_row] + data_rows
    if len(all_rows) < 2:
        # Only header, no data — skip table
        return None

    return {
        "type": "table",
        "table": {
            "table_width": 4,
            "has_column_header": True,
            "has_row_header": False,
            "children": all_rows,
        },
    }


def get_children(chapter, summary, bookmark_list):
    """Build the top-level blocks for a book page.

    Returns ``(children, child_pages_data)`` where *children* is a list of
    Notion block dicts to append directly, and *child_pages_data* is a list
    of ``{"title": ..., "blocks": [...]}`` dicts — one per chapter that has
    notes — used by :func:`create_chapter_child_pages`.
    """
    children = []
    child_pages_data = []

    # ── Chapter overview table ──────────────────────────────────────────
    all_chapters = []
    if chapter:
        for uid, info in chapter.items():
            item = dict(info)
            item["chapterUid"] = item.get("chapterUid", uid)
            all_chapters.append(item)
        all_chapters.sort(key=lambda x: x.get("chapterIdx", 0))

    chapter_table = _build_chapter_table(all_chapters, bookmark_list)
    if chapter_table:
        children.append(chapter_table)

    # ── Reviews / 点评 (toggleable heading) ─────────────────────────────
    if summary is not None and len(summary) > 0:
        review_heading = get_heading(1, "\u70b9\u8bc4", toggleable=True)
        review_children = []
        for i in summary:
            content = (i.get("review") or {}).get("content") or ""
            if not content:
                continue
            for j in range(0, len(content) // 2000 + 1):
                review_children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        icon=NOTE_CALLOUT_ICON,
                    )
                )
        if review_children:
            review_heading[review_heading["type"]]["children"] = review_children
        children.append(review_heading)

    # ── Group bookmarks by chapterUid for child pages ───────────────────
    bookmarks_by_chapter = {}
    for data in bookmark_list:
        uid = data.get("chapterUid", 1)
        bookmarks_by_chapter.setdefault(uid, []).append(data)

    for uid, bookmarks in bookmarks_by_chapter.items():
        info = None
        if chapter:
            info = chapter.get(uid) or chapter.get(str(uid))
        ch_title = info.get("title", "") if info else ""
        ch_level = info.get("level", 1) if info else 1

        # Build readable page title
        if ch_title:
            page_title = "第{}章 {}".format(uid, ch_title)
        else:
            page_title = "章节 {}".format(uid)
        page_title += " ({}条笔记)".format(len(bookmarks))

        # Build callout blocks for inside the child page
        blocks = []
        for bm in bookmarks:
            markText = bm.get("markText") or ""
            if not markText:
                continue
            if len(markText) > 2000:
                for j in range(0, len(markText) // 2000 + 1):
                    chunk = markText[j * 2000 : (j + 1) * 2000]
                    icon = bm.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
                    blocks.append(get_callout(chunk, icon=icon))
            else:
                blocks.append(_build_callout(bm))

        if blocks:
            child_pages_data.append({
                "title": page_title,
                "blocks": blocks,
                "level": ch_level,
            })

    return children, child_pages_data


def create_chapter_child_pages(book_page_id, child_pages_data):
    """Create a Notion child page for each chapter that has notes.

    Child pages appear as clickable links in the book page's sidebar / body,
    and contain all the callout blocks for that chapter's notes.
    """
    created = 0
    for cp in child_pages_data:
        time.sleep(0.5)  # respect rate limits
        try:
            # Notion requires non-empty children; always satisfied here
            # because we only include chapters with blocks.
            client.pages.create(
                parent={"page_id": book_page_id},
                properties={
                    "title": {
                        "title": [{"type": "text", "text": {"content": cp["title"]}}]
                    }
                },
                children=cp["blocks"],
            )
            created += 1
        except Exception as e:
            print("  Failed to create chapter page '{}': {}".format(cp["title"], e))
    if created:
        print("  Created {} chapter pages".format(created))


def transform_id(book_id):
    id_length = len(book_id)

    if re.match(r"^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(item) for item in value if item is not None)
    return str(value)


def to_name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_date_value(value):
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return value


def ensure_library_setup():
    global books_db_id
    parent_page = os.getenv("NOTION_PAGE", "")
    if not parent_page:
        fail_config("\u7f3a\u5c11 NOTION_PAGE\uff0c\u8bf7\u5728 .env \u4e2d\u914d\u7f6e")
    # Extract UUID from URL or ID
    match = NOTION_ID_IN_TEXT_PATTERN.search(parent_page)
    if match:
        parent_page_id = match.group(0)
    else:
        fail_config("NOTION_PAGE \u683c\u5f0f\u4e0d\u6b63\u786e")
    # Rename parent page to "个人图书馆"
    rename_page(parent_page_id, "\u4e2a\u4eba\u56fe\u4e66\u9986")
    books_db_id = ensure_library(parent_page_id)
    print("Books DB: " + books_db_id)


def get_latest_sort():
    """Get the latest Sort value from our books database."""
    filter_body = {
        "property": "Sort",
        "number": {"is_not_empty": True}
    }
    sort_body = [{"property": "Sort", "direction": "descending"}]
    result = query_books_db(books_db_id, filter_body, sort_body, page_size=1)
    results = result.get("results", [])
    if results:
        props = results[0].get("properties", {})
        sort_prop = props.get("Sort", {})
        return sort_prop.get("number", 0) or 0
    return 0


def sync():
    global client, books_db_id, weread
    secrets = validate_secret_inputs()
    notion_token = secrets["notion_token"]
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )
    print(f"Notion API Version: {NOTION_VERSION}")
    ensure_library_setup()
    latest_sort = get_latest_sort()
    books = get_notebooklist()
    if books != None:
        for index, book in enumerate(books):
            sort = book["sort"]
            if sort <= latest_sort:
                continue
            book = book.get("book") or book
            title = book.get("title") or ""
            cover = (book.get("cover") or "").replace("/s_", "/t7_")
            bookId = book.get("bookId")
            author = book.get("author") or ""
            if not bookId:
                continue
            categories = book.get("categories")
            if categories != None:
                categories = [x["title"] for x in categories]
            print(f"\u6b63\u5728\u540c\u6b65 {title} ,\u4e00\u5171{len(books)}\u672c\uff0c\u5f53\u524d\u662f\u7b2c{index+1}\u672c\u3002")
            check(bookId)
            isbn, rating = get_bookinfo(bookId)
            id = insert_to_notion(
                title, bookId, cover, sort, author, isbn, rating, categories
            )
            chapter = get_chapter_info(bookId)
            bookmark_list = get_bookmark_list(bookId)
            summary, reviews = get_review_list(bookId)
            bookmark_list.extend(reviews)
            bookmark_list = sorted(
                bookmark_list,
                key=lambda x: get_note_sort_key(x, chapter),
            )
            children, child_pages_data = get_children(chapter, summary, bookmark_list)
            results = add_children(id, children)
            create_chapter_child_pages(id, child_pages_data)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description="Sync WeRead highlights and notes to Notion.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync"],
        help="Command to run. Defaults to sync.",
    )
    parser.parse_args(argv)
    try:
        sync()
    except ConfigError:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

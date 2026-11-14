"""Private PocketReader XL catalog, cache, package, and progress helpers."""

import binascii
import asyncio as aio
import hashlib
import json
import network
import os
import struct
import time
from lib import pocketreader_font_data as font_data
from lib import pocketreader_pagination as pagination
from lib import wifi_setup


CONFIG_PATH = "/data/pocketreader_config.json"
CACHE_STATE_PATH = "/data/pocketreader_books.json"
PROGRESS_PATH = "/data/pocketreader_progress.json"
READING_STATE_PATH = "/data/pocketreader_reading_state.json"
LOCAL_CATALOG_PATH = "/data/pocketreader_local_books.json"
PREFERENCES_PATH = "/data/pocketreader_preferences.json"
BOOK_DIR = "/books"


class LibraryError(Exception):
    pass


def _read_json(path, fallback=None):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def _write_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(value, handle)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(temporary, path)


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False










def light_settings():
    settings = _read_json(PREFERENCES_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    brightness = settings.get("light_brightness", "medium")
    if brightness not in ("off", "dim", "medium", "bright"):
        brightness = "medium"
    return brightness, bool(settings.get("availability_lights", False))


def set_light_setting(key, value):
    if key == "light_brightness" and value not in ("off", "dim", "medium", "bright"):
        raise LibraryError("Invalid light brightness")
    if key == "availability_lights":
        value = bool(value)
    elif key != "light_brightness":
        raise LibraryError("Invalid light setting")
    settings = _read_json(PREFERENCES_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    settings[key] = value
    _write_json(PREFERENCES_PATH, settings)


def library_sort_settings():
    settings = _read_json(PREFERENCES_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    order = settings.get('library_sort', 'title')
    if order == 'author':
        order = 'author_last'
    if order not in ('title', 'author_first', 'author_last', 'added', 'opened'):
        order = 'title'
    return order, bool(settings.get('group_series', False))


def set_library_sort_settings(order, group_series):
    if order == 'author':
        order = 'author_last'
    if order not in ('title', 'author_first', 'author_last', 'added', 'opened'):
        raise LibraryError('Invalid library sort order')
    settings = _read_json(PREFERENCES_PATH, {})
    if not isinstance(settings, dict):
        settings = {}
    settings['library_sort'] = order
    settings['group_series'] = bool(group_series)
    _write_json(PREFERENCES_PATH, settings)


def mark_opened(book_id):
    book_id = _safe_id(book_id)
    state = _read_json(READING_STATE_PATH, {})
    state['sequence'] = int(state.get('sequence', 0)) + 1
    state.setdefault('opened', {})[book_id] = state['sequence']
    _write_json(READING_STATE_PATH, state)


def author_sort_parts(author):
    """Handle common surname-last and 'surname, given names' forms."""
    name = str(author or '').strip().lower()
    if not name or name in ('unknown', 'unknown author'):
        return ('\uffff', '\uffff')
    if ',' in name:
        family, given = name.split(',', 1)
        return (given.strip() or family.strip(), family.strip())
    parts = name.split()
    if len(parts) == 1:
        return (parts[0], parts[0])
    if parts[-1].rstrip('.') in ('jr', 'sr', 'ii', 'iii', 'iv') and len(parts) > 2:
        parts = parts[:-1]
    start = len(parts) - 1
    while start > 1 and parts[start - 1] in ('de', 'del', 'der', 'di', 'la', 'le', 'van', 'von'):
        start -= 1
    return (' '.join(parts[:start]), ' '.join(parts[start:]))


def sort_books(entries):
    """Sort without relying on a clock; older books with unknown order sort last."""
    order, group = library_sort_settings()
    reading = _read_json(READING_STATE_PATH, {})
    opened = reading.get('opened', {})
    installed = installed_state()
    added = reading.setdefault('added', {})
    changed = False
    for entry in entries:
        book_id = entry.get('id')
        downloaded = installed.get(book_id, {}).get('download_order', 0)
        if book_id and book_id not in added and downloaded:
            added[book_id] = int(downloaded)
            changed = True
    if changed:
        _write_json(READING_STATE_PATH, reading)
    def text(entry, key):
        return str(entry.get(key) or '').lower()
    def sort_key(entry):
        title = text(entry, 'title')
        if order in ('author_first', 'author_last'):
            given, family = author_sort_parts(entry.get('author'))
            primary = (given, family, title) if order == 'author_first' else (family, given, title)
        elif order == 'opened':
            primary = (-int(opened.get(entry.get('id'), 0)), title)
        elif order == 'added':
            primary = (-int(added.get(entry.get('id'), 0)), title)
        else:
            primary = (title,)
        return ((text(entry, 'series') or title,) + primary) if group else primary
    return sorted(entries, key=sort_key)








def load_catalog():
    return {"schema": 1, "books": []}




def _safe_id(book_id):
    if not book_id or len(book_id) > 96:
        raise LibraryError("Invalid book identifier")
    for character in book_id:
        if not ("a" <= character <= "z" or "A" <= character <= "Z" or "0" <= character <= "9" or character in "-_"):
            raise LibraryError("Invalid book identifier")
    return book_id


def package_path(entry):
    return BOOK_DIR + "/" + _safe_id(entry["id"]) + ".prb"


def installed_state():
    return _read_json(CACHE_STATE_PATH, {})


def local_books():
    return _read_json(LOCAL_CATALOG_PATH, {}).get("books", [])


def is_installed(entry):
    state = installed_state().get(entry.get("id"), {})
    return state.get("sha256") == entry.get("sha256") and _exists(package_path(entry))


def has_download(entry):
    return entry.get("id") in installed_state() and _exists(package_path(entry))


def text_size():
    size = _read_json(PREFERENCES_PATH, {}).get("text_size", "medium")
    return size if size in ("small", "medium", "large", "xl") else "medium"


def set_text_size(size):
    if size not in ("small", "medium", "large", "xl"):
        raise LibraryError("Invalid text size")
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences["text_size"] = size
    _write_json(PREFERENCES_PATH, preferences)


def font_family():
    preferences = _read_json(PREFERENCES_PATH, {})
    family = preferences.get('font_family', 'opendyslexic') if isinstance(preferences, dict) else 'opendyslexic'
    return family if family in ('opendyslexic', 'ubuntu', 'tinos') else 'opendyslexic'


def set_font_family(family):
    if family not in ('opendyslexic', 'ubuntu', 'tinos'):
        raise LibraryError('Invalid font')
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences['font_family'] = family
    _write_json(PREFERENCES_PATH, preferences)


def idle_light_minutes():
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        return 5
    value = preferences.get("idle_light_minutes")
    if type(value) is int and value in (0, 1, 5, 10):
        return value
    # Preserve the previous opt-out when upgrading the on/off setting.
    return 0 if preferences.get("idle_lights_enabled") is False else 5


def set_idle_light_minutes(minutes):
    if type(minutes) is not int or minutes not in (0, 1, 5, 10):
        raise LibraryError("Invalid light sleep time")
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences["idle_light_minutes"] = minutes
    _write_json(PREFERENCES_PATH, preferences)


def sleep_timer_minutes():
    preferences = _read_json(PREFERENCES_PATH, {})
    value = preferences.get('sleep_timer_minutes', 0) if isinstance(preferences, dict) else 0
    return value if value in (0, 5, 10, 30) else 0


def set_sleep_timer_minutes(minutes):
    if minutes not in (0, 5, 10, 30):
        raise LibraryError('Invalid sleep timer')
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences['sleep_timer_minutes'] = minutes
    _write_json(PREFERENCES_PATH, preferences)


def reading_light_mode():
    preferences = _read_json(PREFERENCES_PATH, {})
    mode = preferences.get("reading_light_mode", "off") if isinstance(preferences, dict) else "off"
    return mode if mode in ("off", "book", "chapter") else "off"


def set_reading_light_mode(mode):
    if mode not in ("off", "book", "chapter"):
        raise LibraryError("Invalid reading light mode")
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences["reading_light_mode"] = mode
    _write_json(PREFERENCES_PATH, preferences)


def quick_boot_enabled():
    preferences = _read_json(PREFERENCES_PATH, {})
    return isinstance(preferences, dict) and preferences.get("quick_boot") is True


def set_quick_boot_enabled(enabled):
    preferences = _read_json(PREFERENCES_PATH, {})
    if not isinstance(preferences, dict):
        preferences = {}
    preferences["quick_boot"] = bool(enabled)
    _write_json(PREFERENCES_PATH, preferences)


def version_for(entry, size):
    versions = entry.get("versions", {})
    version = versions.get(size)
    if not version:
        if size != "medium":
            raise LibraryError("Library needs new text-size versions")
        return entry
    selected = dict(entry)
    selected.update(version)
    return selected


def remove_download(entry):
    """Remove only the device copy; the catalog and progress remain intact."""
    path = package_path(entry)
    if _exists(path):
        os.remove(path)
    for size in ("small", "medium", "large", "xl"):
        try:
            os.remove(path + "." + size + ".pages")
        except OSError:
            pass
    state = installed_state()
    if state.get(entry["id"], {}).get("local_only"):
        local = _read_json(LOCAL_CATALOG_PATH, {})
        local["books"] = [item for item in local.get("books", []) if item.get("id") != entry["id"]]
        _write_json(LOCAL_CATALOG_PATH, local)
    state.pop(entry["id"], None)
    _write_json(CACHE_STATE_PATH, state)


def _next_sequence():
    reading = _read_json(READING_STATE_PATH, {})
    sequence = int(reading.get("sequence", 0)) + 1
    reading["sequence"] = sequence
    _write_json(READING_STATE_PATH, reading)
    return sequence


def mark_read(book_id):
    book_id = _safe_id(book_id)
    reading = _read_json(READING_STATE_PATH, {})
    sequence = int(reading.get("sequence", 0)) + 1
    reading["sequence"] = sequence
    reading.setdefault("books", {})[book_id] = sequence
    _write_json(READING_STATE_PATH, reading)
    path = BOOK_DIR + "/" + book_id + ".prp"
    state = _read_book_progress(book_id)
    state["book_id"] = book_id
    state["last_read_order"] = sequence
    state.setdefault("bookmarks", [])
    _write_json(path, state)


def most_recent_read(entries):
    orders = _read_json(READING_STATE_PATH, {}).get("books", {})
    ranked = [(int(orders.get(entry.get("id"), 0)), index)
              for index, entry in enumerate(entries)]
    if not ranked:
        return None
    order, index = max(ranked)
    return index if order > 0 else None


def ensure_space(required, protected_id):
    """Evict oldest unread downloads first, then least recently read books."""
    state = installed_state()
    read_order = _read_json(READING_STATE_PATH, {}).get("books", {})
    candidates = []
    for book_id, metadata in state.items():
        if book_id == protected_id or metadata.get("local_only") or not _exists(package_path({"id": book_id})):
            continue
        order = read_order.get(book_id)
        candidates.append((0 if order is None else 1,
                           int(metadata.get("download_order", 0)) if order is None else int(order),
                           book_id))
    candidates.sort()
    def free_bytes():
        stats = os.statvfs(BOOK_DIR)
        return stats[0] * stats[3]
    available = free_bytes()
    if available < required:
        for _, _, book_id in candidates:
            available += os.stat(package_path({"id": book_id}))[6]
        if available < required:
            raise LibraryError("Not enough storage for this book")
    for _, _, book_id in candidates:
        if free_bytes() >= required:
            return
        remove_download({"id": book_id})
    if free_bytes() < required:
        raise LibraryError("Not enough storage for this book")








def load_progress():
    progress = _read_json(PROGRESS_PATH, {})
    try:
        files = os.listdir(BOOK_DIR)
    except OSError:
        return progress
    for name in files:
        if name.endswith(".prxl") or name.endswith(".prp"):
            book_id = name[:-5] if name.endswith(".prxl") else name[:-4]
            saved = _read_book_progress(book_id)
            if saved.get("book_id") == book_id:
                progress[book_id] = saved
    return progress


def _read_book_progress(book_id):
    book_id = _safe_id(book_id)
    path = BOOK_DIR + "/" + book_id + ".prp"
    if _exists(path):
        return _read_json(path, {})
    return _read_json(BOOK_DIR + "/" + book_id + ".prxl", {})


def save_progress(book_id, page, word):
    book_id = _safe_id(book_id)
    path = BOOK_DIR + "/" + book_id + ".prp"
    progress = _read_book_progress(book_id)
    progress.update({"book_id": book_id, "page": int(page), "word": int(word)})
    progress.setdefault("bookmarks", [])
    _write_json(path, progress)


def bookmarks(book_id):
    book_id = _safe_id(book_id)
    value = _read_book_progress(book_id)
    return sorted(set(int(word) for word in value.get("bookmarks", []) if int(word) >= 0))


def add_bookmark(book_id, word):
    book_id = _safe_id(book_id)
    path = BOOK_DIR + "/" + book_id + ".prp"
    state = _read_book_progress(book_id)
    state["book_id"] = book_id
    saved = sorted(set(int(item) for item in state.get("bookmarks", [])) | {int(word)})
    state["bookmarks"] = saved
    _write_json(path, state)


def remove_bookmarks(book_id, words):
    """Remove these bookmark positions without disturbing reading progress."""
    book_id = _safe_id(book_id)
    path = BOOK_DIR + "/" + book_id + ".prp"
    state = _read_book_progress(book_id)
    removed = set(int(word) for word in words)
    state["bookmarks"] = [int(word) for word in state.get("bookmarks", [])
                          if int(word) not in removed]
    _write_json(path, state)


class BookPackage:
    def __init__(self, entry):
        self.entry = entry
        self.handle = open(package_path(entry), "rb")
        self.header = json.loads(self.handle.readline())
        if self.header.get("schema") not in (1, 2, 3) or self.header.get("id") != entry.get("id"):
            self.handle.close()
            raise LibraryError("Book package metadata does not match catalog")
        self.data_offset = self.handle.tell()
        self.body_end = (self.data_offset + int(self.header["body_length"])
                         if "body_length" in self.header else None)
        self.pages = self.header.get("pages", [])
        self.size = None
        self.font_family = 'opendyslexic'
        self.advances = None
        self.lines_per_page = 0

    def _lines(self, position=None):
        return pagination.layout_lines(self.handle, self.data_offset, self.advances,
                                       position, self.body_end)

    def _index_path(self):
        return BOOK_DIR + "/" + _safe_id(self.entry["id"]) + ".pri"

    def chapters(self):
        index = _read_json(self._index_path(), {})
        if (not isinstance(index, dict) or index.get("version") != 1
                or index.get("book_id") != self.entry.get("id")
                or index.get("sha256") != self.entry.get("sha256")):
            return []
        chapters = index.get("chapters", [])
        if not isinstance(chapters, list) or len(chapters) > 512:
            return []
        previous = -1
        total = int(self.header.get("word_count", 0))
        for chapter in chapters:
            if (not isinstance(chapter, list) or len(chapter) != 2
                    or type(chapter[0]) is not int or chapter[0] < previous
                    or chapter[0] < 0 or chapter[0] >= total
                    or not isinstance(chapter[1], str) or not chapter[1] or len(chapter[1]) > 120):
                return []
            previous = chapter[0]
        return chapters

    def _read_size_index(self, size, advances, lines):
        index = _read_json(self._index_path(), {})
        metrics = binascii.hexlify(hashlib.sha256(bytes(advances)).digest()).decode()
        if (index.get("version") != 1 or index.get("book_id") != self.entry["id"]
                or index.get("sha256") != self.entry.get("sha256")):
            return None
        sizes = index.get("sizes", {})
        if not isinstance(sizes, dict):
            return None
        selected = sizes.get(size, {})
        if not isinstance(selected, dict):
            return None
        if (selected.get("layout_version") != pagination.PAGINATION_VERSION or selected.get("metrics_sha256") != metrics
                or selected.get("lines") != lines):
            return None
        pages = selected.get("pages")
        if selected.get("encoding") == "delta-varint-base64":
            try:
                pages = pagination.unpack_page_positions(pages)
            except (ValueError, TypeError):
                return None
        if not isinstance(pages, list) or not pages:
            return None
        body_limit = ((self.body_end - self.data_offset) if self.body_end is not None
                      else os.stat(package_path(self.entry))[6] - self.data_offset)
        previous_offset, previous_word = -1, -1
        for page in pages:
            if not isinstance(page, list) or len(page) != 3:
                return None
            if any(not isinstance(value, int) or value < 0 for value in page):
                return None
            if page[0] >= body_limit or page[0] < previous_offset or page[2] < previous_word:
                return None
            previous_offset, previous_word = page[0], page[2]
        # PRI positions are relative to the book body so the converter and
        # badge use the same portable format; the reader seeks absolute offsets.
        return [[self.data_offset + page[0], page[1], page[2]] for page in pages]

    def _save_size_index(self, size, advances, lines, pages):
        path = self._index_path()
        index = _read_json(path, {})
        if (index.get("version") != 1 or index.get("book_id") != self.entry["id"]
                or index.get("sha256") != self.entry.get("sha256")):
            index = {"version": 1, "book_id": self.entry["id"],
                     "sha256": self.entry.get("sha256"), "sizes": {}}
        index.setdefault("sizes", {})[size] = {
            "layout_version": pagination.PAGINATION_VERSION, "lines": lines,
            "metrics_sha256": binascii.hexlify(hashlib.sha256(bytes(advances)).digest()).decode(),
            "encoding": "delta-varint-base64",
            "pages": pagination.pack_page_positions([[page[0] - self.data_offset, page[1], page[2]] for page in pages])}
        _write_json(path, index)

    def _font_metrics(self, size, family):
        suffix = {'small': '18', 'medium': '24', 'large': '30', 'xl': '42'}[size]
        prefix = {'opendyslexic': 'OPEN_DYSLEXIC', 'ubuntu': 'UBUNTU', 'tinos': 'TINOS'}[family]
        lines = {'small': 32, 'medium': 24, 'large': 19, 'xl': 14}[size]
        return getattr(font_data, prefix + suffix + '_ADVANCES'), lines

    async def prepare(self, size, family='opendyslexic'):
        if self.header["schema"] == 1:
            return
        self.size = size
        self.font_family = family
        self.advances, self.lines_per_page = self._font_metrics(size, family)
        key = size if family == 'opendyslexic' else family + ':' + size
        saved = self._read_size_index(key, self.advances, self.lines_per_page)
        if saved:
            self.pages = saved
            return
        embedded = self._embedded_index(key, self.advances, self.lines_per_page)
        if embedded is not None:
            self.pages = embedded
            self._save_size_index(key, self.advances, self.lines_per_page, embedded)
            return
        pages = []
        count = 0
        for offset, line_index, word, text in self._lines():
            if text is None:
                count = 0
                continue
            if count == 0:
                if not text:
                    continue
                pages.append([offset, line_index, word])
            count += 1
            if count >= self.lines_per_page:
                count = 0
            if len(pages) % 32 == 0:
                await aio.sleep_ms(0)
        if not pages:
            raise LibraryError("Book has no readable text")
        self.pages = pages
        self._save_size_index(key, self.advances, self.lines_per_page, pages)

    def has_index(self, size, family='opendyslexic'):
        if self.header.get("schema") == 1:
            return True
        advances, count = self._font_metrics(size, family)
        key = size if family == 'opendyslexic' else family + ':' + size
        if self._embedded_metadata(key, advances, count):
            return True
        return bool(self._read_size_index(key, advances, count))

    def _embedded_metadata(self, size, advances, lines):
        if (self.header.get("schema") != 3 or self.header.get("index_version") != 1
                or pagination.PAGINATION_VERSION != 1):
            return None
        meta = self.header.get("indexes", {}).get(size, {})
        metrics = binascii.hexlify(hashlib.sha256(bytes(advances)).digest()).decode()
        if meta.get("metrics_sha256") != metrics or meta.get("lines") != lines:
            return None
        offset, count = int(meta.get("offset", -1)), int(meta.get("count", 0))
        if offset < 0 or count < 1 or count > 100000 or self.body_end is None:
            return None
        file_size = os.stat(package_path(self.entry))[6]
        if self.body_end + offset + count * 12 > file_size:
            return None
        return (offset, count)

    def _embedded_index(self, size, advances, lines):
        meta = self._embedded_metadata(size, advances, lines)
        if meta is None:
            return None
        offset, count = meta
        self.handle.seek(self.body_end + offset)
        pages = []
        for _ in range(count):
            record = self.handle.read(12)
            if len(record) != 12:
                raise LibraryError("Book page index is incomplete")
            position, line, word = struct.unpack("<III", record)
            if position >= self.body_end - self.data_offset:
                raise LibraryError("Book page index is invalid")
            pages.append([self.data_offset + position, line, word])
        return pages

    async def locate_excerpt(self, excerpt, previous_word):
        """Match a legacy page's text to its word position in a reflowable book."""
        if self.header.get("schema") not in (2, 3) or not excerpt:
            return None
        needle = excerpt.strip()[:60]
        self.handle.seek(self.data_offset)
        word = 0
        best = None
        while True:
            if self.body_end is not None and self.handle.tell() >= self.body_end:
                return best
            record = self.handle.readline()
            if not record:
                return best
            text = record.decode("utf-8", "replace")
            found = text.find(needle)
            if found >= 0:
                candidate = word + len(text[:found].split())
                if best is None or abs(candidate - previous_word) < abs(best - previous_word):
                    best = candidate
            if text.strip() != "\x1e":
                word += len(text.split())
            if word % 1024 < 20:
                await aio.sleep_ms(0)

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def read_page(self, page):
        page = max(0, min(int(page), len(self.pages) - 1))
        offset, length, start_word = self.pages[page]
        if self.header["schema"] in (2, 3):
            lines = []
            for record_offset, line_index, _, text in self._lines(offset):
                if record_offset == offset and line_index < length:
                    continue
                if text is None:
                    break
                lines.append(text)
                if len(lines) >= self.lines_per_page:
                    break
            return lines, start_word
        self.handle.seek(self.data_offset + offset)
        text = self.handle.read(length).decode("utf-8", "replace")
        return text.split("\n"), start_word

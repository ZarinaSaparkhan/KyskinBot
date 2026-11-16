"""Чтение личного контента и выбор без недавних повторов."""

import hashlib
import json
import logging
import random
import sqlite3
from contextlib import closing
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "media"
MESSAGES_FILE = BASE_DIR / "messages.json"
QUOTES_FILE = BASE_DIR / "quotes.json"
HISTORY_FILE = BASE_DIR / "history.sqlite3"
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".mp4", ".gif"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_OTHER_MEDIA_BYTES = 50 * 1024 * 1024


def list_media(mood: str | None = None, extensions: set[str] | None = None) -> list[str]:
    """Общая коллекция котика из media/ без деления по настроениям."""
    if not MEDIA_DIR.is_dir():
        return []
    allowed = MEDIA_EXTENSIONS if extensions is None else MEDIA_EXTENSIONS & extensions
    return sorted(
        str(path.relative_to(BASE_DIR))
        for path in MEDIA_DIR.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(MEDIA_DIR).parts)
        and path.suffix.lower() in allowed
        and path.stat().st_size <= (
            MAX_PHOTO_BYTES if path.suffix.lower() in PHOTO_EXTENSIONS
            else MAX_OTHER_MEDIA_BYTES
        )
    )


def load_messages(mood: str) -> list[str]:
    """Только тексты, которые владелица сама записала в messages.json."""
    if not MESSAGES_FILE.exists():
        return []
    try:
        data = json.loads(MESSAGES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Корень JSON должен быть объектом")
        entries = data.get(mood, []) + data.get("common", [])
        if not isinstance(entries, list):
            raise ValueError("Каждое настроение должно содержать список")
        return list(dict.fromkeys(text.strip() for text in entries if isinstance(text, str) and text.strip()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logging.exception("Не удалось прочитать messages.json")
        return []


def load_quotes(mood: str) -> list[str]:
    """Цитаты с автором; данные об источниках остаются только в файле."""
    if not QUOTES_FILE.exists():
        return []
    try:
        data = json.loads(QUOTES_FILE.read_text(encoding="utf-8"))
        entries = data.get(mood, [])
        if not isinstance(entries, list):
            raise ValueError("Цитаты настроения должны быть списком")
        formatted = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            quote = entry.get("text", "").strip()
            author = entry.get("author", "").strip()
            work = entry.get("work", "").strip()
            source = entry.get("source", "").strip()
            if quote and author and work and source.startswith("https://"):
                formatted.append(f"«{quote}»\n— {author}")
        return list(dict.fromkeys(formatted))
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        logging.exception("Не удалось прочитать quotes.json")
        return []


def _content_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(HISTORY_FILE)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sent_history ("
        "id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL, content_id TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS current_cycle ("
        "chat_id INTEGER NOT NULL, kind TEXT NOT NULL, content_id TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, kind, content_id))"
    )
    connection.execute(
        'CREATE INDEX IF NOT EXISTS sent_history_lookup '
        'ON sent_history (chat_id, kind, id DESC)'
    )
    return connection


def choose_fresh(chat_id: int, kind: str, values: list[str]) -> str | None:
    """Каждый вариант показывается один раз за круг, затем круг начинается заново."""
    options = list(dict.fromkeys(values))
    if not options:
        return None
    recent_count = min(3, len(options) - 1)
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT content_id FROM sent_history WHERE chat_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, kind, recent_count),
        ).fetchall()
        used_rows = connection.execute(
            "SELECT content_id FROM current_cycle WHERE chat_id = ? AND kind = ?",
            (chat_id, kind),
        ).fetchall()
        used = {row[0] for row in used_rows}
        unseen = [value for value in options if _content_id(value) not in used]
        if not unseen:
            with connection:
                connection.executemany(
                    "DELETE FROM current_cycle WHERE chat_id = ? AND kind = ? AND content_id = ?",
                    ((chat_id, kind, _content_id(value)) for value in options),
                )
            unseen = options
    recent = {row[0] for row in rows}
    available = [value for value in unseen if _content_id(value) not in recent]
    return random.choice(available or unseen)


def mark_sent(chat_id: int, kind: str, value: str) -> None:
    """Записываем элемент только после успешной отправки в Telegram."""
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO sent_history (chat_id, kind, content_id) VALUES (?, ?, ?)",
                (chat_id, kind, _content_id(value)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO current_cycle (chat_id, kind, content_id) VALUES (?, ?, ?)",
                (chat_id, kind, _content_id(value)),
            )


# Новая библиотека отделена от старых личных сообщений и цитат.
CONTENT_DIR = BASE_DIR / 'content'
MOOD_FILES = {'good': 'facts.json', 'neutral': 'quotes.json', 'bad': 'jokes.json'}
_library_cache: dict[str, tuple[int, dict[str, dict]]] = {}


def load_mood_content(mood: str) -> dict[str, dict]:
    """Идентификаторы стабильны при переименовании файлов и перезапуске."""
    if mood not in MOOD_FILES:
        return {}
    path = CONTENT_DIR / MOOD_FILES[mood]
    stamp = path.stat().st_mtime_ns
    cached = _library_cache.get(mood)
    if cached and cached[0] == stamp:
        return cached[1]
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        raise ValueError(f'{path.name}: ожидается список материалов')
    entries = {}
    texts = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f'{path.name}: неверная запись')
        item_id, text = item.get('id'), item.get('text')
        if not isinstance(item_id, str) or not isinstance(text, str) or not text.strip():
            raise ValueError(f'{path.name}: нужны id и text')
        normalized = ' '.join(text.casefold().replace('ё', 'е').split())
        if item_id in entries or normalized in texts:
            raise ValueError(f'{path.name}: повтор идентификатора или текста')
        if mood == 'neutral' and not item.get('author'):
            raise ValueError(f'{path.name}: у цитаты нет автора')
        if mood in {'good', 'neutral'} and not str(item.get('source', '')).startswith('https://'):
            raise ValueError(f'{path.name}: нет источника')
        suffix = f"\n— {item['author']}" if mood == 'neutral' else ''
        # Telegram ограничивает подпись 1024 символами UTF-16. Оставляем место префиксам.
        if len((text + suffix).encode('utf-16-le')) // 2 > 850:
            raise ValueError(f'{path.name}: текст слишком длинный для подписи')
        entries[item_id] = item
        texts.add(normalized)
    _library_cache[mood] = (stamp, entries)
    return entries


def validate_mood_content(expected_count: int | None = None) -> dict[str, int]:
    counts = {mood: len(load_mood_content(mood)) for mood in MOOD_FILES}
    if any(count == 0 for count in counts.values()):
        raise ValueError(f'Библиотека настроения пуста: {counts}')
    if expected_count is not None and any(count != expected_count for count in counts.values()):
        raise ValueError(f'В каждой базе должно быть {expected_count} записей, найдено: {counts}')
    return counts


def _ensure_views(connection: sqlite3.Connection) -> None:
    connection.execute(
        'CREATE TABLE IF NOT EXISTS active_views ('
        'chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, mood TEXT, stage TEXT NOT NULL)'
    )


def get_active_view(chat_id: int) -> dict | None:
    with closing(_connect()) as connection:
        _ensure_views(connection)
        row = connection.execute(
            'SELECT message_id, mood, stage FROM active_views WHERE chat_id = ?', (chat_id,)
        ).fetchone()
    return dict(zip(('message_id', 'mood', 'stage'), row)) if row else None


def _set_view(connection: sqlite3.Connection, chat_id: int, message_id: int, mood: str | None, stage: str) -> None:
    _ensure_views(connection)
    connection.execute(
        'INSERT INTO active_views (chat_id, message_id, mood, stage) VALUES (?, ?, ?, ?) '
        'ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id, mood=excluded.mood, stage=excluded.stage',
        (chat_id, message_id, mood, stage),
    )


def set_active_view(chat_id: int, message_id: int, mood: str | None, stage: str) -> None:
    with closing(_connect()) as connection:
        with connection:
            _set_view(connection, chat_id, message_id, mood, stage)


def commit_delivery(chat_id: int, records: list[tuple[str, str]], message_id: int, mood: str | None, stage: str) -> None:
    """Одна транзакция для медиа, пожелания и текущих кнопок после отправки."""
    with closing(_connect()) as connection:
        with connection:
            for kind, value in records:
                content_id = _content_id(value)
                connection.execute(
                    'INSERT INTO sent_history (chat_id, kind, content_id) VALUES (?, ?, ?)',
                    (chat_id, kind, content_id),
                )
                connection.execute(
                    'INSERT OR IGNORE INTO current_cycle (chat_id, kind, content_id) VALUES (?, ?, ?)',
                    (chat_id, kind, content_id),
                )
            _set_view(connection, chat_id, message_id, mood, stage)

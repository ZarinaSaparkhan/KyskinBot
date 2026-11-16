"""Показывает количество файлов по форматам, не открывая личные фото и видео."""

from collections import Counter
from pathlib import Path


MEDIA_DIR = Path(__file__).resolve().parent / "media"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4"}
ANIMATION_EXTENSIONS = {".gif"}
SUPPORTED = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS | ANIMATION_EXTENSIONS
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_OTHER_MEDIA_BYTES = 50 * 1024 * 1024


def main() -> None:
    files = [
        path
        for path in MEDIA_DIR.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    ]
    counts = Counter(path.suffix.lower() or "(без расширения)" for path in files)
    oversized = [
        path for path in files
        if path.suffix.lower() in SUPPORTED
        and path.stat().st_size > (
            MAX_PHOTO_BYTES if path.suffix.lower() in PHOTO_EXTENSIONS
            else MAX_OTHER_MEDIA_BYTES
        )
    ]
    print(f"Всего файлов: {len(files)}")
    print(f"Фото для финала: {sum(counts[ext] for ext in PHOTO_EXTENSIONS)}")
    print(f"Видео и GIF: {sum(counts[ext] for ext in VIDEO_EXTENSIONS | ANIMATION_EXTENSIONS)}")
    print(f"Слишком большие для отправки ботом: {len(oversized)}")
    print("По форматам:")
    for extension, count in sorted(counts.items()):
        label = "подходит" if extension in SUPPORTED else "нужна подготовка"
        print(f"  {extension}: {count} — {label}")


if __name__ == "__main__":
    main()

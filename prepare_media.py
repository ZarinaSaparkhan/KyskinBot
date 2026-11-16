"""Готовит копии экспорта «Фото» для бота. Оригиналы не меняются.

python prepare_media.py incoming --inspect
python prepare_media.py incoming
"""
import argparse
from collections import Counter
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

BASE_DIR = Path(__file__).resolve().parent
PHOTO_INPUTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.tif', '.tiff', '.webp'}
VIDEO_INPUTS = {'.mov', '.mp4', '.m4v'}
INPUTS = PHOTO_INPUTS | VIDEO_INPUTS | {'.gif'}
MAX_PHOTO = 10_000_000
MAX_VIDEO = 48_000_000
MIN_FREE_BYTES = 2 * 1024**3
PREFETCH_COUNT = 4
PREFETCH_MAX_BYTES = 64 * 1024**2


def free_space(path):
    return shutil.disk_usage(path).free


def needs_download(path):
    return bool(getattr(path.stat(), 'st_flags', 0) & getattr(stat, 'SF_DATALESS', 0x40000000))


def same_source(entry, meta):
    # Времена файлов iCloud могут округляться через NSDate/double с погрешностью
    # в сотни наносекунд. Допуск 1 мкс сохраняет обнаружение обычных изменений.
    return bool(entry and entry.get('source_size')==meta.st_size
                and entry.get('source_mtime_ns') is not None
                and abs(entry['source_mtime_ns']-meta.st_mtime_ns)<=1000)


def prefetched_files(files, output, by_path, folder, min_free_bytes):
    """Небольшой запас облачных оригиналов; память и расход диска ограничены."""
    def warm(path):
        try:
            meta = path.stat()
            previous = by_path.get(str(path.relative_to(folder)))
            if same_source(previous,meta):
                return
            if meta.st_size > PREFETCH_MAX_BYTES:
                return
            reserve = min_free_bytes+2*(MAX_VIDEO+2_000_000)+PREFETCH_COUNT*PREFETCH_MAX_BYTES
            if free_space(output) < reserve:
                return
            with path.open('rb') as stream:
                while stream.read(1024*1024):
                    pass
        except OSError:
            # Основная обработка повторит чтение и запишет ошибку в отчёт.
            pass

    with ThreadPoolExecutor(max_workers=PREFETCH_COUNT) as pool:
        queue = deque()
        cursor = 0
        for _ in range(min(PREFETCH_COUNT,len(files))):
            queue.append(pool.submit(warm,files[cursor]))
            cursor += 1
        for index, path in enumerate(files,1):
            queue.popleft().result()
            if cursor < len(files):
                queue.append(pool.submit(warm,files[cursor]))
                cursor += 1
            yield index,path


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


@lru_cache(maxsize=1)
def ffmpeg_exe():
    import imageio_ffmpeg
    if os.getenv('IMAGEIO_FFMPEG_EXE'):
        return imageio_ffmpeg.get_ffmpeg_exe()
    # Исполняемый файл хранится локально, вне оптимизируемой папки iCloud.
    # Его оригинал — уже установленный официальный пакет imageio-ffmpeg.
    binaries = sorted((Path(imageio_ffmpeg.__file__).parent/'binaries').glob('ffmpeg-*'))
    source = next((p for p in binaries if p.is_file()), None)
    if source is None:
        return imageio_ffmpeg.get_ffmpeg_exe()
    directory = Path.home()/'Library'/'Caches'/'KyskinBot'/'bin'
    directory.mkdir(parents=True,exist_ok=True)
    target = directory/source.name
    if not target.is_file() or target.stat().st_size != source.stat().st_size:
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source,temporary)
            temporary.chmod(0o755)
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
    subprocess.run([str(target),'-version'],capture_output=True,text=True,check=True,timeout=60)
    return str(target)


def execute(args, *, timeout=3600, allow_probe=False):
    process = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             errors='replace', timeout=timeout)
    if process.returncode and not allow_probe:
        raise RuntimeError(process.stderr[-1600:].strip() or 'Не удалось подготовить файл')
    return process.stderr


def video_info(path):
    # ffmpeg поставляется вместе с imageio-ffmpeg; отдельная установка VLC не нужна.
    text = execute([ffmpeg_exe(), '-hide_banner', '-nostdin', '-i', str(path)], timeout=60, allow_probe=True)
    video_lines = [line for line in text.splitlines() if 'Stream #' in line and 'Video:' in line]
    if not video_lines:
        raise ValueError('В файле нет читаемой видеодорожки')
    duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', text)
    if not duration:
        raise ValueError('Не удалось определить продолжительность видео')
    seconds = int(duration[1])*3600 + int(duration[2])*60 + float(duration[3])
    if seconds <= 0:
        raise ValueError('Продолжительность видео должна быть больше нуля')
    line = video_lines[0]
    return {'duration': seconds, 'hdr': bool(re.search(r'smpte2084|arib-std-b67', line)),
            'h264': 'Video: h264' in line, 'yuv420p': bool(re.search(r'\byuv420p\b', line))}


def convert_video(source, target, *, edge=1080, crf=23):
    info = video_info(source)
    # Перевод HDR iPhone в SDR делает картинку пригодной для обычных экранов.
    tone = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
            'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,') if info['hdr'] else ''
    common = [ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
              '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1',
              '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-threads', '2',
              '-color_range', 'tv', '-c:a', 'aac', '-movflags', '+faststart', '-metadata:s:v:0', 'rotate=0']
    for attempt in range(2):
        size = edge if attempt == 0 else min(edge, 720)
        scale = f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv"
        args = common + ['-vf', tone+scale+',fps=30,format=yuv420p']
        if attempt == 0:
            args += ['-crf', str(crf), '-b:a', '96k' if edge<=720 else '128k']
        else:
            budget = int(MAX_VIDEO*8/info['duration']*0.88) - 64_000
            if budget < 64_000:
                raise ValueError('Видео слишком длинное для лимита бота; нужна отдельная подготовка')
            bitrate = min(2_500_000, budget)
            args += ['-b:v', str(bitrate), '-maxrate', str(bitrate), '-bufsize', str(bitrate*2), '-b:a', '64k']
        if info['hdr']:
            args += ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
        # Ограничение первой попытки предотвращает разрастание временного файла.
        # Размер выше лимита допустимого результата, поэтому оборванное видео
        # не принимается; затем выполняется новая попытка с расчётным битрейтом.
        execute(args + ['-fs', str(MAX_VIDEO+1_000_000), str(target)])
        if 0 < target.stat().st_size <= MAX_VIDEO:
            result = video_info(target)
            if not result['h264'] or not result['yuv420p']:
                raise ValueError('Видео не преобразовано в совместимый формат')
            if abs(result['duration']-info['duration']) > max(0.25, info['duration']*0.01):
                raise ValueError('После преобразования изменилась длительность видео')
            return
    raise ValueError('После сжатия видео всё ещё превышает лимит бота')


def convert_photo(source, target):
    sips = shutil.which('sips')
    if not sips:
        raise RuntimeError('Для HEIC и подготовки фотографий нужен Mac с программой sips')
    meta = subprocess.run([sips, '-g', 'pixelWidth', '-g', 'pixelHeight', str(source)],
                          capture_output=True, text=True, timeout=60)
    if meta.returncode or not re.search(r'pixelWidth:\s*[1-9]\d*', meta.stdout):
        raise ValueError('Не удалось прочитать изображение')
    execute([sips, '-s', 'format', 'jpeg', '-s', 'formatOptions', '85', '--resampleHeightWidthMax',
             '2048', str(source), '--out', str(target)], timeout=120)
    if not 0 < target.stat().st_size <= MAX_PHOTO:
        raise ValueError('Фото превышает лимит после подготовки')


def source_files(folder):
    return sorted(p for p in folder.rglob('*')
                  if p.is_file() and not any(part.startswith('.') for part in p.relative_to(folder).parts)
                  and p.suffix.lower() in INPUTS)


def import_folder(folder, output=None, *, video_edge=1080, video_crf=23, min_free_bytes=MIN_FREE_BYTES):
    folder = Path(folder).resolve()
    output = Path(output or BASE_DIR/'media').resolve()
    if not folder.is_dir():
        raise ValueError('Папка оригиналов не найдена')
    if folder == output or folder.is_relative_to(output) or output.is_relative_to(folder):
        raise ValueError('Папки оригиналов и готовых файлов должны быть раздельными')
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output/'.import_manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    tracked_targets = {entry['target'] for entry in manifest.values()}
    known = {digest(p): p for p in output.rglob('*') if p.is_file()
             and not any(part.startswith('.') for part in p.relative_to(output).parts)
             and str(p.relative_to(output)) not in tracked_targets and not needs_download(p)
             and p.suffix.lower() in {'.jpg','.jpeg','.png','.mp4','.gif'}}
    files = source_files(folder)
    # Сначала фотографии и небольшие локальные файлы: бот получает коллекцию
    # быстрее, а загрузка больших оригиналов откладывается до проверки места.
    files.sort(key=lambda p:(p.suffix.lower() in VIDEO_INPUTS, needs_download(p), p.stat().st_size, str(p)))
    by_path = {entry.get('source_path'):entry for entry in manifest.values() if entry.get('source_path')}
    skip_path = output/'.import_skipped.json'
    skipped = json.loads(skip_path.read_text()) if skip_path.exists() else {}
    summary = Counter()
    remaining = []
    for source in files:
        relative = str(source.relative_to(folder))
        previous = by_path.get(relative)
        meta = source.stat()
        ignored = skipped.get(relative)
        if isinstance(ignored, dict) and same_source(ignored, meta):
            summary['skipped'] += 1
        elif same_source(previous,meta):
            summary['already_imported' if (output/previous['target']).is_file() else 'previously_removed'] += 1
        else:
            remaining.append(source)
    files = remaining
    errors = []
    pending = []
    report = output/'.import_report.json'
    def checkpoint():
        temporary_report = report.with_suffix('.json.tmp')
        temporary_report.write_text(json.dumps({'summary':dict(summary), 'errors':errors, 'pending_space':pending},ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temporary_report,report)
    for index, source in prefetched_files(files,output,by_path,folder,min_free_bytes):
        try:
            meta = source.stat()
            relative = str(source.relative_to(folder))
            previous = by_path.get(relative)
            if same_source(previous,meta):
                summary['already_imported' if (output/previous['target']).is_file() else 'previously_removed'] += 1
                continue
            video = source.suffix.lower() in VIDEO_INPUTS
            room = (2*(MAX_VIDEO+2_000_000) if video else 2*MAX_PHOTO)+PREFETCH_COUNT*PREFETCH_MAX_BYTES
            if needs_download(source):
                room += meta.st_size
            if free_space(output) < min_free_bytes+room:
                pending = [str(p.relative_to(folder)) for p in files[index-1:]]
                summary['pending_space'] = len(pending)
                print(f'Мало свободного места: отложено {len(pending)} файлов. Оригиналы сохранены.', flush=True)
                break
            original_hash = digest(source)
            if original_hash in known:
                summary['already_present'] += 1
                continue
            if original_hash in manifest:
                # Удалённый вручную готовый файл не восстанавливается повторным импортом.
                entry = manifest[original_hash]
                summary['already_imported' if (output/entry['target']).is_file() else 'previously_removed'] += 1
                current_meta = source.stat()
                entry.update(source_path=relative,source_size=current_meta.st_size,source_mtime_ns=current_meta.st_mtime_ns)
                by_path[relative] = entry
                temporary_manifest = manifest_path.with_suffix('.json.tmp')
                temporary_manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
                os.replace(temporary_manifest,manifest_path)
                continue
            extension = '.mp4' if video else '.gif' if source.suffix.lower() == '.gif' else '.jpg'
            target = output/('cat_'+original_hash[:24]+extension)
            # Частично подготовленные файлы не видны работающему боту.
            with tempfile.TemporaryDirectory(prefix='.preparing-', dir=output) as tmp:
                temporary = Path(tmp)/('prepared'+extension)
                if video:
                    convert_video(source, temporary, edge=video_edge, crf=video_crf)
                elif extension == '.gif':
                    if source.stat().st_size > MAX_VIDEO:
                        raise ValueError('GIF превышает лимит бота')
                    shutil.copy2(source, temporary)
                else:
                    convert_photo(source, temporary)
                os.replace(temporary, target)
            current_meta = source.stat()
            manifest[original_hash] = {'target':target.name, 'source_name':source.name,
                                      'source_path':relative, 'source_size':current_meta.st_size,
                                      'source_mtime_ns':current_meta.st_mtime_ns}
            by_path[relative] = manifest[original_hash]
            journal_tmp = manifest_path.with_suffix('.json.tmp')
            journal_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(journal_tmp, manifest_path)
            known[original_hash] = target
            summary['videos' if video else 'animations' if extension=='.gif' else 'photos'] += 1
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            summary['errors'] += 1
            errors.append({'file':str(source.relative_to(folder)), 'error':str(error)})
            checkpoint()
        finally:
            if index%20 == 0 or index == len(files):
                print(f'Обработано {index}/{len(files)}; готовых новых: {summary["photos"]+summary["videos"]+summary["animations"]}; ошибок: {summary["errors"]}', flush=True)
                checkpoint()
    checkpoint()
    error_path = folder/'import_errors.json'
    if errors:
        error_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
    elif error_path.exists() and not pending:
        error_path.unlink()
    return dict(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--inspect', action='store_true', help='Только посчитать файлы и объём')
    parser.add_argument('--video-edge', type=int, choices=(720,1080), default=1080, help='Максимальная сторона видео')
    parser.add_argument('--video-crf', type=int, choices=range(18,29), default=23, help='Уровень сжатия видео (больше — компактнее)')
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_dir():
        parser.error('Папка не найдена')
    files = source_files(folder)
    size = sum(p.stat().st_size for p in files)
    print('Файлов для подготовки:',len(files))
    print('По форматам:',dict(Counter(p.suffix.lower() for p in files)))
    print(f'Объём оригиналов: {size/1024**3:.2f} ГиБ')
    print(f'Свободно на диске бота: {shutil.disk_usage(BASE_DIR).free/1024**3:.2f} ГиБ')
    if args.inspect:
        return
    print('Результат:',import_folder(folder,video_edge=args.video_edge,video_crf=args.video_crf))


if __name__ == '__main__':
    main()

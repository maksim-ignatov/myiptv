#!/usr/bin/env python3

import json
import os
import random
import signal
import subprocess
import threading
import time
from datetime import datetime

BASE_PATH = '/app'
RTSP_URL = os.getenv('RTSP_URL', 'rtsp://mediamtx:8554/stream')
AUDIO_TRACK = int(os.getenv('AUDIO_TRACK', '0'))
VIDEOS_BASE = os.getenv('VIDEOS_BASE', os.path.join(BASE_PATH, 'videos'))
CONFIG_FILE = os.path.join(BASE_PATH, 'config.myiptv')
PLAYED_FILE = os.path.join(BASE_PATH, 'data', 'played.json')
FIFO_PATH = '/tmp/iptv_pipe.ts'

FAST_FAIL_THRESHOLD = 5.0

SLOTS = ('early_morning', 'morning', 'late_morning', 'afternoon',
         'evening', 'late_evening', 'night', 'late_night')

LOGO_POSITIONS = {
    'top-left':     '10:10',
    'top-right':    'W-w-10:10',
    'bottom-left':  '10:H-h-10',
    'bottom-right': 'W-w-10:H-h-10',
}

shutdown_event = threading.Event()
played_videos = {}
outer_process = None


# ---------------------------------------------------------------------------
# Цвета
# ---------------------------------------------------------------------------

_CYAN    = '\033[96m'   # системные события: старт, FIFO, watchdog, переходы
_GREEN   = '\033[92m'   # успех: ffmpeg запущен, эпизод завершён, сброс
_YELLOW  = '\033[93m'   # текущий эпизод: что сейчас играет
_MAGENTA = '\033[95m'   # аудиодорожки
_BLUE    = '\033[94m'   # история и расписание
_RED     = '\033[91m'   # ошибки и предупреждения
_GRAY    = '\033[90m'   # сырой вывод subprocess
_RESET   = '\033[0m'

def _c(color, text):
    return f"{color}{text}{_RESET}"

def _ts():
    return _c(_GRAY, f"[{datetime.now()}]")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _logo_exists(path):
    if not path:
        return False
    try:
        open(path, 'rb').close()
        return True
    except OSError:
        return False


def get_current_category():
    hour = datetime.now().hour
    if 5 <= hour < 7:   return 'early_morning'
    elif 7 <= hour < 10: return 'morning'
    elif 10 <= hour < 12: return 'late_morning'
    elif 12 <= hour < 15: return 'afternoon'
    elif 15 <= hour < 18: return 'evening'
    elif 18 <= hour < 21: return 'late_evening'
    elif 21 <= hour < 24: return 'night'
    else:                 return 'late_night'


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def load_config():
    result = {slot: [] for slot in SLOTS}
    result['*'] = []
    result['settings'] = {}
    if not os.path.isfile(CONFIG_FILE):
        return result
    current_section = '*'
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    section = line[1:-1].strip().lower()
                    current_section = section if section in result else '*'
                    continue
                if '=' in line:
                    key, _, value = line.partition('=')
                    key, value = key.strip(), value.strip()
                    if current_section == 'settings':
                        result['settings'][key] = value
                    else:
                        result[current_section].append({'folder': key, 'audio': value})
                else:
                    if current_section != 'settings':
                        result[current_section].append({'folder': line.strip(), 'audio': ''})
    except OSError:
        pass
    return result


def get_shows_for_slot(slot, config):
    entries = config.get(slot, []) + config.get('*', [])
    show_videos = {}
    audio_map = {}
    for entry in entries:
        show_name = entry['folder'].replace('\\', '/').split('/')[0]
        folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
        vids = find_videos(folder_path)
        if not vids:
            print(f"{_ts()} {_c(_RED, f'⚠ Нет видео в папке: {folder_path}')}")
        show_videos.setdefault(show_name, []).extend(vids)
        if entry['audio']:
            audio_map[entry['folder']] = entry['audio']
    return show_videos, audio_map


def find_videos(folder):
    videos = []
    extensions = ('.mp4', '.mkv', '.avi', '.mpg', '.mov', '.ts')
    for root, _, files in os.walk(folder, followlinks=True):
        for file in files:
            if file.lower().endswith(extensions):
                videos.append(os.path.join(root, file))
    return videos


# ---------------------------------------------------------------------------
# История просмотров
# ---------------------------------------------------------------------------

def load_played():
    global played_videos
    try:
        with open(PLAYED_FILE, encoding='utf-8') as f:
            data = json.load(f)
        played_videos = {tuple(k.split('|', 1)): set(v) for k, v in data.items()}
        total = sum(len(v) for v in played_videos.values())
        print(f"{_ts()} {_c(_BLUE, f'📂 История загружена: {total} просмотренных серий')}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{_ts()} {_c(_RED, f'⚠ Не удалось загрузить историю: {e}')}")


def save_played():
    try:
        os.makedirs(os.path.dirname(PLAYED_FILE), exist_ok=True)
        data = {'|'.join(k): list(v) for k, v in played_videos.items()}
        with open(PLAYED_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"{_ts()} {_c(_RED, f'⚠ Не удалось сохранить историю: {e}')}")


# ---------------------------------------------------------------------------
# Аудиодорожки
# ---------------------------------------------------------------------------

def get_audio_streams(video_path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'stream=index,codec_type:stream_tags=language,title',
             '-select_streams', 'a',
             '-of', 'compact', video_path],
            capture_output=True, text=True, timeout=10
        )
        streams = []
        for line in result.stdout.splitlines():
            if 'codec_type=audio' not in line:
                continue
            idx = len(streams)
            lang = ''
            title = ''
            for part in line.split('|'):
                if part.startswith('tag:language='):
                    lang = part.split('=', 1)[1]
                elif part.startswith('tag:title='):
                    title = part.split('=', 1)[1]
            streams.append({'index': idx, 'lang': lang, 'title': title})
        return streams
    except Exception:
        return []


def resolve_audio_track(streams, preference):
    if not streams:
        return AUDIO_TRACK
    pref = preference.strip()
    if pref.isdigit():
        idx = int(pref)
        return idx if idx < len(streams) else AUDIO_TRACK
    if ':' in pref:
        lang, _, n = pref.partition(':')
        lang = lang.strip().lower()
        try:
            n = int(n.strip())
        except ValueError:
            n = 1
        matches = [s for s in streams if s['lang'].lower() == lang]
        if matches and n <= len(matches):
            return matches[n - 1]['index']
        print(f"{_ts()} {_c(_RED, f'⚠ Дорожка {repr(preference)} не найдена, используем дефолт')}")
        return AUDIO_TRACK
    lang_lower = pref.lower()
    for s in streams:
        if s['lang'].lower() == lang_lower:
            return s['index']
    for s in streams:
        if pref.lower() in s['title'].lower():
            return s['index']
    return AUDIO_TRACK


def get_audio_track(video_path, audio_map):
    if not audio_map:
        return AUDIO_TRACK
    path_parts_lower = [p.lower() for p in video_path.replace('\\', '/').split('/')]
    best_match = None
    best_len = 0
    for folder_key, preference in audio_map.items():
        key_parts_lower = [p.strip().lower() for p in folder_key.replace('\\', '/').split('/')]
        key_len = len(key_parts_lower)
        for i in range(len(path_parts_lower) - key_len + 1):
            if path_parts_lower[i:i + key_len] == key_parts_lower:
                if key_len > best_len:
                    best_len = key_len
                    best_match = (folder_key, preference)
                break
    if best_match:
        folder_key, preference = best_match
        streams = get_audio_streams(video_path)
        if streams:
            tracks_str = '  '.join(
                f"[{s['index']}] {s['lang'] or '?'}{(' ' + repr(s['title'])) if s['title'] else ''}"
                for s in streams
            )
            print(f"{_ts()} {_c(_MAGENTA, f'🎵 Дорожки: {tracks_str}')}")
        track = resolve_audio_track(streams, preference)
        print(f"{_ts()} {_c(_MAGENTA, f'🎵 Выбрана: {repr(preference)} → дорожка {track}')}")
        return track
    return AUDIO_TRACK


# ---------------------------------------------------------------------------
# Сводка расписания
# ---------------------------------------------------------------------------

def print_schedule_summary(config):
    print(_c(_BLUE, "📋 Расписание:"))
    has_any = False
    for slot in SLOTS:
        entries = config.get(slot, []) + config.get('*', [])
        if not entries:
            continue
        parts = []
        for entry in entries:
            show_name = entry['folder'].replace('\\', '/').split('/')[0]
            folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
            count = len(find_videos(folder_path))
            parts.append(f"{show_name} ({count} серий)")
        if parts:
            print(_c(_BLUE, f"   {slot:15s} → {', '.join(parts)}"))
            has_any = True
    wildcard = config.get('*', [])
    if wildcard:
        parts = []
        for entry in wildcard:
            show_name = entry['folder'].replace('\\', '/').split('/')[0]
            folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
            count = len(find_videos(folder_path))
            parts.append(f"{show_name} ({count} серий)")
        print(_c(_BLUE, f"   {'[всегда]':15s} → {', '.join(parts)}"))
        has_any = True
    if not has_any:
        print(_c(_YELLOW, "   (нет настроенных шоу — заполните config.myiptv)"))
    settings = config.get('settings', {})
    raw = settings.get('logo', '').strip()
    logo_path = raw if os.path.isabs(raw) else (os.path.join(BASE_PATH, raw) if raw else '')
    position = settings.get('logo_position', 'bottom-left')
    if _logo_exists(logo_path):
        print(_c(_BLUE, f"   🖼 Логотип: {logo_path} ({position})"))
    elif logo_path:
        print(_c(_RED, f"   🖼 Логотип: ⚠ файл не найден ({logo_path})"))
    else:
        print(_c(_BLUE, "   🖼 Логотип: отключён"))
    for slot in SLOTS:
        for entry in config.get(slot, []):
            folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
            if not os.path.isdir(folder_path):
                print(_c(_RED, f"   ⚠ [{slot}] папка не найдена: {folder_path}"))
    print()


# ---------------------------------------------------------------------------
# Внешний ffmpeg (живёт всегда, читает FIFO → RTSP)
# ---------------------------------------------------------------------------

def build_outer_cmd(_settings):
    """
    Внешний ffmpeg: читает FIFO-pipe (уже закодированный TS с логотипом),
    пушит в RTSP без перекодирования — нулевая нагрузка на CPU.
    """
    return [
        'ffmpeg', '-y',
        '-fflags', '+genpts+discardcorrupt+nobuffer',
        '-i', FIFO_PATH,
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        '-af', 'aresample=async=1000',
        '-fflags', '+genpts+flush_packets+nobuffer',
        '-avoid_negative_ts', 'make_zero',
        '-flush_packets', '1',
        '-max_muxing_queue_size', '1024',
        '-ignore_unknown',
        '-loglevel', 'warning',
        '-f', 'rtsp', '-rtsp_transport', 'tcp',
        RTSP_URL,
    ]


def start_outer_ffmpeg(settings):
    global outer_process
    cmd = build_outer_cmd(settings)
    print(f"{_ts()} {_c(_GREEN, f'🟢 Внешний ffmpeg запущен (copy) → {RTSP_URL}')}")
    outer_process = subprocess.Popen(cmd, stderr=subprocess.PIPE)

    def log_stderr():
        for line in outer_process.stderr:
            line = line.decode(errors='replace').rstrip()
            if line:
                print(_c(_GRAY, f"[outer] {line}"))
        # Поток stderr закрылся — outer ffmpeg завершился
        rc = outer_process.poll()
        if rc is not None and not shutdown_event.is_set():
            print(f"{_ts()} {_c(_RED, f'🔴 Внешний ffmpeg завершился (код {rc})')}")

    threading.Thread(target=log_stderr, daemon=True).start()
    return outer_process


# ---------------------------------------------------------------------------
# Внутренний ffmpeg: видеофайл → FIFO (MPEGTS, без звука re-encode)
# ---------------------------------------------------------------------------

INNER_TS_ARGS = [
    # Видео
    '-c:v', 'libx264',
    '-preset', 'ultrafast',
    '-x264-params', 'repeat-headers=1',
    '-crf', '23',
    '-maxrate', '1500k',
    '-bufsize', '4000k',
    # Аудио — global_header чтобы outer ffmpeg мог copy в RTSP без перекодирования
    '-c:a', 'aac',
    '-b:a', '128k',
    '-ar', '44100',
    '-ac', '2',
    # Поток
    '-pix_fmt', 'yuv420p',
    '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
    '-fflags', '+genpts+flush_packets+nobuffer',
    '-avoid_negative_ts', 'make_zero',
    '-reset_timestamps', '1',
    '-flush_packets', '1',
    '-max_muxing_queue_size', '1024',
    '-ignore_unknown',
    '-loglevel', 'warning',
    '-f', 'mpegts',
    FIFO_PATH,
]


def build_inner_cmd(video_path, audio_track, settings):
    """Кодирует видео → MPEGTS в FIFO. Логотип накладывается здесь."""
    raw = settings.get('logo', '').strip()
    logo_path = raw if os.path.isabs(raw) else (os.path.join(BASE_PATH, raw) if raw else '')
    position_key = settings.get('logo_position', 'bottom-left')
    overlay_pos = LOGO_POSITIONS.get(position_key, LOGO_POSITIONS['bottom-left'])

    if _logo_exists(logo_path):
        return [
            'ffmpeg', '-y', '-re',
            '-err_detect', 'ignore_err',
            '-i', video_path,
            '-i', logo_path,
            '-filter_complex', f'[0:v][1:v]overlay={overlay_pos}',
            '-map', '0:a:' + str(audio_track),
        ] + INNER_TS_ARGS
    else:
        return [
            'ffmpeg', '-y', '-re',
            '-err_detect', 'ignore_err',
            '-i', video_path,
            '-map', '0:v:0',
            '-map', '0:a:' + str(audio_track),
        ] + INNER_TS_ARGS


# ---------------------------------------------------------------------------
# Чёрный экран: генерация через lavfi → FIFO
# ---------------------------------------------------------------------------

def _run_blackscreen(duration=30):
    proc = subprocess.Popen(build_blackscreen_cmd(duration), stderr=subprocess.PIPE)

    def log_stderr():
        for line in proc.stderr:
            line = line.decode(errors='replace').rstrip()
            if line:
                print(_c(_GRAY, f"[blackscreen] {line}"))

    threading.Thread(target=log_stderr, daemon=True).start()
    while proc.poll() is None and not shutdown_event.is_set():
        time.sleep(1)
    rc = proc.poll()
    if rc is None:
        proc.terminate()
    else:
        print(f"{_ts()} {_c(_CYAN, f'🌑 Чёрный экран завершился (код {rc})')}")


def build_blackscreen_cmd(duration=30):
    """Генерирует чёрный экран 1920x1080 25fps в FIFO на duration секунд."""
    return [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', f'color=black:size=1920x1080:rate=25:duration={duration}',
        '-f', 'lavfi', '-i', f'anullsrc=r=44100:cl=stereo',
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
        '-c:a', 'aac', '-b:a', '64k',
        '-pix_fmt', 'yuv420p',
        '-g', '25', '-keyint_min', '25',
        '-fflags', '+genpts+flush_packets+nobuffer',
        '-flush_packets', '1',
        '-loglevel', 'warning',
        '-f', 'mpegts',
        FIFO_PATH,
    ]


# ---------------------------------------------------------------------------
# Сигналы завершения
# ---------------------------------------------------------------------------

def handle_shutdown(_signum, _frame):
    print(f"\n{_ts()} {_c(_CYAN, '🛑 Получен сигнал завершения, останавливаем...')}")
    shutdown_event.set()
    if outer_process and outer_process.poll() is None:
        outer_process.terminate()


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


# ---------------------------------------------------------------------------
# Основной цикл воспроизведения
# ---------------------------------------------------------------------------

def run_inner(cmd, label):
    """
    Запускает внутренний ffmpeg, ждёт завершения.
    Возвращает (exit_code, duration).
    """
    start = time.time()
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    stderr_lines = []
    fatal_kw = ['no such file or directory', 'moov atom not found', 'invalid data found']

    def read_stderr():
        for line in proc.stderr:
            line = line.decode(errors='replace').rstrip()
            stderr_lines.append(line)
            if any(kw in line.lower() for kw in fatal_kw):
                print(f"{_ts()} {_c(_RED, f'❌ Фатальная ошибка: {line}')}")
                proc.kill()

    t = threading.Thread(target=read_stderr, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=2)
    duration = time.time() - start
    if proc.returncode not in (0, -9, -15) and stderr_lines:
        print(f"{_ts()} {_c(_RED, f'FFmpeg ({label}) вывод:')}")
        for line in stderr_lines[-10:]:
            print(_c(_GRAY, f"    {line}"))
    return proc.returncode, duration


def _outer_watchdog(settings_ref):
    """Фоновый поток — следит за внешним ffmpeg, при падении перезапускает немедленно."""
    while not shutdown_event.is_set():
        shutdown_event.wait(timeout=1)
        if shutdown_event.is_set():
            break
        if outer_process and outer_process.poll() is not None:
            print(f"{_ts()} {_c(_RED, f'⚠ Watchdog: внешний ffmpeg упал (код {outer_process.returncode}), перезапуск...')}")
            start_outer_ffmpeg(settings_ref[0])
            shutdown_event.wait(timeout=2)


def continuous_playback():
    current_slot = get_current_category()
    print(f"{_ts()} {_c(_CYAN, f'▶ Начинаем показ категории: {current_slot}')}")

    # Создаём FIFO
    if os.path.exists(FIFO_PATH):
        os.remove(FIFO_PATH)
    os.mkfifo(FIFO_PATH)
    print(f"{_ts()} {_c(_CYAN, f'📡 FIFO создан: {FIFO_PATH}')}")

    # Открываем FIFO сами в режиме O_RDWR — "якорный" дескриптор.
    # Пока он открыт, читатель (внешний ffmpeg) никогда не получит EOF.
    fifo_anchor_fd = os.open(FIFO_PATH, os.O_RDWR | os.O_NONBLOCK)
    print(f"{_ts()} {_c(_CYAN, f'🔗 FIFO anchor открыт (fd={fifo_anchor_fd})')}")

    config = load_config()
    settings = config.get('settings', {})
    settings_ref = [settings]

    start_outer_ffmpeg(settings)
    time.sleep(1)

    threading.Thread(target=_outer_watchdog, args=(settings_ref,), daemon=True).start()
    print(f"{_ts()} {_c(_CYAN, '👁 Watchdog запущен')}")

    while not shutdown_event.is_set():
        new_slot = get_current_category()
        if new_slot != current_slot:
            print(f"{_ts()} {_c(_CYAN, f'🔄 Переход на новую категорию: {new_slot}')}")
            current_slot = new_slot

        config = load_config()
        settings = config.get('settings', {})
        settings_ref[0] = settings
        show_videos, audio_map = get_shows_for_slot(current_slot, config)

        if not show_videos:
            print(f"{_ts()} {_c(_YELLOW, f'🌑 Нет видео для [{current_slot}] — чёрный экран 30с')}")
            _run_blackscreen(30)
            continue

        # Выбираем следующую серию
        available = {}
        for show, vids in show_videos.items():
            if not vids:
                continue
            key = (current_slot, show)
            seen = played_videos.get(key, set())
            unplayed = [v for v in vids if v not in seen]
            if not unplayed:
                print(f"{_ts()} {_c(_GREEN, f'✅ Все серии {repr(show)} в {current_slot} воспроизведены. Сброс.')}")
                played_videos[key] = set()
                unplayed = vids[:]
            available[show] = unplayed

        if not available:
            print(f"{_ts()} {_c(_YELLOW, f'🌑 Нет доступных видео для [{current_slot}] — чёрный экран 30с')}")
            _run_blackscreen(30)
            continue

        chosen_show = random.choice(list(available.keys()))
        video_path = random.choice(available[chosen_show])

        total = len(show_videos[chosen_show])
        seen_count = len(played_videos.get((current_slot, chosen_show), set()))
        pct = int(seen_count / total * 100) if total else 0
        print(f"{_ts()} {_c(_YELLOW, f'🟡 [{chosen_show}]')} {os.path.basename(video_path)}  {_c(_GRAY, f'({seen_count}/{total}, {pct}%)')}")

        track = get_audio_track(video_path, audio_map)
        cmd = build_inner_cmd(video_path, track, settings)
        raw = settings.get('logo', '').strip()
        logo_path = raw if os.path.isabs(raw) else (os.path.join(BASE_PATH, raw) if raw else '')
        logo_note = ' +лого' if _logo_exists(logo_path) else ''
        print(f"{_ts()} {_c(_YELLOW, f'▶ Запуск: {os.path.basename(video_path)}')} {_c(_GRAY, f'(аудио: {track}{logo_note})')}")

        exit_code, duration = run_inner(cmd, os.path.basename(video_path))
        mins, secs = divmod(int(duration), 60)

        if exit_code in (0, -9, -15, 255):
            print(f"{_ts()} {_c(_GREEN, f'⏹ Завершено за {mins}м {secs:02d}с: {os.path.basename(video_path)}')}")
            key = (current_slot, chosen_show)
            played_videos.setdefault(key, set()).add(video_path)
            save_played()
            # Запускаем чёрный экран на 3с — он пишет в FIFO пока outer жив
            # Затем перезапускаем outer (сброс накопленных timestamps)
            # VLC не замечает разрыва т.к. данные в FIFO не прерываются
            bs_proc = subprocess.Popen(build_blackscreen_cmd(3), stderr=subprocess.DEVNULL)
            shutdown_event.wait(timeout=1)
            if outer_process and outer_process.poll() is None:
                print(f"{_ts()} {_c(_CYAN, '🔄 Перезапуск outer ffmpeg (сброс timestamps)...')}")
                outer_process.terminate()
                try:
                    outer_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    outer_process.kill()
                    outer_process.wait()
            start_outer_ffmpeg(settings)
            # Ждём пока чёрный экран доиграет
            while bs_proc.poll() is None and not shutdown_event.is_set():
                shutdown_event.wait(timeout=0.5)
            if bs_proc.poll() is None:
                bs_proc.terminate()
        elif duration < FAST_FAIL_THRESHOLD:
            print(f"{_ts()} {_c(_RED, f'⚠ Быстрый сбой за {duration:.1f}с (exit: {exit_code})')}")
            shutdown_event.wait(timeout=5)
        else:
            print(f"{_ts()} {_c(_RED, f'❌ Ошибка в файле (exit: {exit_code}, {duration:.1f}с): {os.path.basename(video_path)}')}")
            key = (current_slot, chosen_show)
            played_videos.setdefault(key, set()).add(video_path)
            save_played()


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # Логирование в файл с ротацией (10MB x 3 файла) + вывод в stdout
    import logging
    import logging.handlers
    import re

    log_dir = os.path.join(BASE_PATH, 'data')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'stream.log')

    _ansi_re = re.compile(r'\033\[[0-9;]*m')

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(message)s'))
    root_logger.addHandler(file_handler)

    # Перенаправляем print в logging чтобы всё попадало и в файл и в stdout
    # В файл пишем без ANSI-кодов
    import builtins
    _orig_print = builtins.print
    def _logging_print(*args, **kwargs):
        msg = ' '.join(str(a) for a in args)
        _orig_print(*args, **kwargs)
        logging.info(_ansi_re.sub('', msg))
    builtins.print = _logging_print

    print(f"{_c(_GREEN, '🚀 IPTV-поток запущен.')}\n")
    print(f"{_c(_BLUE, f'📝 Лог: {log_file}')}")
    load_played()
    print_schedule_summary(load_config())
    continuous_playback()

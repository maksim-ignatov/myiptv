#!/usr/bin/env python3

import os
import random
import signal
import threading
import time
from datetime import datetime
import subprocess

VIDEO_DIRS = {
    'early_morning': 'videos/early_morning',  # 5-7
    'morning': 'videos/morning',              # 7-10
    'late_morning': 'videos/late_morning',    # 10-12
    'afternoon': 'videos/afternoon',          # 12-15
    'evening': 'videos/evening',              # 15-18
    'late_evening': 'videos/late_evening',    # 18-21
    'night': 'videos/night',                  # 21-24
    'late_night': 'videos/late_night'         # 0-5
}

BASE_PATH = '/app'
RTSP_URL = os.getenv('RTSP_URL', 'rtsp://mediamtx:8554/stream')
AUDIO_TRACK = int(os.getenv('AUDIO_TRACK', '0'))
AUDIO_CONFIG_FILE = os.path.join(BASE_PATH, 'audio_config.myiptv')

# Если ffmpeg завершился быстрее этого порога — скорее всего RTSP недоступен,
# а не проблема в файле. Видео НЕ помечаем как воспроизведённое.
FAST_FAIL_THRESHOLD = 5.0

FFMPEG_CMD_TEMPLATE = [
    'ffmpeg', '-re',
    '-err_detect', 'ignore_err',
    '-i', '',
    '-map', '0:v:0',
    '-map', '0:a:AUDIO_TRACK_PLACEHOLDER',

    # Видео
    '-c:v', 'libx264',
    '-preset', 'veryfast',
    '-x264-params', 'repeat-headers=1',
    '-crf', '23',
    '-maxrate', '2000k',
    '-bufsize', '500k',

    # Аудио
    '-c:a', 'aac',
    '-b:a', '128k',
    '-ar', '44100',
    '-ac', '2',

    # Поток
    '-pix_fmt', 'yuv420p',
    '-g', '25',
    '-keyint_min', '25',
    '-sc_threshold', '0',

    # Поведение
    '-fflags', '+genpts+flush_packets+nobuffer',
    '-avoid_negative_ts', 'make_zero',
    '-flush_packets', '1',
    '-max_muxing_queue_size', '1024',
    '-ignore_unknown',

    # RTSP-вывод
    '-loglevel', 'warning',
    '-f', 'rtsp',
    '-rtsp_transport', 'tcp',
    RTSP_URL
]

INPUT_INDEX = FFMPEG_CMD_TEMPLATE.index('-i') + 1

current_process = None
shutdown_event = threading.Event()
played_videos = {k: set() for k in VIDEO_DIRS}


def handle_shutdown(signum, frame):
    print(f"\n[{datetime.now()}] 🛑 Получен сигнал завершения, останавливаем...")
    shutdown_event.set()
    if current_process and current_process.poll() is None:
        current_process.terminate()


signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def get_current_category():
    hour = datetime.now().hour
    if 5 <= hour < 7:
        return 'early_morning'
    elif 7 <= hour < 10:
        return 'morning'
    elif 10 <= hour < 12:
        return 'late_morning'
    elif 12 <= hour < 15:
        return 'afternoon'
    elif 15 <= hour < 18:
        return 'evening'
    elif 18 <= hour < 21:
        return 'late_evening'
    elif 21 <= hour < 24:
        return 'night'
    else:  # 0-5
        return 'late_night'


def load_audio_config():
    """Читает audio_config.myiptv, возвращает dict {папка: предпочтение}."""
    config = {}
    if not os.path.isfile(AUDIO_CONFIG_FILE):
        return config
    try:
        with open(AUDIO_CONFIG_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    folder, _, pref = line.partition('=')
                    config[folder.strip()] = pref.strip()
    except OSError:
        pass
    return config


def get_audio_streams(video_path):
    """Возвращает список дорожек: [{'index': 0, 'lang': 'rus', 'title': '...'}]"""
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
    """
    Находит индекс дорожки по предпочтению:
      'Рен-ТВ'   → ищет по title (частичное совпадение, без регистра)
      'rus'      → первая дорожка с языком rus
      'rus:2'    → вторая дорожка с языком rus
      '1'        → дорожка с индексом 1
    """
    if not streams:
        return AUDIO_TRACK

    pref = preference.strip()

    # Число — прямой индекс
    if pref.isdigit():
        idx = int(pref)
        return idx if idx < len(streams) else AUDIO_TRACK

    # lang:N — N-й по счёту (1-based) с нужным языком
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
        return AUDIO_TRACK

    # Просто язык — первая подходящая дорожка
    lang_lower = pref.lower()
    for s in streams:
        if s['lang'].lower() == lang_lower:
            return s['index']

    # Название перевода — поиск по title
    for s in streams:
        if pref.lower() in s['title'].lower():
            return s['index']

    return AUDIO_TRACK


def get_audio_track(video_path):
    """Определяет индекс аудиодорожки по конфигу audio_config.myiptv.
    Поддерживает подпапки: 'South Park/Season 3' более специфично, чем 'South Park'.
    Побеждает самое длинное совпадение.
    """
    config = load_audio_config()
    if not config:
        return AUDIO_TRACK

    path_parts_lower = [p.lower() for p in video_path.replace('\\', '/').split('/')]

    best_match = None
    best_len = 0

    for folder_key, preference in config.items():
        key_parts_lower = [p.strip().lower() for p in folder_key.replace('\\', '/').split('/')]
        key_len = len(key_parts_lower)
        # Ищем key_parts как непрерывную подпоследовательность path_parts
        for i in range(len(path_parts_lower) - key_len + 1):
            if path_parts_lower[i:i + key_len] == key_parts_lower:
                if key_len > best_len:
                    best_len = key_len
                    best_match = (folder_key, preference)
                break

    if best_match:
        folder_key, preference = best_match
        streams = get_audio_streams(video_path)
        track = resolve_audio_track(streams, preference)
        print(f"[{datetime.now()}] 🎵 Конфиг: '{folder_key}' → '{preference}' → дорожка {track}")
        return track

    return AUDIO_TRACK


def find_videos(folder):
    videos = []
    extensions = ('.mp4', '.mkv', '.avi', '.mpg', '.mov', '.ts')
    for root, _, files in os.walk(folder, followlinks=True):
        for file in files:
            if file.lower().endswith(extensions):
                videos.append(os.path.join(root, file))
    return videos


def play_video(video_path):
    """
    Returns:
      True  — видео успешно воспроизведено
      False — ошибка в файле, пропустить
      None  — быстрый сбой (RTSP недоступен), не помечать файл как воспроизведённый
    """
    global current_process

    cmd = FFMPEG_CMD_TEMPLATE[:]
    cmd[INPUT_INDEX] = video_path
    track = get_audio_track(video_path)
    cmd[cmd.index('0:a:AUDIO_TRACK_PLACEHOLDER')] = f'0:a:{track}'

    print(f"[{datetime.now()}] ▶ Запуск видео: {video_path} (аудио: {track})")
    start_time = time.time()

    try:
        current_process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        stderr_lines = []
        fatal_event = threading.Event()
        fatal_keywords = ['no such file or directory', 'moov atom not found', 'invalid data found']

        def monitor_stderr():
            for line in current_process.stderr:
                line = line.rstrip()
                stderr_lines.append(line)
                if any(kw in line.lower() for kw in fatal_keywords):
                    print(f"[{datetime.now()}] ❌ Фатальная ошибка: {line}")
                    fatal_event.set()
                    current_process.kill()
                    break

        monitor_thread = threading.Thread(target=monitor_stderr, daemon=True)
        monitor_thread.start()

        current_process.wait()
        monitor_thread.join(timeout=2)

        exit_code = current_process.returncode
        duration = time.time() - start_time

        # Нормальное завершение или killed сигналом (SIGKILL/SIGTERM)
        if exit_code in (0, -9, -15):
            print(f"[{datetime.now()}] ⏹ Видео завершилось: {video_path}")
            return True

        # Быстрый сбой — RTSP скорее всего недоступен, файл не виноват
        if duration < FAST_FAIL_THRESHOLD:
            print(f"[{datetime.now()}] ⚠ Быстрый сбой за {duration:.1f}с (exit: {exit_code}) — возможно RTSP недоступен")
            if stderr_lines:
                for line in stderr_lines[-5:]:
                    print(f"    {line}")
            return None  # не помечать файл как воспроизведённый

        # Медленный сбой — проблема в файле
        print(f"[{datetime.now()}] ❌ Ошибка в файле (exit: {exit_code}, {duration:.1f}с): {os.path.basename(video_path)}")
        if stderr_lines:
            print(f"[{datetime.now()}] FFmpeg вывод:")
            for line in stderr_lines[-15:]:
                print(f"    {line}")
        return False

    except Exception as e:
        print(f"[{datetime.now()}] ⚠ Ошибка при запуске ffmpeg: {e}")
        return None


def continuous_playback():
    current_category = get_current_category()
    print(f"[{datetime.now()}] ▶ Начинаем показ категории: {current_category}")

    while not shutdown_event.is_set():
        new_category = get_current_category()
        if new_category != current_category:
            print(f"[{datetime.now()}] 🔄 Переход на новую категорию: {new_category}")
            current_category = new_category

        folder = os.path.join(BASE_PATH, VIDEO_DIRS[current_category])
        all_videos = find_videos(folder)

        if not all_videos:
            print(f"[{datetime.now()}] ⚠ Нет видео в папке: {folder}")
            shutdown_event.wait(timeout=10)
            continue

        unplayed = [v for v in all_videos if v not in played_videos[current_category]]
        if not unplayed:
            print(f"[{datetime.now()}] ✅ Все видео в {current_category} воспроизведены. Сброс.")
            played_videos[current_category].clear()
            unplayed = all_videos[:]

        video_path = random.choice(unplayed)
        print(f"[{datetime.now()}] 🟡 Воспроизводим: {video_path}")
        result = play_video(video_path)

        if result is True:
            played_videos[current_category].add(video_path)
        elif result is False:
            # Файл битый — пометить чтобы не повторять
            played_videos[current_category].add(video_path)
            print(f"[{datetime.now()}] ⚠ Пропускаем битый файл: {os.path.basename(video_path)}\n")
        elif result is None:
            # RTSP недоступен — НЕ помечаем файл, ждём перед повтором
            print(f"[{datetime.now()}] ⏳ Ждём 5с перед повтором (RTSP?)...")
            shutdown_event.wait(timeout=5)

        shutdown_event.wait(timeout=1)


if __name__ == '__main__':
    print("🚀 IPTV-поток запущен. Ожидаем видео...\n")
    continuous_playback()

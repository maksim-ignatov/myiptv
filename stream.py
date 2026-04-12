#!/usr/bin/env python3

import os
import random
import signal
import threading
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

FFMPEG_CMD_TEMPLATE = [
    'ffmpeg', '-re', '-i', '',

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

    # RTSP-вывод
    '-loglevel', 'warning',
    '-f', 'rtsp',
    '-rtsp_transport', 'tcp',
    RTSP_URL
]

current_process = None
shutdown_event = threading.Event()
played_videos = {k: set() for k in VIDEO_DIRS}


def handle_shutdown(signum, frame):
    print(f"\n[{datetime.now()}] \U0001f6d1 Получен сигнал завершения, останавливаем...")
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


def find_videos(folder):
    videos = []
    extensions = ('.mp4', '.mkv', '.avi', '.mpg', '.mov', '.ts')
    for root, _, files in os.walk(folder, followlinks=True):
        for file in files:
            if file.lower().endswith(extensions):
                videos.append(os.path.join(root, file))
    return videos


def play_video(video_path):
    global current_process

    cmd = FFMPEG_CMD_TEMPLATE[:]
    cmd[3] = video_path

    print(f"[{datetime.now()}] \u25b6 Запуск видео: {video_path}")

    try:
        current_process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        error_event = threading.Event()
        # Only patterns that reliably indicate a broken/unreadable file
        error_keywords = ['corrupt', 'moov atom not found', 'invalid data found', 'no such file']

        def monitor_errors():
            for line in current_process.stderr:
                if any(kw in line.lower() for kw in error_keywords):
                    print(f"[{datetime.now()}] \u274c Обнаружена ошибка ffmpeg: {line.strip()}")
                    error_event.set()
                    current_process.kill()
                    break

        monitor_thread = threading.Thread(target=monitor_errors, daemon=True)
        monitor_thread.start()

        current_process.wait()
        monitor_thread.join(timeout=2)

        if error_event.is_set():
            print(f"[{datetime.now()}] \U0001f501 Видео прервано из-за ошибки. Переходим к следующему.")
            return False

        print(f"[{datetime.now()}] \u23f9 Видео завершилось: {video_path}")
        return True

    except Exception as e:
        print(f"[{datetime.now()}] \u26a0 Ошибка при запуске ffmpeg: {e}")
        return False


def continuous_playback():
    current_category = get_current_category()
    print(f"[{datetime.now()}] \u25b6 Начинаем показ категории: {current_category}")

    while not shutdown_event.is_set():
        new_category = get_current_category()
        if new_category != current_category:
            print(f"[{datetime.now()}] \U0001f504 Переход на новую категорию: {new_category}")
            current_category = new_category

        folder = os.path.join(BASE_PATH, VIDEO_DIRS[current_category])
        all_videos = find_videos(folder)

        if not all_videos:
            print(f"[{datetime.now()}] \u26a0 Нет видео в папке: {folder}")
            shutdown_event.wait(timeout=10)
            continue

        unplayed = [v for v in all_videos if v not in played_videos[current_category]]
        if not unplayed:
            print(f"[{datetime.now()}] \u2705 Все видео в {current_category} были воспроизведены. Сброс.")
            played_videos[current_category].clear()
            unplayed = all_videos[:]

        video_path = random.choice(unplayed)

        print(f"[{datetime.now()}] \U0001f7e1 Пытаемся воспроизвести: {video_path}")
        success = play_video(video_path)

        # Mark as played regardless — on error, skip to next video next iteration
        played_videos[current_category].add(video_path)

        if not success:
            print(f"[{datetime.now()}] \u26a0 Ошибка с {video_path} — пробуем следующее...\n")

        shutdown_event.wait(timeout=1)


if __name__ == '__main__':
    print("\U0001f680 IPTV-поток запущен. Ожидаем видео...\n")
    continuous_playback()

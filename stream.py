#!/usr/bin/env python3

import os
import random
import time
import subprocess
import threading
from datetime import datetime

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

FFMPEG_CMD_TEMPLATE = [
    'ffmpeg', '-re', '-i', '',

    # Видео
    '-c:v', 'libx264',
    '-preset', 'veryfast',
    '-tune', 'zerolatency',
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
    'rtsp://mediamtx:8554/stream'
]


current_process = None
played_videos = {
    'early_morning': set(),
    'morning': set(),
    'late_morning': set(),
    'afternoon': set(),
    'evening': set(),
    'late_evening': set(),
    'night': set(),
    'late_night': set()
}


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
    for root, _, files in os.walk(folder, followlinks=True):
        for file in files:
            if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mpg', '.mov', '.AVI', '.ts')):
                full_path = os.path.join(root, file)
                videos.append(full_path)
    return videos


def play_video(video_path):
    global current_process

    cmd = FFMPEG_CMD_TEMPLATE[:]
    cmd[3] = video_path

    print(f"[{datetime.now()}] ▶ Запуск видео: {video_path}")

    try:
        # Запускаем ffmpeg и читаем stderr
        current_process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        error_detected = False
        keywords = ['error', 'invalid', 'corrupt', 'broken', 'non-monotonous', 'mismatch', 'decode', 'incomplete', 'unavailable']

        def monitor_errors():
            nonlocal error_detected
            for line in current_process.stderr:
                line_lower = line.lower()
                if any(keyword in line_lower for keyword in keywords):
                    print(f"[{datetime.now()}] ❌ Обнаружена ошибка ffmpeg: {line.strip()}")
                    error_detected = True
                    current_process.kill()
                    break

        # Запускаем мониторинг ошибок в отдельном потоке
        monitor_thread = threading.Thread(target=monitor_errors)
        monitor_thread.start()

        current_process.wait()  # Ждём завершения ffmpeg
        monitor_thread.join()

        if error_detected:
            print(f"[{datetime.now()}] 🔁 Видео прервано из-за ошибки. Переходим к следующему.")
            return False  # Ошибка — перейти к следующему видео

        print(f"[{datetime.now()}] ⏹ Видео завершилось: {video_path}")
        return True  # Успешно проиграно до конца

    except Exception as e:
        print(f"[{datetime.now()}] ⚠ Ошибка при запуске ffmpeg: {e}")
        return False


def continuous_playback():
    current_category = get_current_category()
    print(f"[{datetime.now()}] ▶ Начинаем показ категории: {current_category}")

    while True:
        new_category = get_current_category()

        if new_category != current_category:
            print(f"[{datetime.now()}] 🔄 Переход на новую категорию: {new_category}")
            current_category = new_category

        folder = os.path.join(BASE_PATH, VIDEO_DIRS[current_category])
        all_videos = find_videos(folder)
        already_played = played_videos[current_category]

        if not all_videos:
            print(f"[{datetime.now()}] ⚠ Нет видео в папке: {folder}")
            time.sleep(10)
            continue

        # Обновляем список невоспроизведённых
        unplayed_videos = [v for v in all_videos if v not in already_played]
        if not unplayed_videos:
            print(f"[{datetime.now()}] ✅ Все видео в {current_category} были воспроизведены. Сброс.")
            played_videos[current_category].clear()
            unplayed_videos = all_videos[:]

        random.shuffle(unplayed_videos)  # случайный порядок

        for video_path in unplayed_videos:
            print(f"[{datetime.now()}] 🟡 Пытаемся воспроизвести: {video_path}")
            success = play_video(video_path)

            if success:
                played_videos[current_category].add(video_path)
                break  # выйти из цикла и перейти к следующему видео
            else:
                print(f"[{datetime.now()}] ⚠ Ошибка с {video_path} — пробуем следующее...\n")
                continue  # пробуем следующее видео

        time.sleep(1)

if __name__ == '__main__':
    print("🚀 IPTV-поток запущен. Ожидаем видео...\n")
    continuous_playback()

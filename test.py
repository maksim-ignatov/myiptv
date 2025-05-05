#!/usr/bin/env python3

import os
import random
import time
import subprocess
import threading
from datetime import datetime

BASE_PATH = os.path.expanduser('~/iptv-cartoons')
SAMPLE_DIR = 'sample'  # Новая папка для тестовых видео

FFMPEG_CMD_TEMPLATE = [
    'ffmpeg', '-re', '-i', '',
    
    # Видео
    '-c:v', 'libx264',
    '-preset', 'veryfast',
    '-tune', 'zerolatency',
    '-x264-params', 'repeat-headers=1',
    '-crf', '23',
    '-maxrate', '2000k',
    '-bufsize', '2000k',

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
    '-fflags', '+genpts+flush_packets',
    '-avoid_negative_ts', 'make_zero',
    '-flush_packets', '1',
    '-max_muxing_queue_size', '1024',

    # RTSP-вывод
    '-loglevel', 'warning',
    '-f', 'rtsp',
    '-rtsp_transport', 'tcp',
    'rtsp://127.0.0.1:8554/stream'
]

current_process = None
played_videos = set()  # Единый набор для всех воспроизведенных видео

def find_videos(folder):
    videos = []
    for root, _, files in os.walk(folder, followlinks=True):
        for file in files:
            if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                full_path = os.path.join(root, file)
                videos.append(full_path)
    return videos

def play_video(video_path):
    global current_process

    cmd = FFMPEG_CMD_TEMPLATE[:]
    cmd[3] = video_path

    print(f"[{datetime.now()}] ▶ Запуск видео: {video_path}")

    try:
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

        monitor_thread = threading.Thread(target=monitor_errors)
        monitor_thread.start()

        current_process.wait()
        monitor_thread.join()

        if error_detected:
            print(f"[{datetime.now()}] 🔁 Видео прервано из-за ошибки. Переходим к следующему.")
            return False

        print(f"[{datetime.now()}] ⏹ Видео завершилось: {video_path}")
        return True

    except Exception as e:
        print(f"[{datetime.now()}] ⚠ Ошибка при запуске ffmpeg: {e}")
        return False

def continuous_playback():
    print(f"[{datetime.now()}] ▶ Начинаем тестовый показ из папки 'sample'")

    while True:
        folder = os.path.join(BASE_PATH, SAMPLE_DIR)
        all_videos = find_videos(folder)

        if not all_videos:
            print(f"[{datetime.now()}] ⚠ Нет видео в папке: {folder}")
            time.sleep(10)
            continue

        unplayed_videos = [v for v in all_videos if v not in played_videos]
        if not unplayed_videos:
            print(f"[{datetime.now()}] ✅ Все тестовые видео были воспроизведены. Сброс.")
            played_videos.clear()
            unplayed_videos = all_videos[:]

        random.shuffle(unplayed_videos)

        for video_path in unplayed_videos:
            print(f"[{datetime.now()}] 🟡 Пытаемся воспроизвести: {video_path}")
            success = play_video(video_path)

            if success:
                played_videos.add(video_path)
                break
            else:
                print(f"[{datetime.now()}] ⚠ Ошибка с {video_path} - пробуем следующее...\n")
                continue

        time.sleep(1)

if __name__ == '__main__':
    print("🧪 Тестовый поток запущен. Ожидаем видео...\n")
    continuous_playback()

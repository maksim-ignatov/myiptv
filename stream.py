import os
import random
import time
import subprocess
import threading
from datetime import datetime

VIDEO_DIRS = {
    'morning': 'morning',
    'day': 'day',
    'evening': 'evening',
    'night': 'night'
}

BASE_PATH = os.path.expanduser('~/iptv-cartoons')

FFMPEG_CMD_TEMPLATE = [
    'ffmpeg', '-re', '-i', '',  # input подставим позже
    '-c:v', 'libx264', '-preset', 'ultrafast',
    '-b:v', '700k',
    '-threads', '2',
    '-max_muxing_queue_size', '1024',
    '-loglevel', 'error',
    '-f', 'mpegts', 'udp://239.0.0.1:1489?pkt_size=1316'
]

current_process = None
played_videos = {
    'morning': set(),
    'day': set(),
    'evening': set(),
    'night': set()
}


def get_current_category():
    hour = datetime.now().hour
    if 6 <= hour < 12:
        return 'morning'
    elif 12 <= hour < 18:
        return 'day'
    elif 18 <= hour < 24:
        return 'evening'
    else:
        return 'night'


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

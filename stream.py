#!/usr/bin/env python3

import json
import os
import random
import signal
import threading
import time
from datetime import datetime
import subprocess

BASE_PATH = '/app'
RTSP_URL = os.getenv('RTSP_URL', 'rtsp://mediamtx:8554/stream')
AUDIO_TRACK = int(os.getenv('AUDIO_TRACK', '0'))  # fallback если дорожка не найдена
VIDEOS_BASE = os.getenv('VIDEOS_BASE', os.path.join(BASE_PATH, 'videos'))
CONFIG_FILE = os.path.join(BASE_PATH, 'config.myiptv')
PLAYED_FILE = os.path.join(BASE_PATH, 'data', 'played.json')

# Если ffmpeg завершился быстрее этого порога — скорее всего RTSP недоступен,
# а не проблема в файле. Видео НЕ помечаем как воспроизведённое.
FAST_FAIL_THRESHOLD = 5.0

SLOTS = ('early_morning', 'morning', 'late_morning', 'afternoon',
         'evening', 'late_evening', 'night', 'late_night')

FFMPEG_BASE_ARGS = [
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


def build_ffmpeg_cmd(video_path, audio_track, settings):
    """Собирает команду ffmpeg. Если задан логотип — добавляет overlay."""
    raw = settings.get('logo', '').strip()
    logo_path = raw if os.path.isabs(raw) else (os.path.join(BASE_PATH, raw) if raw else '')

    position_key = settings.get('logo_position', 'bottom-left')
    overlay_pos = LOGO_POSITIONS.get(position_key, LOGO_POSITIONS['bottom-left'])

    if _logo_exists(logo_path):
        cmd = [
            'ffmpeg', '-re',
            '-err_detect', 'ignore_err',
            '-i', video_path,
            '-i', logo_path,
            '-filter_complex', f'[0:v][1:v]overlay={overlay_pos}',
            '-map', '0:a:' + str(audio_track),
        ]
    else:
        cmd = [
            'ffmpeg', '-re',
            '-err_detect', 'ignore_err',
            '-i', video_path,
            '-map', '0:v:0',
            '-map', '0:a:' + str(audio_track),
        ]
    return cmd + FFMPEG_BASE_ARGS

current_process = None
shutdown_event = threading.Event()
# played_videos[(slot, show_name)] = set of video paths
# show_name — первый компонент пути папки из конфига (т.е. "South Park", а не "South Park/Season 1")
played_videos = {}


def load_played():
    """Загружает историю просмотров из файла при старте."""
    global played_videos
    try:
        with open(PLAYED_FILE, encoding='utf-8') as f:
            data = json.load(f)
        # JSON хранит ключи как "slot|show", значения как списки → конвертируем обратно
        played_videos = {tuple(k.split('|', 1)): set(v) for k, v in data.items()}
        total = sum(len(v) for v in played_videos.values())
        print(f"[{datetime.now()}] 📂 История загружена: {total} просмотренных серий")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[{datetime.now()}] ⚠ Не удалось загрузить историю: {e}")


def save_played():
    """Сохраняет историю просмотров на диск."""
    try:
        os.makedirs(os.path.dirname(PLAYED_FILE), exist_ok=True)
        data = {'|'.join(k): list(v) for k, v in played_videos.items()}
        with open(PLAYED_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[{datetime.now()}] ⚠ Не удалось сохранить историю: {e}")


def print_schedule_summary(config):
    """#1 — сводка расписания при старте + #5 — предупреждение о пустых слотах."""
    print("📋 Расписание:")
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
            print(f"   {slot:15s} → {', '.join(parts)}")
            has_any = True

    wildcard = config.get('*', [])
    if wildcard:
        parts = []
        for entry in wildcard:
            show_name = entry['folder'].replace('\\', '/').split('/')[0]
            folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
            count = len(find_videos(folder_path))
            parts.append(f"{show_name} ({count} серий)")
        print(f"   {'[всегда]':15s} → {', '.join(parts)}")
        has_any = True

    if not has_any:
        print("   (нет настроенных шоу — заполните config.myiptv)")

    settings = config.get('settings', {})
    raw = settings.get('logo', '').strip()
    logo_path = raw if os.path.isabs(raw) else (os.path.join(BASE_PATH, raw) if raw else '')
    position = settings.get('logo_position', 'bottom-left')
    if _logo_exists(logo_path):
        print(f"   🖼 Логотип: {logo_path} ({position})")
    elif logo_path:
        print(f"   🖼 Логотип: ⚠ файл не найден ({logo_path})")
    else:
        print(f"   🖼 Логотип: отключён")

    # #5 — пустые слоты с настроенными, но ненайденными папками
    for slot in SLOTS:
        for entry in config.get(slot, []):
            folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
            if not os.path.isdir(folder_path):
                print(f"   ⚠ [{slot}] папка не найдена: {folder_path}")
    print()


def handle_shutdown(_signum, _frame):
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


def _logo_exists(path):
    """Проверяет доступность файла через open() — работает даже на NTFS/WSL где stat() возвращает ?."""
    if not path:
        return False
    try:
        open(path, 'rb').close()
        return True
    except OSError:
        return False


LOGO_POSITIONS = {
    'top-left':     '10:10',
    'top-right':    'W-w-10:10',
    'bottom-left':  '10:H-h-10',
    'bottom-right': 'W-w-10:H-h-10',
}


def load_config():
    """
    Парсит config.myiptv.
    Возвращает dict:
      'settings': {'logo': str, 'logo_position': str}
      slot: [{'folder': str, 'audio': str}]
      '*': [...]  — папки без временного слота (играют в любое время)
    """
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
    """
    Возвращает (show_videos, audio_map) для текущего слота.

    show_videos: {show_name: [video_paths]}
      show_name — первый компонент пути из конфига ("South Park", не "South Park/Season 1").
      Все сезоны одного шоу объединяются под одним ключом → равные шансы между шоу.

    audio_map: {folder_key: audio_pref}
      folder_key может быть как "South Park", так и "South Park/Season 1" — побеждает длиннее.
    """
    entries = config.get(slot, []) + config.get('*', [])
    show_videos = {}
    audio_map = {}
    for entry in entries:
        show_name = entry['folder'].replace('\\', '/').split('/')[0]
        folder_path = os.path.join(VIDEOS_BASE, entry['folder'])
        vids = find_videos(folder_path)
        if not vids:
            print(f"[{datetime.now()}] ⚠ Нет видео в папке: {folder_path}")
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
        print(f"[{datetime.now()}] ⚠ Дорожка '{preference}' не найдена (найдено {len(matches)} '{lang}'-дорожек), используем дефолт")
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


def get_audio_track(video_path, audio_map):
    """
    Определяет индекс аудиодорожки по audio_map из конфига.
    Поддерживает подпапки: 'South Park/Season 3' более специфично, чем 'South Park'.
    Побеждает самое длинное совпадение.
    """
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
        # #3 — список всех доступных дорожек
        if streams:
            tracks_str = '  '.join(
                f"[{s['index']}] {s['lang'] or '?'}{(' ' + repr(s['title'])) if s['title'] else ''}"
                for s in streams
            )
            print(f"[{datetime.now()}] 🎵 Дорожки: {tracks_str}")
        track = resolve_audio_track(streams, preference)
        print(f"[{datetime.now()}] 🎵 Выбрана: '{preference}' → дорожка {track}")
        return track

    return AUDIO_TRACK


def play_video(video_path, audio_map, settings):
    """
    Returns:
      True  — видео успешно воспроизведено
      False — ошибка в файле, пропустить
      None  — быстрый сбой (RTSP недоступен), не помечать файл как воспроизведённый
    """
    global current_process

    track = get_audio_track(video_path, audio_map)
    cmd = build_ffmpeg_cmd(video_path, track, settings)
    logo_note = ' +лого' if cmd.count('-i') > 1 else ''
    print(f"[{datetime.now()}] ▶ Запуск видео: {video_path} (аудио: {track}{logo_note})")
    start_time = time.time()

    try:
        current_process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        stderr_lines = []
        fatal_keywords = ['no such file or directory', 'moov atom not found', 'invalid data found']

        def monitor_stderr():
            for line in current_process.stderr:
                line = line.rstrip()
                stderr_lines.append(line)
                if any(kw in line.lower() for kw in fatal_keywords):
                    print(f"[{datetime.now()}] ❌ Фатальная ошибка: {line}")
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
            # #4 — длительность воспроизведения
            mins, secs = divmod(int(duration), 60)
            print(f"[{datetime.now()}] ⏹ Завершено за {mins}м {secs:02d}с: {os.path.basename(video_path)}")
            return True

        # Быстрый сбой — RTSP скорее всего недоступен, файл не виноват
        if duration < FAST_FAIL_THRESHOLD:
            print(f"[{datetime.now()}] ⚠ Быстрый сбой за {duration:.1f}с (exit: {exit_code}) — возможно RTSP недоступен")
            if stderr_lines:
                for line in stderr_lines[-5:]:
                    print(f"    {line}")
            return None

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
    current_slot = get_current_category()
    print(f"[{datetime.now()}] ▶ Начинаем показ категории: {current_slot}")

    while not shutdown_event.is_set():
        new_slot = get_current_category()
        if new_slot != current_slot:
            print(f"[{datetime.now()}] 🔄 Переход на новую категорию: {new_slot}")
            current_slot = new_slot

        config = load_config()
        settings = config.get('settings', {})
        show_videos, audio_map = get_shows_for_slot(current_slot, config)

        if not show_videos:
            print(f"[{datetime.now()}] ⚠ Нет видео для категории: {current_slot}")
            shutdown_event.wait(timeout=10)
            continue

        # Для каждого шоу — список непросмотренных серий
        available = {}
        for show, vids in show_videos.items():
            key = (current_slot, show)
            seen = played_videos.get(key, set())
            unplayed = [v for v in vids if v not in seen]
            if not unplayed:
                print(f"[{datetime.now()}] ✅ Все серии '{show}' в {current_slot} воспроизведены. Сброс.")
                played_videos[key] = set()
                unplayed = vids[:]
            available[show] = unplayed

        # Сначала случайное шоу, потом случайная серия — равные шансы между шоу
        chosen_show = random.choice(list(available.keys()))
        video_path = random.choice(available[chosen_show])

        # #2 — прогресс шоу
        total = len(show_videos[chosen_show])
        seen_count = len(played_videos.get((current_slot, chosen_show), set()))
        pct = int(seen_count / total * 100) if total else 0
        print(f"[{datetime.now()}] 🟡 [{chosen_show}] {os.path.basename(video_path)}  ({seen_count}/{total}, {pct}%)")
        result = play_video(video_path, audio_map, settings)

        key = (current_slot, chosen_show)
        if result is True:
            played_videos.setdefault(key, set()).add(video_path)
            save_played()
        elif result is False:
            played_videos.setdefault(key, set()).add(video_path)
            save_played()
            print(f"[{datetime.now()}] ⚠ Пропускаем битый файл: {os.path.basename(video_path)}\n")
        elif result is None:
            print(f"[{datetime.now()}] ⏳ Ждём 5с перед повтором (RTSP?)...")
            shutdown_event.wait(timeout=5)

        shutdown_event.wait(timeout=1)


if __name__ == '__main__':
    print("🚀 IPTV-поток запущен.\n")
    load_played()
    print_schedule_summary(load_config())
    continuous_playback()

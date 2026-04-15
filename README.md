# 📺 myiptv

Личный IPTV-сервер. Берёт видео с диска/NAS, стримит как непрерывный телеканал по RTSP.

## Быстрый старт

```bash
git clone https://github.com/maksimignatov1337/myiptv.git
cd myiptv
```

Укажите путь к видео в `docker-compose.yml`:
```yaml
- /path/to/your/videos:/app/videos
```

Настройте `config.myiptv`, затем:
```bash
docker compose up -d
```

Смотреть: `rtsp://<сервер>:8554/stream`

## config.myiptv

Всё управление через один файл. Примеры и описание форматов — внутри файла.

**Расписание** — шоу по времени суток:
```ini
[late_evening]
South Park = rus:2

[morning]
Чудеса на виражах = rus

[*]
Goofy Goof Troop =    # играет в любое время
```

**Подпапки** — разные настройки для сезонов:
```ini
South Park = rus:2
South Park/Season 7 = eng
```

**Логотип:**
```ini
[settings]
logo = assets/logo.png
logo_position = bottom-left   # top-left, top-right, bottom-left, bottom-right
logo_offset_x = 10            # отступ от края по горизонтали в пикселях (по умолчанию 10)
logo_offset_y = 10            # отступ от края по вертикали в пикселях (по умолчанию 10)
```

**Без повторов подряд** — если в слоте несколько шоу, следующее всегда будет другим:
```ini
[settings]
no_repeat = true   # по умолчанию true
```

При `no_repeat = true`: после `Шоу А` никогда не запустится снова `Шоу А` — только `Шоу Б`, `Шоу В` и т.д. Если в слоте одно шоу — настройка не влияет.

## Структура

```
myiptv/
  config.myiptv       # расписание, аудио, логотип
  docker-compose.yml
  assets/             # логотипы (PNG с прозрачностью)
  data/               # история просмотров — создаётся автоматически
```

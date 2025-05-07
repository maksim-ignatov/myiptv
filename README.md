# 📺 My IPTV

Ваш собственный IPTV-сервер (Linux) - RTSP

## 🚀 Установка

```bash
git clone https://github.com/maksim-ignatov/myiptv.git
```

```bash
cd myiptv
```

## 📂 Подготовка видео

Положите видеофайлы в директории внутри папки `videos/`.


## 🐳 Запуск через Docker Compose

```bash
docker compose up -d
```

## ✅ Результат

Теперь вы получите ссылку для просмотра IPTV:

```bash
rtsp://<server_address>:8554/stream
```
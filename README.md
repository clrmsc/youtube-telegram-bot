# YouTube Telegram Bot для Raspberry Pi (Bookworm)

Бот качает видео с YouTube через **yt-dlp** и либо отправляет файл в чат,
либо сохраняет его в `/media/share/youtube`. Качество: 1080p / 720p / 480p /
360p / MP3. Раз в сутки автоматически обновляет yt-dlp.

## 1. Установка зависимостей

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg
```

`ffmpeg` обязателен — он склеивает видео+аудио и конвертирует в MP3.

## 2. Подготовка папок и кода

```bash
# Скопируй файлы бота в /home/pi/youtube-bot
mkdir -p /home/pi/youtube-bot
cd /home/pi/youtube-bot
# (положи сюда bot.py и requirements.txt)

# Папка для сохранения
sudo mkdir -p /media/share/youtube
sudo chown pi:pi /media/share/youtube

# Виртуальное окружение
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Получи токен бота

1. Напиши в Telegram боту **@BotFather** → `/newbot`.
2. Получишь токен вида `123456789:AAA...`.

## 4. Проверка запуска вручную

```bash
export BOT_TOKEN="ВСТАВЬ_ТОКЕН"
export SAVE_DIR="/media/share/youtube"
source venv/bin/activate
python bot.py
```

Пришли боту ссылку на YouTube — он покажет кнопки выбора качества.

## 5. Автозапуск через systemd

```bash
# Отредактируй youtube-bot.service: впиши токен и проверь пути/пользователя
sudo cp youtube-bot.service /etc/systemd/system/youtube-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now youtube-bot
# Логи:
journalctl -u youtube-bot -f
```

## Важно про лимит размера файла

Стандартный Telegram Bot API **не даёт боту отправлять файлы больше 50 МБ**.
Видео 1080p часто крупнее. Логика бота:

- Если файл больше лимита и выбрана отправка «в чат» — он автоматически
  **сохраняется на Pi** в `/media/share/youtube`, а в чат приходит путь к файлу.
- Чтобы отправлять в чат файлы до 2 ГБ, нужно поднять **локальный Telegram Bot
  API server** и задать `TELEGRAM_UPLOAD_LIMIT_MB=2000`.

Для больших видео проще выбирать «→ на Pi» и забирать файл из сетевой папки.

## Команды бота

- Прислать ссылку YouTube → кнопки выбора качества и места сохранения
- `/update` — обновить yt-dlp вручную
- `/myid` — узнать свой Telegram ID (для `ALLOWED_USER_IDS`)

## Ограничение доступа (рекомендуется)

Чтобы ботом не пользовались чужие, задай свой ID:

```
Environment=ALLOWED_USER_IDS=123456789
```

(несколько ID — через запятую). Узнать ID: команда `/myid`.

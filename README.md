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

- Прислать ссылку YouTube → превью (миниатюра, название, длительность) и кнопки
  выбора качества и места сохранения
- `/queue` — что сейчас качается и сколько в очереди
- `/update` — обновить yt-dlp вручную
- `/myid` — узнать свой Telegram ID (для `ALLOWED_USER_IDS`)

Загрузки идут **по очереди** (по одной за раз — бережёт Pi). У активной и
ожидающей загрузки есть кнопка **«Отмена»**.

## Локальный Bot API server (файлы до 2 ГБ в чат)

Чтобы отправлять в чат файлы крупнее 50 МБ (например, 1080p), нужен локальный
Telegram Bot API server. Самый простой путь — Docker:

```bash
# 1. Получи api_id и api_hash на https://my.telegram.org → API development tools
# 2. Подними сервер (замени значения):
docker run -d --name telegram-bot-api --restart unless-stopped \
  -p 8081:8081 \
  -e TELEGRAM_API_ID=ВАШ_API_ID \
  -e TELEGRAM_API_HASH=ВАШ_API_HASH \
  aiogram/telegram-bot-api:latest
```

Затем в `.env` бота добавь:

```
LOCAL_BOT_API_URL=http://localhost:8081
```

Лимит файла поднимется до 2000 МБ автоматически. Перезапусти бота.

> ⚠️ При первом переходе бота с облачного API на локальный Telegram требует
> один раз «разлогинить» бота из облака (метод `logOut`). Если бот не
> подключается к локальному серверу — выполни один раз:
> `curl https://api.telegram.org/bot<ТОКЕН>/logOut`, затем перезапусти бота.

## Переменные окружения (.env)

| Переменная | Назначение |
|------------|-----------|
| `BOT_TOKEN` | токен от @BotFather (обязательно) |
| `SAVE_DIR` | папка сохранения (по умолчанию `/media/share/youtube`) |
| `ALLOWED_USER_IDS` | белый список ID через запятую |
| `COOKIES_FROM_BROWSER` | cookies из браузера, напр. `firefox:/путь/к/профилю` |
| `COOKIES_FILE` | путь к cookies.txt (если без браузера) |
| `JS_RUNTIME` | JS-движок, напр. `node` |
| `REMOTE_COMPONENTS` | EJS-компоненты, по умолчанию `ejs:npm` |
| `LOCAL_BOT_API_URL` | адрес локального Bot API, напр. `http://localhost:8081` |
| `TELEGRAM_UPLOAD_LIMIT_MB` | лимит отправки в чат (авто: 50 или 2000) |

## Ограничение доступа (рекомендуется)

Чтобы ботом не пользовались чужие, задай свой ID:

```
Environment=ALLOWED_USER_IDS=123456789
```

(несколько ID — через запятую). Узнать ID: команда `/myid`.

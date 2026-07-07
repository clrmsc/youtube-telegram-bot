#!/usr/bin/env python3
"""
Telegram-бот для скачивания видео с YouTube на Raspberry Pi (Bookworm).

Возможности:
  - Скачивание видео в разрешениях 1080 / 720 / 480 / 360 или аудио (mp3).
  - Превью: миниатюра, название, длительность, канал перед выбором качества.
  - Отправка результата прямо в чат ИЛИ сохранение в папку на Raspberry Pi.
  - Очередь загрузок: задания выполняются по одному (бережёт слабый Pi).
  - Кнопка «Отмена» для текущей или ожидающей загрузки.
  - Живой прогресс-бар скачивания.
  - Поддержка локального Telegram Bot API server (файлы до 2 ГБ в чат).
  - Использует yt-dlp (через subprocess); раз в сутки сам обновляет yt-dlp.

Запуск:  python3 bot.py
Конфиг:  переменные окружения / .env (см. README.md)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #

# Токен бота от @BotFather. Лучше задать через переменную окружения.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Папка, куда сохраняются файлы при выборе "Сохранить на Pi".
SAVE_DIR = Path(os.environ.get("SAVE_DIR", "/media/share/youtube"))

# Опциональный белый список Telegram user_id (через запятую).
# Если пусто — бот отвечает всем.
_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x.strip()} if _allowed else set()

# Локальный Telegram Bot API server, например "http://localhost:8081".
# Если задан — бот шлёт запросы туда, а лимит файла поднимается до 2000 МБ.
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").strip().rstrip("/")

# Лимит размера файла для отправки в чат.
# Обычный облачный Bot API: 50 МБ. Локальный сервер: до 2000 МБ.
_default_limit = 2000 if LOCAL_BOT_API_URL else 50
TELEGRAM_UPLOAD_LIMIT_MB = int(os.environ.get("TELEGRAM_UPLOAD_LIMIT_MB", str(_default_limit)))

# Как часто проверять обновления yt-dlp (в часах).
UPDATE_INTERVAL_HOURS = 24

# YouTube часто требует авторизацию ("Sign in to confirm you're not a bot").
# Два способа передать cookies (приоритет у браузера):
#
# 1) COOKIES_FROM_BROWSER — брать cookies прямо из браузера на этой машине.
#    Например: "firefox" или "firefox:/home/pi/.mozilla/firefox/xxxx.default-release"
# 2) COOKIES_FILE — файл cookies.txt в формате Netscape (если браузера нет).
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "").strip()
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/home/pi/youtube-bot/cookies.txt"))

# JavaScript-движок для решения "n-challenge" YouTube (например node).
JS_RUNTIME = os.environ.get("JS_RUNTIME", "").strip()

# Удалённые компоненты EJS (решатель JS-challenge + движок).
# "ejs:npm" — скачивает решатель и deno с npm-реестра загрузчиком yt-dlp
# (не требует системного deno/node, работает через прокси). Кешируется.
REMOTE_COMPONENTS = os.environ.get("REMOTE_COMPONENTS", "ejs:npm").strip()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("youtube-bot")

# Разрешения и соответствующие форматы yt-dlp.
QUALITY_FORMATS = {
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
    "360": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
}

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/|youtu\.be/|youtube\.com/shorts/)\S+",
    re.IGNORECASE,
)

# token -> {"url", "title", "is_photo"} — данные превью между сообщением и выбором.
_pending: dict[str, dict] = {}

# Очередь загрузок и реестр активных заданий.
# Очередь создаётся в on_post_init (когда уже есть event loop).
download_queue: "asyncio.Queue[Job] | None" = None
JOBS: dict[str, "Job"] = {}
_state: dict = {"current": None}  # текущее выполняемое задание (для /queue)


class CancelledDownload(Exception):
    """Загрузка прервана пользователем."""


class Job:
    def __init__(self, job_id, url, quality, dest, title, chat_id, message_id, is_photo):
        self.job_id = job_id
        self.url = url
        self.quality = quality
        self.dest = dest
        self.title = title
        self.chat_id = chat_id
        self.message_id = message_id
        self.is_photo = is_photo
        self.proc: subprocess.Popen | None = None
        self.cancelled = False


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def common_yt_dlp_args() -> list[str]:
    """Общие аргументы yt-dlp: cookies, движок, EJS-компоненты."""
    args: list[str] = []
    if COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    elif COOKIES_FILE.exists():
        args += ["--cookies", str(COOKIES_FILE)]
    if JS_RUNTIME:
        args += ["--js-runtimes", JS_RUNTIME]
    if REMOTE_COMPONENTS:
        args += ["--remote-components", REMOTE_COMPONENTS]
    return args


def build_keyboard(token: str) -> InlineKeyboardMarkup:
    """Клавиатура: качество × место назначения."""
    rows = []
    for q in ["1080", "720", "480", "360"]:
        rows.append(
            [
                InlineKeyboardButton(f"{q}p → в чат", callback_data=f"dl|{token}|{q}|chat"),
                InlineKeyboardButton(f"{q}p → на Pi", callback_data=f"dl|{token}|{q}|disk"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🎵 MP3 → в чат", callback_data=f"dl|{token}|mp3|chat"),
            InlineKeyboardButton("🎵 MP3 → на Pi", callback_data=f"dl|{token}|mp3|disk"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{job_id}")]]
    )


def fetch_metadata(url: str) -> dict:
    """Достаёт название/длительность/канал/миниатюру одним вызовом yt-dlp."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--skip-download", "--dump-single-json",
        *common_yt_dlp_args(),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:] or "не удалось получить информацию")
    data = json.loads(proc.stdout)
    vid = data.get("id", "")
    return {
        "title": data.get("title") or "видео",
        "duration": fmt_duration(data.get("duration")),
        "uploader": data.get("uploader") or data.get("channel") or "—",
        "thumbnail": data.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None),
    }


def run_yt_dlp(url: str, quality: str, out_dir: Path, job: "Job | None" = None, progress_cb=None) -> Path:
    """
    Запускает yt-dlp как отдельный процесс. Возвращает путь к скачанному файлу.
    progress_cb(percent, speed, eta) — колбэк для прогресс-бара.
    Если job.cancelled выставлен и процесс убит — бросает CancelledDownload.
    """
    out_template = str(out_dir / "%(title).180s [%(id)s].%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        # Сохраняем читаемое имя (кириллица, пробелы), но чистим символы,
        # недопустимые на exfat/SMB/Windows — файл откроется с любого устройства.
        "--windows-filenames",
        "-o", out_template,
        "--print", "after_move:FILEPATH=%(filepath)s",
        "--no-simulate",
        "--newline",
        "--progress-template",
        "download:PROGRESS=%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        *common_yt_dlp_args(),
    ]

    if quality == "mp3":
        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["-f", QUALITY_FORMATS[quality], "--merge-output-format", "mp4"]

    cmd.append(url)
    logger.info("Запуск: %s", " ".join(cmd))

    # start_new_session=True — отдельная группа процессов, чтобы при отмене
    # убить и дочерние процессы (ffmpeg/deno) целиком.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if job is not None:
        job.proc = proc

    filepath: str | None = None
    log_tail: list[str] = []

    for raw in proc.stdout:  # type: ignore[union-attr]
        line = raw.rstrip("\n")
        if not line:
            continue
        if line.startswith("PROGRESS="):
            if progress_cb:
                parts = line[len("PROGRESS="):].split("|")
                pct_str = parts[0] if len(parts) > 0 else ""
                speed = parts[1] if len(parts) > 1 else ""
                eta = parts[2] if len(parts) > 2 else ""
                try:
                    pct = float(pct_str.strip().rstrip("%"))
                except ValueError:
                    pct = 0.0
                progress_cb(pct, speed.strip(), eta.strip())
        elif line.startswith("FILEPATH="):
            filepath = line[len("FILEPATH="):].strip()
        else:
            log_tail.append(line)
            if len(log_tail) > 40:
                log_tail.pop(0)

    proc.wait()

    if job is not None and job.cancelled:
        raise CancelledDownload()

    if proc.returncode != 0:
        raise RuntimeError("yt-dlp завершился с ошибкой:\n" + "\n".join(log_tail[-15:]))

    if filepath and Path(filepath).exists():
        return Path(filepath)

    files = sorted(out_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if files:
        return files[0]
    raise RuntimeError("Не удалось определить путь к скачанному файлу.")


def update_yt_dlp() -> str:
    """Обновляет yt-dlp через pip. Возвращает текстовый отчёт."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    tail = "\n".join(out.splitlines()[-3:])
    if proc.returncode != 0:
        logger.error("Обновление yt-dlp не удалось: %s", tail)
        return f"❌ Обновление не удалось:\n{tail}"
    logger.info("yt-dlp обновление: %s", tail)
    return f"✅ yt-dlp проверен/обновлён:\n{tail}"


async def edit_control(bot, job: "Job", text: str, markup=None) -> None:
    """Безопасно правит управляющее сообщение (подпись фото или текст)."""
    try:
        if job.is_photo:
            await bot.edit_message_caption(
                chat_id=job.chat_id, message_id=job.message_id,
                caption=text, reply_markup=markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=job.chat_id, message_id=job.message_id,
                text=text, reply_markup=markup,
            )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  Воркер очереди и собственно загрузка
# --------------------------------------------------------------------------- #

async def queue_worker(app: Application) -> None:
    """Фоновый воркер: берёт задания из очереди и выполняет по одному."""
    logger.info("Воркер очереди запущен.")
    while True:
        job = await download_queue.get()
        try:
            if job.cancelled:
                await edit_control(app.bot, job, f"🎬 {job.title}\n\n🚫 Отменено")
            else:
                _state["current"] = job
                await process_download(app, job)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка в воркере")
        finally:
            _state["current"] = None
            JOBS.pop(job.job_id, None)
            download_queue.task_done()


async def process_download(app: Application, job: "Job") -> None:
    bot = app.bot
    loop = asyncio.get_running_loop()
    label = "MP3" if job.quality == "mp3" else f"{job.quality}p"
    where = "в чат" if job.dest == "chat" else "на Pi"
    head = f"🎬 {job.title}"

    await edit_control(bot, job, f"{head}\n\n⏳ Скачиваю {label} ({where})…", cancel_keyboard(job.job_id))
    await bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_VIDEO)

    if job.dest == "disk":
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        work_dir = SAVE_DIR
        cleanup = False
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="ytbot_"))
        cleanup = True

    progress_state = {"last_edit": 0.0, "last_text": ""}

    def on_progress(pct: float, speed: str, eta: str) -> None:
        now = time.monotonic()
        if now - progress_state["last_edit"] < 3.0:
            return
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        text = (
            f"{head}\n\n⏳ Скачиваю {label} ({where})\n"
            f"[{bar}] {pct:.0f}%\n"
            f"⚡ {speed or '—'}   ⏳ ETA {eta or '—'}"
        )
        if text == progress_state["last_text"]:
            return
        progress_state["last_edit"] = now
        progress_state["last_text"] = text
        asyncio.run_coroutine_threadsafe(
            edit_control(bot, job, text, cancel_keyboard(job.job_id)), loop
        )

    try:
        file_path = await asyncio.to_thread(
            run_yt_dlp, job.url, job.quality, work_dir, job, on_progress
        )
    except CancelledDownload:
        await edit_control(bot, job, f"{head}\n\n🚫 Отменено")
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка скачивания")
        await edit_control(bot, job, f"{head}\n\n❌ Ошибка:\n{e}")
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        return

    size_mb = file_path.stat().st_size / (1024 * 1024)

    if job.dest == "disk":
        await edit_control(bot, job, f"{head}\n\n✅ Сохранено на Pi ({size_mb:.1f} МБ):\n{file_path}")
        return

    # dest == "chat"
    if size_mb > TELEGRAM_UPLOAD_LIMIT_MB:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        final = SAVE_DIR / file_path.name
        shutil.move(str(file_path), str(final))
        shutil.rmtree(work_dir, ignore_errors=True)
        await edit_control(
            bot, job,
            f"{head}\n\n⚠️ Файл {size_mb:.1f} МБ больше лимита ({TELEGRAM_UPLOAD_LIMIT_MB} МБ), "
            f"сохранён на Pi:\n{final}",
        )
        return

    try:
        await edit_control(bot, job, f"{head}\n\n📤 Отправляю в чат ({size_mb:.1f} МБ)…")
        with open(file_path, "rb") as f:
            if job.quality == "mp3":
                await bot.send_audio(
                    job.chat_id, audio=f, filename=file_path.name,
                    read_timeout=900, write_timeout=900,
                )
            else:
                await bot.send_video(
                    job.chat_id, video=f, filename=file_path.name,
                    supports_streaming=True, read_timeout=900, write_timeout=900,
                )
        await edit_control(bot, job, f"{head}\n\n✅ Готово: {file_path.name}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка отправки")
        await edit_control(bot, job, f"{head}\n\n❌ Не удалось отправить файл:\n{e}")
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Обработчики команд
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Пришли ссылку на YouTube — покажу превью и предложу качество.\n\n"
        "Команды:\n"
        "/queue — что сейчас в очереди\n"
        "/update — обновить yt-dlp вручную\n"
        "/myid — показать твой Telegram ID"
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    current = _state["current"]
    waiting = download_queue.qsize()
    lines = []
    if current:
        lines.append(f"▶️ Сейчас: {current.title}")
    lines.append(f"🕓 В очереди: {waiting}")
    await update.message.reply_text("\n".join(lines) if lines else "Очередь пуста.")


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("⏳ Обновляю yt-dlp...")
    report = await asyncio.to_thread(update_yt_dlp)
    await msg.edit_text(report)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = update.message.text or ""
    match = YOUTUBE_RE.search(text)
    if not match:
        await update.message.reply_text("Это не похоже на ссылку YouTube 🤔")
        return

    url = match.group(0)
    token = f"{update.effective_chat.id}_{update.message.message_id}"

    info_msg = await update.message.reply_text("⏳ Получаю информацию о видео…")

    try:
        meta = await asyncio.to_thread(fetch_metadata, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("Метаданные не получены: %s", e)
        # Фолбэк: без превью, просто кнопки.
        _pending[token] = {"url": url, "title": "видео", "is_photo": False}
        await info_msg.edit_text(
            "Не удалось получить превью, но скачать можно.\nВыбери качество:",
            reply_markup=build_keyboard(token),
        )
        return

    _pending[token] = {"url": url, "title": meta["title"], "is_photo": bool(meta["thumbnail"])}
    caption = (
        f"🎬 {meta['title']}\n"
        f"⏱ {meta['duration']}   📺 {meta['uploader']}\n\n"
        "Выбери качество и куда сохранить:"
    )

    if meta["thumbnail"]:
        try:
            await context.bot.send_photo(
                update.effective_chat.id,
                photo=meta["thumbnail"],
                caption=caption,
                reply_markup=build_keyboard(token),
            )
            await info_msg.delete()
            return
        except Exception:  # noqa: BLE001
            pass  # не вышло с фото — упадём в текстовый вариант ниже

    _pending[token]["is_photo"] = False
    await info_msg.edit_text(caption, reply_markup=build_keyboard(token))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, quality, dest = query.data.split("|")
    except ValueError:
        return

    pending = _pending.get(token)
    if not pending:
        await edit_control(
            context.bot,
            Job("x", "", "", "", "", query.message.chat_id, query.message.message_id,
                bool(query.message.photo)),
            "⚠️ Ссылка устарела, пришли её ещё раз.",
        )
        return

    chat_id = query.message.chat_id
    message_id = query.message.message_id
    job_id = f"{chat_id}_{message_id}"
    job = Job(
        job_id=job_id, url=pending["url"], quality=quality, dest=dest,
        title=pending["title"], chat_id=chat_id, message_id=message_id,
        is_photo=bool(query.message.photo),
    )
    JOBS[job_id] = job

    ahead = download_queue.qsize() + (1 if _state["current"] else 0)
    if ahead > 0:
        status = f"🕓 В очереди (перед тобой: {ahead})"
    else:
        status = "🕓 Скоро начну…"
    await edit_control(context.bot, job, f"🎬 {job.title}\n\n{status}", cancel_keyboard(job_id))

    await download_queue.put(job)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        job_id = query.data.split("|", 1)[1]
    except IndexError:
        await query.answer()
        return

    job = JOBS.get(job_id)
    if not job:
        await query.answer("Уже завершено")
        return

    job.cancelled = True
    # Убиваем всю группу процессов (yt-dlp + ffmpeg/deno), если запущена.
    if job.proc and job.proc.poll() is None:
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                job.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
    await query.answer("Отменяю…")


# --------------------------------------------------------------------------- #
#  Фоновые задачи и запуск
# --------------------------------------------------------------------------- #

async def scheduled_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Плановая проверка обновлений yt-dlp...")
    await asyncio.to_thread(update_yt_dlp)


async def on_post_init(app: Application) -> None:
    # Создаём очередь и стартуем воркер внутри loop приложения.
    global download_queue
    download_queue = asyncio.Queue()
    app.create_task(queue_worker(app))


def main() -> None:
    if not BOT_TOKEN:
        print("Ошибка: не задан BOT_TOKEN (переменная окружения).", file=sys.stderr)
        sys.exit(1)

    update_yt_dlp()

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(30)
        .post_init(on_post_init)
    )
    if LOCAL_BOT_API_URL:
        logger.info("Использую локальный Bot API: %s", LOCAL_BOT_API_URL)
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(
            f"{LOCAL_BOT_API_URL}/file/bot"
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    if app.job_queue:
        app.job_queue.run_repeating(
            scheduled_update,
            interval=UPDATE_INTERVAL_HOURS * 3600,
            first=UPDATE_INTERVAL_HOURS * 3600,
        )

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

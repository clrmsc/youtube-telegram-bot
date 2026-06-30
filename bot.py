#!/usr/bin/env python3
"""
Telegram-бот для скачивания видео с YouTube на Raspberry Pi (Bookworm).

Возможности:
  - Скачивание видео в разрешениях 1080 / 720 / 480 / 360 или аудио (mp3).
  - Отправка результата прямо в чат ИЛИ сохранение в папку на Raspberry Pi.
  - Использует yt-dlp (через subprocess), что позволяет применять обновления
    библиотеки без перезапуска бота.
  - Раз в сутки проверяет и автоматически обновляет yt-dlp.

Запуск:  python3 bot.py
Конфиг:  переменные окружения или файл config.py / .env (см. README.md)
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

# Лимит размера файла для отправки в чат через стандартный Bot API.
# Обычный Bot API не даёт боту отправлять файлы больше 50 МБ.
# Если поднят локальный Bot API server — можно увеличить до 2000 МБ.
TELEGRAM_UPLOAD_LIMIT_MB = int(os.environ.get("TELEGRAM_UPLOAD_LIMIT_MB", "50"))

# Как часто проверять обновления yt-dlp (в часах).
UPDATE_INTERVAL_HOURS = 24

# YouTube часто требует авторизацию ("Sign in to confirm you're not a bot").
# Два способа передать cookies (приоритет у браузера):
#
# 1) COOKIES_FROM_BROWSER — брать cookies прямо из браузера на этой машине.
#    Например: "firefox" или "firefox:/home/pi/snap/firefox/common/.mozilla/firefox"
#    (для snap-версии Firefox нужно указать путь к профилю).
# 2) COOKIES_FILE — файл cookies.txt в формате Netscape (если браузера на машине нет).
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "").strip()
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/home/pi/youtube-bot/cookies.txt"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("youtube-bot")

# Разрешения и соответствующие форматы yt-dlp.
# Для видео берём лучший mp4 с высотой <= указанной + лучшее аудио, склеиваем в mp4.
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

# Временное хранилище URL по callback-токену (чтобы не упереться в лимит 64 байта у callback_data).
_pending_urls: dict[str, str] = {}


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


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


def run_yt_dlp(url: str, quality: str, out_dir: Path) -> Path:
    """
    Синхронный запуск yt-dlp как отдельного процесса.
    Возвращает путь к скачанному файлу.
    """
    out_template = str(out_dir / "%(title).180s [%(id)s].%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--restrict-filenames",
        "-o", out_template,
        "--print", "after_move:filepath",  # печатает итоговый путь
        "--no-simulate",
    ]

    # Передаём cookies (обходит "Sign in to confirm you're not a bot").
    # Приоритет: браузер на этой машине, иначе файл cookies.txt.
    if COOKIES_FROM_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    elif COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    if quality == "mp3":
        cmd += [
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
        ]
    else:
        cmd += [
            "-f", QUALITY_FORMATS[quality],
            "--merge-output-format", "mp4",
        ]

    cmd.append(url)

    logger.info("Запуск: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp завершился с ошибкой:\n{proc.stderr[-1500:]}")

    # Последняя непустая строка stdout — путь к файлу.
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if paths and Path(paths[-1]).exists():
        return Path(paths[-1])

    # Фолбэк: самый свежий файл в каталоге.
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


# --------------------------------------------------------------------------- #
#  Обработчики команд
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Привет! Пришли мне ссылку на YouTube-видео, "
        "и я предложу скачать его в нужном качестве.\n\n"
        "Команды:\n"
        "/update — обновить yt-dlp вручную\n"
        "/myid — показать твой Telegram ID"
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")


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
    # Короткий токен на основе message_id — без random, чтобы было детерминированно.
    token = f"{update.effective_chat.id}_{update.message.message_id}"
    _pending_urls[token] = url

    await update.message.reply_text(
        "Выбери качество и куда сохранить:",
        reply_markup=build_keyboard(token),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, quality, dest = query.data.split("|")
    except ValueError:
        await query.edit_message_text("Некорректные данные кнопки.")
        return

    url = _pending_urls.get(token)
    if not url:
        await query.edit_message_text("⚠️ Ссылка устарела, пришли её ещё раз.")
        return

    label = "MP3" if quality == "mp3" else f"{quality}p"
    where = "в чат" if dest == "chat" else "на Pi"
    await query.edit_message_text(f"⏳ Скачиваю {label} ({where})...")

    chat_id = query.message.chat_id
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

    # Если сохраняем на диск — качаем сразу в SAVE_DIR.
    # Если отправляем в чат — качаем во временную папку.
    if dest == "disk":
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        work_dir = SAVE_DIR
        cleanup = False
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="ytbot_"))
        cleanup = True

    try:
        file_path = await asyncio.to_thread(run_yt_dlp, url, quality, work_dir)
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка скачивания")
        await query.edit_message_text(f"❌ Ошибка:\n{e}")
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
        return

    size_mb = file_path.stat().st_size / (1024 * 1024)

    if dest == "disk":
        await query.edit_message_text(
            f"✅ Сохранено на Pi ({size_mb:.1f} МБ):\n`{file_path}`",
            parse_mode="Markdown",
        )
        return

    # dest == "chat"
    if size_mb > TELEGRAM_UPLOAD_LIMIT_MB:
        # Слишком большой для отправки — сохраняем на диск и сообщаем.
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        final = SAVE_DIR / file_path.name
        shutil.move(str(file_path), str(final))
        shutil.rmtree(work_dir, ignore_errors=True)
        await query.edit_message_text(
            f"⚠️ Файл {size_mb:.1f} МБ больше лимита Telegram "
            f"({TELEGRAM_UPLOAD_LIMIT_MB} МБ), поэтому сохранён на Pi:\n`{final}`",
            parse_mode="Markdown",
        )
        return

    try:
        await query.edit_message_text(f"📤 Отправляю в чат ({size_mb:.1f} МБ)...")
        with open(file_path, "rb") as f:
            if quality == "mp3":
                await context.bot.send_audio(chat_id, audio=f, filename=file_path.name)
            else:
                await context.bot.send_video(
                    chat_id, video=f, filename=file_path.name, supports_streaming=True
                )
        await query.edit_message_text(f"✅ Готово: {file_path.name}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка отправки")
        await query.edit_message_text(f"❌ Не удалось отправить файл:\n{e}")
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Фоновая задача обновления
# --------------------------------------------------------------------------- #

async def scheduled_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Плановая проверка обновлений yt-dlp...")
    await asyncio.to_thread(update_yt_dlp)


# --------------------------------------------------------------------------- #
#  Точка входа
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN:
        print("Ошибка: не задан BOT_TOKEN (переменная окружения).", file=sys.stderr)
        sys.exit(1)

    # Обновим yt-dlp при старте.
    update_yt_dlp()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # Раз в сутки обновляем yt-dlp.
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

#!/usr/bin/env python3
"""NihaoExpress Telegram intake bot for Yandex Cloud Functions."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

# В облаке папка с кодом только для чтения. Используем /tmp для сохранений.
BASE_DIR = Path(tempfile.gettempdir())
# .env читаем из папки с кодом
ENV_FILE = Path(__file__).resolve().parent / ".env"
OFFSET_FILE = BASE_DIR / ".polling-offset"
MANAGER_ID_FILE = BASE_DIR / ".manager-chat-id"
TICKETS_FILE = BASE_DIR / ".tickets.json"

SESSIONS: dict[int, dict[str, Any]] = {}

def load_local_env() -> None:
    """Read simple KEY=VALUE settings without requiring python-dotenv."""
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)

load_local_env()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "bomj01").strip().lstrip("@").casefold()
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "").strip()

if MANAGER_ID_FILE.exists():
    MANAGER_CHAT_ID = MANAGER_ID_FILE.read_text(encoding="utf-8").strip()

try:
    TICKETS: dict[str, dict[str, Any]] = json.loads(
        TICKETS_FILE.read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError):
    TICKETS = {}

REPLY_TARGETS: dict[int, str] = {}
MEDIA_FIELDS = {
    "photo", "document", "video", "video_note", "voice", "audio",
    "animation", "sticker", "location", "venue", "contact",
    "poll", "dice", "game", "invoice", "successful_payment",
    "passport_data", "web_app_data", "paid_media",
}
API_URL = f"https://api.telegram.org/bot{TOKEN}/" if TOKEN else ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("nihaoexpress-bot")


class TelegramError(RuntimeError):
    pass


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📝 Оставить заявку", "style": "primary"}],
        [{"text": "ℹ️ Что вы можете"}, {"text": "👤 Менеджер"}],
    ],
    "resize_keyboard": True,
}
MANAGER_SETUP_BUTTON = "🛠 Подключить этот чат"
SERVICE_KEYBOARD = {
    "keyboard": [
        [{"text": "Закупить готовый товар", "style": "primary"}],
        [{"text": "Найти фабрику / производство", "style": "primary"}],
        [{"text": "Производство под своим брендом", "style": "primary"}],
        [{"text": "Образец / проверка качества", "style": "primary"}],
        [{"text": "Доставка", "style": "primary"}, {"text": "Другое"}],
        [{"text": "Отмена"}],
    ],
    "resize_keyboard": True,
}
PRODUCT_KEYBOARD = {
    "keyboard": [
        [{"text": "Дальше", "style": "primary"}],
        [{"text": "Отмена", "style": "danger"}],
    ],
    "resize_keyboard": True,
}
VOLUME_KEYBOARD = {
    "keyboard": [
        [{"text": "Пока уточняю"}],
        [{"text": "Отмена", "style": "danger"}],
    ],
    "resize_keyboard": True,
}
PRIORITY_KEYBOARD = {
    "keyboard": [
        [{"text": "Себестоимость", "style": "primary"}, {"text": "Качество", "style": "primary"}],
        [{"text": "Своя марка / изменения", "style": "primary"}, {"text": "Образец", "style": "primary"}],
        [{"text": "Дальше", "style": "primary"}, {"text": "Отмена", "style": "danger"}],
    ],
    "resize_keyboard": True,
}
CONFIRM_KEYBOARD = {
    "keyboard": [
        [{"text": "Отправить менеджеру", "style": "success"}],
        [{"text": "Начать заново"}],
    ],
    "resize_keyboard": True,
}


def api_call(method: str, payload: dict[str, Any] | None = None) -> Any:
    if not TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")

    request = urllib.request.Request(
        API_URL + method,
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:400]
        raise TelegramError(f"Telegram returned HTTP {exc.code}: {details}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TelegramError("Could not connect to Telegram") from None
    except json.JSONDecodeError:
        raise TelegramError("Telegram returned an unreadable response") from None

    if not result.get("ok"):
        raise TelegramError(result.get("description", "Telegram API request failed"))
    return result.get("result")


def send_message(
    chat_id: int | str,
    text: str,
    keyboard: dict[str, Any] | None = None,
    inline_keyboard: list[list[dict[str, str]]] | None = None,
) -> Any:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    return api_call("sendMessage", payload)


def clipped(value: str, limit: int = 700) -> str:
    value = value.replace("\x00", "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def is_manager(user: dict[str, Any], chat_id: int | None = None) -> bool:
    if chat_id is not None and MANAGER_CHAT_ID and str(chat_id) == MANAGER_CHAT_ID:
        return True
    username = user.get("username", "").casefold()
    return bool(username and username == MANAGER_USERNAME)


def home_keyboard(user: dict[str, Any]) -> dict[str, Any]:
    keyboard = {
        "keyboard": [row.copy() for row in MAIN_KEYBOARD["keyboard"]],
        "resize_keyboard": True,
    }
    if is_manager(user):
        keyboard["keyboard"].append(
            [{"text": MANAGER_SETUP_BUTTON, "style": "primary"}]
        )
    return keyboard


def main_menu(
    chat_id: int, first_name: str = "", user: dict[str, Any] | None = None
) -> None:
    name = f", {clipped(first_name, 60)}" if first_name else ""
    send_message(
        chat_id,
        "👋 Привет" + name + "! Я помогу подготовить короткую заявку для NihaoExpress.\n\n"
        "Мы ищем фабрики и производителей в Китае, обсуждаем цену и параметры товара, "
        "помогаем проверить образцы и организуем поставки. Ответьте на несколько вопросов — "
        "менеджер получит сводку и продолжит общение с вами в личных сообщениях.",
        keyboard=home_keyboard(user or {}),
    )


def begin_brief(chat_id: int) -> None:
    SESSIONS[chat_id] = {
        "step": "service",
        "service": "",
        "product_notes": [],
        "attachments": [],
        "volume": "",
        "destination": "",
        "priorities": [],
        "contact_method": "В личных сообщениях",
    }
    send_message(
        chat_id,
        "🔎 Что нужно сделать? Выберите ближайший вариант — детали уточним по ходу.",
        keyboard=SERVICE_KEYBOARD,
    )


def ask_product(chat_id: int) -> None:
    SESSIONS[chat_id]["step"] = "product"
    send_message(
        chat_id,
        "Пришлите фото, ссылку или описание товара. Можно добавить несколько фото; "
        "когда закончите, нажмите «Дальше».",
        keyboard=PRODUCT_KEYBOARD,
    )


def ask_volume(chat_id: int) -> None:
    SESSIONS[chat_id]["step"] = "volume"
    send_message(
        chat_id,
        "Какой объём планируете? Можно примерно. "
        "Если пока неизвестно, нажмите «Пока уточняю».",
        keyboard=VOLUME_KEYBOARD,
    )


def ask_destination(chat_id: int) -> None:
    SESSIONS[chat_id]["step"] = "destination"
    send_message(
        chat_id,
        "Куда доставить товар? Напишите страну и город, например: Россия, Москва.",
        keyboard={
            "keyboard": [[{"text": "Отмена", "style": "danger"}]],
            "resize_keyboard": True,
        },
    )


def ask_priorities(chat_id: int) -> None:
    SESSIONS[chat_id]["step"] = "priorities"
    send_message(
        chat_id,
        "Что особенно важно: цена, качество, своя марка, изменения товара или образец? "
        "Можно выбрать несколько вариантов или написать своими словами.",
        keyboard=PRIORITY_KEYBOARD,
    )


def shown(value: str) -> str:
    return clipped(value, 700) if value else "Не указано"


def product_summary(session: dict[str, Any]) -> str:
    notes = "\n".join(session["product_notes"])
    attachments = session["attachments"]
    if notes and attachments:
        return f"{clipped(notes, 900)}\nВложения: {len(attachments)}"
    if notes:
        return clipped(notes, 1100)
    if attachments:
        return f"Фото / файлы приложены ({len(attachments)})"
    return "Не указано"


def review_brief(chat_id: int) -> None:
    session = SESSIONS[chat_id]
    session["step"] = "confirm"
    priorities = ", ".join(session["priorities"]) or "Не указано"
    text = (
        "Проверьте заявку:\n\n"
        f"Запрос: {shown(session['service'])}\n"
        f"Товар: {product_summary(session)}\n"
        f"Объём: {shown(session['volume'])}\n"
        f"Доставка: {shown(session['destination'])}\n"
        f"Приоритеты: {shown(priorities)}\n\n"
        f"Связь с менеджером: {shown(session['contact_method'])}\n\n"
        "Если всё верно, отправьте заявку менеджеру. Ответы и вложения будут переданы "
        "менеджеру NihaoExpress, чтобы он ответил по вашему запросу."
    )
    send_message(chat_id, text, keyboard=CONFIRM_KEYBOARD)


def display_name(user: dict[str, Any]) -> str:
    name = " ".join(
        part for part in (user.get("first_name", ""), user.get("last_name", "")) if part
    )
    return clipped(name or "Имя не указано", 100)


def save_tickets() -> None:
    temporary = TICKETS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(TICKETS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(TICKETS_FILE)


def ticket_for_customer(chat_id: int) -> str | None:
    for ticket, details in reversed(list(TICKETS.items())):
        if int(details.get("customer_chat_id", 0)) == chat_id:
            return ticket
    return None


def answer_callback(callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    api_call("answerCallbackQuery", payload)


def submit_brief(chat_id: int, user: dict[str, Any]) -> None:
    if not MANAGER_CHAT_ID:
        send_message(
            chat_id,
            "Уведомления менеджеру ещё не подключены. Сводка заявки выше — перешлите её "
            "основателю @bomj01 вместе с вложениями. После настройки бот будет отправлять "
            "заявки автоматически.",
            keyboard=CONFIRM_KEYBOARD,
        )
        return

    session = SESSIONS[chat_id]
    ticket = "NE-" + uuid4().hex[:6].upper()
    username = user.get("username")
    username_line = f"@{username}" if username else "не указан"
    priorities = ", ".join(session["priorities"]) or "Не указано"
    summary = (
        f"Новая заявка NihaoExpress · {ticket}\n\n"
        f"Запрос: {shown(session['service'])}\n"
        f"Товар: {product_summary(session)}\n"
        f"Объём: {shown(session['volume'])}\n"
        f"Доставка: {shown(session['destination'])}\n"
        f"Приоритеты: {shown(priorities)}\n"
        f"Как связаться: {shown(session['contact_method'])}\n\n"
        f"Клиент: {display_name(user)}\n"
        f"Telegram: {username_line}\n"
        "Предпочтительный способ связи: личные сообщения."
    )
    TICKETS[ticket] = {
        "customer_chat_id": chat_id,
        "contact_method": session["contact_method"],
    }
    save_tickets()
    try:
        api_call(
            "sendMessage",
            {
                "chat_id": MANAGER_CHAT_ID,
                "text": summary,
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "Личное сообщение", "url": f"tg://user?id={user.get('id', chat_id)}"},
                        {"text": "Ответить через бота", "callback_data": f"reply|{ticket}"},
                    ]]
                },
            },
        )
    except TelegramError:
        TICKETS.pop(ticket, None)
        save_tickets()
        LOG.exception("Could not deliver ticket %s to manager", ticket)
        send_message(
            chat_id,
            "Не получилось передать заявку менеджеру. Попробуйте ещё раз или напишите "
            "напрямую @bomj01.",
            keyboard=CONFIRM_KEYBOARD,
        )
        return

    for attachment in session["attachments"]:
        try:
            api_call(
                "copyMessage",
                {
                    "chat_id": MANAGER_CHAT_ID,
                    "from_chat_id": chat_id,
                    "message_id": attachment["message_id"],
                },
            )
        except TelegramError:
            LOG.exception("Could not copy an attachment for ticket %s", ticket)

    del SESSIONS[chat_id]
    send_message(
        chat_id,
        f"✅ Готово, заявка {ticket} передана менеджеру. Он продолжит общение с вами в личных сообщениях.",
        keyboard=MAIN_KEYBOARD,
    )


def handle_callback_query(query: dict[str, Any]) -> None:
    callback_id = query.get("id", "")
    user = query.get("from", {})
    message = query.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    data = query.get("data", "")

    if not is_manager(user, chat_id) or not chat_id or str(chat_id) != MANAGER_CHAT_ID:
        answer_callback(callback_id, "Эта кнопка доступна только менеджеру.")
        return
    if not data.startswith("reply|"):
        answer_callback(callback_id, "Неизвестное действие.")
        return

    ticket = data.split("|", 1)[1]
    if ticket not in TICKETS:
        answer_callback(callback_id, "Заявка больше недоступна.")
        return

    REPLY_TARGETS[chat_id] = ticket
    answer_callback(callback_id, "Можно отвечать клиенту через бота.")
    send_message(
        chat_id,
        f"Ответ по заявке {ticket}. Напишите текст или отправьте файл. "
        "Ответ уйдёт клиенту от имени NihaoExpress. Чтобы завершить режим ответа, отправьте /cancel.",
    )


def relay_manager_message(chat_id: int, message: dict[str, Any]) -> None:
    ticket = REPLY_TARGETS.get(chat_id)
    details = TICKETS.get(ticket or "")
    if not details:
        REPLY_TARGETS.pop(chat_id, None)
        send_message(chat_id, "Не нашёл эту заявку. Нажмите «Ответить через бота» в уведомлении менеджеру.")
        return

    customer_chat_id = int(details["customer_chat_id"])
    text = (message.get("text") or message.get("caption") or "").strip()
    has_media = any(field in message for field in MEDIA_FIELDS)
    try:
        if has_media:
            send_message(customer_chat_id, "Менеджер NihaoExpress прислал материал:")
            api_call(
                "copyMessage",
                {
                    "chat_id": customer_chat_id,
                    "from_chat_id": chat_id,
                    "message_id": message["message_id"],
                },
            )
        elif text:
            send_message(customer_chat_id, f"Менеджер NihaoExpress:\n{text}")
        else:
            send_message(customer_chat_id, "Менеджер NihaoExpress прислал сообщение:")
            api_call(
                "copyMessage",
                {
                    "chat_id": customer_chat_id,
                    "from_chat_id": chat_id,
                    "message_id": message["message_id"],
                },
            )
    except TelegramError:
        LOG.exception("Could not relay manager reply for ticket %s", ticket)
        send_message(chat_id, "Не получилось доставить ответ клиенту. Можно попробовать написать ему лично.")
        return

    send_message(chat_id, f"Ответ по заявке {ticket} доставлен клиенту.")


def relay_customer_message(
    chat_id: int, user: dict[str, Any], message: dict[str, Any], ticket: str
) -> None:
    if not MANAGER_CHAT_ID:
        send_message(chat_id, "Чат менеджера пока не подключён. Напишите @bomj01 напрямую.")
        return

    username = f"@{user['username']}" if user.get("username") else display_name(user)
    text = (message.get("text") or message.get("caption") or "").strip()
    has_media = any(field in message for field in MEDIA_FIELDS)
    summary = f"Сообщение клиента по заявке {ticket} · {username}"
    if has_media:
        summary += "\n\nКлиент прислал материал:"
    elif text:
        summary += f"\n\n{text}"
    else:
        summary += "\n\nКлиент прислал сообщение:"
    try:
        send_message(MANAGER_CHAT_ID, summary)
        if has_media or not text:
            api_call(
                "copyMessage",
                {
                    "chat_id": MANAGER_CHAT_ID,
                    "from_chat_id": chat_id,
                    "message_id": message["message_id"],
                },
            )
    except TelegramError:
        LOG.exception("Could not relay customer message for ticket %s", ticket)
        send_message(chat_id, "Не получилось передать сообщение менеджеру. Попробуйте позже.")
        return

    send_message(chat_id, "Передал сообщение менеджеру NihaoExpress. Ответ придёт в этот чат.")


def manager_contact(chat_id: int) -> None:
    send_message(
        chat_id,
        "Менеджер NihaoExpress: @bomj01. Можно написать напрямую или оставить заявку через бота.",
        inline_keyboard=[[{"text": "Открыть чат с менеджером", "url": "https://t.me/bomj01"}]],
    )


def next_priority_prompt(chat_id: int) -> None:
    choices = ", ".join(SESSIONS[chat_id]["priorities"])
    text = "Принял" + (f": {choices}." if choices else ".")
    text += " Выберите ещё или нажмите «Дальше»."
    send_message(chat_id, text, keyboard=PRIORITY_KEYBOARD)


def add_attachment(session: dict[str, Any], message: dict[str, Any]) -> bool:
    if len(session["attachments"]) >= 5:
        return False
    session["attachments"].append({"message_id": message["message_id"]})
    caption = message.get("caption", "").strip()
    if caption:
        session["product_notes"].append(clipped(caption, 500))
    return True


def handle_message(message: dict[str, Any]) -> None:
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    user = message.get("from", {})
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        SESSIONS.pop(chat_id, None)
        if is_manager(user, chat_id):
            REPLY_TARGETS.pop(chat_id, None)
        main_menu(chat_id, user.get("first_name", ""), user)
        return
    if (
        is_manager(user, chat_id)
        and chat_id in REPLY_TARGETS
        and not text.startswith("/cancel")
    ):
        relay_manager_message(chat_id, message)
        return
    if text == MANAGER_SETUP_BUTTON:
        if not is_manager(user, chat_id):
            send_message(chat_id, "Эта настройка доступна только менеджеру NihaoExpress.")
            return
        global MANAGER_CHAT_ID
        MANAGER_CHAT_ID = str(chat_id)
        MANAGER_ID_FILE.write_text(MANAGER_CHAT_ID, encoding="utf-8")
        send_message(
            chat_id,
            "Этот чат подключён для заявок. Теперь бот будет отправлять их сюда автоматически.",
            keyboard=home_keyboard(user),
        )
        return
    if text.startswith("/help"):
        send_message(
            chat_id,
            "📝 Нажмите «Оставить заявку», выберите задачу и укажите товар, объём, "
            "город доставки и приоритеты. Фото и файлы можно приложить в ходе заявки.\n\n"
            "/cancel — отменить текущую заявку. После передачи заявки можно отправлять "
            "дополнительные сообщения менеджеру прямо в этот чат.",
            keyboard=home_keyboard(user),
        )
        return
    if text.startswith("/cancel") or text == "Отмена":
        if is_manager(user, chat_id) and chat_id in REPLY_TARGETS:
            ticket = REPLY_TARGETS.pop(chat_id)
            send_message(
                chat_id,
                f"Режим ответа по заявке {ticket} завершён.",
                keyboard=home_keyboard(user),
            )
            return
        if chat_id not in SESSIONS and ticket_for_customer(chat_id):
            send_message(
                chat_id,
                "Заявка уже передана менеджеру. Напишите сообщение, если хотите дополнить её.",
                keyboard=MAIN_KEYBOARD,
            )
            return
        SESSIONS.pop(chat_id, None)
        send_message(chat_id, "Заявку отменил. Если захотите продолжить, нажмите «Оставить заявку».", keyboard=home_keyboard(user))
        return
    if text == "Начать заново":
        begin_brief(chat_id)
        return
    if text == "📝 Оставить заявку":
        begin_brief(chat_id)
        return
    if text == "ℹ️ Что вы можете":
        send_message(
            chat_id,
            "NihaoExpress помогает найти фабрику, обсудить цену и параметры товара, "
            "сравнить образцы, проконтролировать заказ в Китае и подобрать доставку "
            "в Россию, страны СНГ и ОАЭ. Работаем напрямую с производителями.",
            keyboard=home_keyboard(user),
        )
        return
    if text == "👤 Менеджер":
        manager_contact(chat_id)
        return

    session = SESSIONS.get(chat_id)
    if not session:
        if is_manager(user, chat_id) and chat_id in REPLY_TARGETS:
            relay_manager_message(chat_id, message)
            return
        ticket = ticket_for_customer(chat_id)
        if ticket:
            relay_customer_message(chat_id, user, message, ticket)
            return
        send_message(chat_id, "Выберите, чем помочь:", keyboard=home_keyboard(user))
        return

    step = session["step"]
    if step == "service":
        if not text:
            send_message(chat_id, "Выберите вариант кнопкой ниже или напишите свой.", keyboard=SERVICE_KEYBOARD)
            return
        session["service"] = clipped(text, 160)
        ask_product(chat_id)
        return

    if step == "product":
        if text.casefold() == "дальше":
            ask_volume(chat_id)
            return
        if message.get("photo") or message.get("document"):
            if not add_attachment(session, message):
                send_message(chat_id, "Можно приложить не больше пяти файлов. Нажмите «Дальше», чтобы продолжить.", keyboard=PRODUCT_KEYBOARD)
                return
            send_message(chat_id, "✅ Материал добавлен. Можно прислать ещё или нажать «Дальше».", keyboard=PRODUCT_KEYBOARD)
            return
        if text:
            session["product_notes"].append(clipped(text, 900))
            send_message(chat_id, "Принял. Можно добавить фото или ещё детали, затем нажмите «Дальше».", keyboard=PRODUCT_KEYBOARD)
            return
        send_message(chat_id, "Пришлите фото, файл, ссылку или описание товара.", keyboard=PRODUCT_KEYBOARD)
        return

    if step == "volume":
        if not text:
            send_message(chat_id, "Напишите примерное количество или нажмите «Пока уточняю».", keyboard=VOLUME_KEYBOARD)
            return
        session["volume"] = "Пока уточняю" if text == "Пока уточняю" else clipped(text, 250)
        ask_destination(chat_id)
        return

    if step == "destination":
        if not text:
            send_message(chat_id, "Напишите страну и город доставки.")
            return
        session["destination"] = clipped(text, 250)
        ask_priorities(chat_id)
        return

    if step == "priorities":
        if text.casefold() == "дальше":
            review_brief(chat_id)
            return
        if not text:
            send_message(chat_id, "Выберите приоритет кнопкой или напишите его своими словами.", keyboard=PRIORITY_KEYBOARD)
            return
        if text not in session["priorities"]:
            session["priorities"].append(clipped(text, 120))
        next_priority_prompt(chat_id)
        return

    if step == "confirm":
        if text == "Отправить менеджеру":
            submit_brief(chat_id, user)
            return
        send_message(chat_id, "Нажмите «Отправить менеджеру» или «Начать заново».", keyboard=CONFIRM_KEYBOARD)


def handle_update(update: dict[str, Any]) -> None:
    callback_query = update.get("callback_query")
    if callback_query:
        try:
            handle_callback_query(callback_query)
        except TelegramError:
            LOG.exception("Could not handle callback query")
        return
    message = update.get("message")
    if message:
        handle_message(message)


# ==========================================
# ТОЧКА ВХОДА ДЛЯ YANDEX CLOUD FUNCTIONS
# ==========================================
def handler(event, context):
    """Функция, которую вызывает Яндекс Облако при каждом сообщении."""
    try:
        body = event.get('body')
        if not body:
            return {'statusCode': 200, 'body': 'ok'}
            
        update = json.loads(body)
        handle_update(update)
    except Exception as e:
        LOG.exception("Error handling update")
        
    return {'statusCode': 200, 'body': 'ok'}
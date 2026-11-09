"""
АкБарак — Telegram-бот для печати.

Студент отправляет боту файл (PDF, Word, TXT или фото) → бот считает страницы и цену →
студент выбирает ч/б или цвет, копии и терминал → оплачивает по ссылке GoPay →
бот пишет «Оплачено», «Печатаю», «Готово — забери на терминале».

Работает внутри server.py (webhook, без отдельного процесса).
Настройка: переменная TG_CLIENT_BOT_TOKEN на Railway (токен от @BotFather),
затем один раз открыть  /tg/setup?key=<ADMIN_KEY>  (или кнопка в админке).
"""

import hashlib
import logging
import os
import secrets
import threading
import uuid

import requests
from flask import jsonify, request

log = logging.getLogger("akbarak.tg")

TOKEN = "".join((os.getenv("TG_CLIENT_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN", "")).split())   # без пробелов/переносов
API = f"https://api.telegram.org/bot{TOKEN}"
SECRET = hashlib.sha256(("akbarak-tg:" + TOKEN).encode()).hexdigest()[:40]
MAX_TG_MB = 20                      # Telegram отдаёт ботам файлы до 20 МБ
DRAFT_HOURS = 6

H = {}   # заполняется в register(): помощники из server.py
LAST_ERROR = {"text": ""}


# ─────────────────────── Telegram API ───────────────────────

def tg(method, **data):
    if not TOKEN:
        return None
    try:
        r = requests.post(f"{API}/{method}", json=data, timeout=20)
        j = r.json()
        if not j.get("ok"):
            log.warning("TG %s: %s", method, j.get("description"))
            LAST_ERROR["text"] = f"{method}: {j.get('description')}"
        return j.get("result")
    except Exception as e:
        log.error("TG %s: %s", method, e)
        LAST_ERROR["text"] = f"{method}: {e}"
        return None


def send(chat_id, text, kb=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        data["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **data)


def edit(chat_id, msg_id, text, kb=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    data["reply_markup"] = {"inline_keyboard": kb or []}
    return tg("editMessageText", **data)


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def btn(text, data=None, url=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = data
    return b


# ─────────────────────── Тексты ───────────────────────

def price_line():
    P = H["PRICES"]
    s = f"ч/б {P['print_bw']} сом/стр"
    if H["COLOR_ENABLED"]:
        s += f", цвет {P['print_color']} сом/стр"
    return s


def hello():
    return ("👋 <b>АкБарак</b> — печать с телефона 24/7.\n\n"
            "Отправь сюда файл — <b>PDF, Word (.docx), TXT или фото</b>. "
            "Я посчитаю страницы и цену, ты оплатишь по ссылке, и лист выйдет из терминала.\n\n"
            f"💰 Цены: {price_line()}.\n"
            "📍 Терминал: КГМА.\n\n"
            "Команды: /orders — мои заказы, /help — помощь.")


HELP = ("<b>Как напечатать</b>\n"
        "1. Отправь файл сюда (скрепка 📎 → Файл). Можно несколько по очереди.\n"
        "2. Выбери ч/б или цвет и число копий.\n"
        "3. Нажми «Оплатить» и оплати в банке.\n"
        "4. Подойди к терминалу — лист выйдет сам. Я напишу, когда будет готово.\n\n"
        "Word старого формата (.doc) пересохрани в .docx или PDF.\n"
        "Вопросы: 0773 731 212 (WhatsApp).")


def draft_text(d):
    color = "цвет" if d["color"] else "ч/б"
    sides = {"single": "с одной стороны", "duplex": "с двух сторон", "booklet": "брошюра"}[d["sides"]]
    return (f"📄 <b>{esc(d['file_name'])}</b>\n"
            f"Страниц: <b>{d['pages']}</b>\n\n"
            f"Печать: {color}, {sides}\n"
            f"Копий: {d['copies']}\n"
            f"Терминал: {esc(d['terminal'])}\n\n"
            f"💰 Итого: <b>{draft_total(d)} сом</b>")


def draft_total(d):
    return H["order_total"]("print", "color" if d["color"] else "bw", d["sides"], d["pages"], d["copies"])


def draft_kb(d):
    i = d["id"]
    kb = []
    if H["COLOR_ENABLED"]:
        kb.append([btn(("✅ " if not d["color"] else "") + "Ч/Б", f"d:{i}:bw"),
                   btn(("✅ " if d["color"] else "") + "Цвет", f"d:{i}:color")])
    kb.append([btn("➖", f"d:{i}:minus"), btn(f"Копий: {d['copies']}", f"d:{i}:noop"), btn("➕", f"d:{i}:plus")])
    if H["DUPLEX_ENABLED"] and d["pages"] > 1:
        kb.append([btn(("✅ " if d["sides"] == k else "") + t, f"d:{i}:s_{k}")
                   for k, t in (("single", "1 сторона"), ("duplex", "2 стороны"), ("booklet", "Брошюра"))])
    terms = terminal_ids()
    if len(terms) > 1:
        kb.append([btn(f"📍 Терминал: {d['terminal']} (сменить)", f"d:{i}:term")])
    kb.append([btn(f"💳 Оплатить {draft_total(d)} сом", f"d:{i}:pay")])
    kb.append([btn("✖ Отменить", f"d:{i}:cancel")])
    return kb


# ─────────────────────── База ───────────────────────

def terminal_ids():
    conn = H["db"]()
    rows = [r["id"] for r in conn.execute("SELECT id FROM terminals ORDER BY id")]
    conn.close()
    default = H["DEFAULT_TERMINAL"]
    return [default] + [t for t in rows if t != default]


def get_draft(draft_id, chat_id):
    conn = H["db"]()
    row = conn.execute("SELECT * FROM tg_drafts WHERE id=? AND chat_id=?", (draft_id, str(chat_id))).fetchone()
    conn.close()
    return dict(row) if row else None


def save_draft(d):
    conn = H["db"]()
    conn.execute("UPDATE tg_drafts SET copies=?, color=?, sides=?, terminal=?, msg_id=?, order_id=? WHERE id=?",
                 (d["copies"], d["color"], d["sides"], d["terminal"], d.get("msg_id"), d.get("order_id"), d["id"]))
    conn.commit()
    conn.close()


# ─────────────────────── Приём файла ───────────────────────

def on_file(chat_id, file_id, file_name, size, is_photo=False):
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".doc":
        return send(chat_id, "Это старый формат Word (.doc) — я не могу точно посчитать страницы.\n"
                             "Открой файл и сохрани как <b>.docx</b> или <b>PDF</b>, потом пришли снова.")
    if ext not in (".pdf", ".docx", ".txt", ".jpg", ".jpeg", ".png"):
        return send(chat_id, f"Формат {esc(ext or 'без расширения')} не печатаю. Подойдут PDF, Word (.docx), TXT и фото.")
    if size and size > MAX_TG_MB * 1024 * 1024:
        return send(chat_id, f"Файл больше {MAX_TG_MB} МБ — Telegram не даёт мне его скачать.\n"
                             "Загрузи его на сайте: akbarak.vercel.app")
    wait = send(chat_id, "⏳ Получил файл, считаю страницы…")
    info = tg("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return send(chat_id, "Не получилось скачать файл. Попробуй ещё раз.")
    path = os.path.join(H["FILES_DIR"], f"{uuid.uuid4().hex}{ext}")
    try:
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        log.error("download: %s", e)
        return send(chat_id, "Не получилось скачать файл. Попробуй ещё раз.")
    if ext == ".pdf":
        pages = H["pdf_page_count"](path)
    elif ext == ".docx":
        pages = H["docx_page_count"](path)
    elif ext == ".txt":
        pages = H["txt_page_count"](path)
    else:
        pages = 1
    if not pages:
        H["remove"](path)
        return send(chat_id, "Файл не открывается — возможно, он повреждён или защищён паролем.")
    pages = max(1, min(H["MAX_PAGES"], pages))
    color = 1 if (is_photo or ext in (".jpg", ".jpeg", ".png")) and H["COLOR_ENABLED"] else 0
    conn = H["db"]()
    cur = conn.execute(
        "INSERT INTO tg_drafts (chat_id, file_path, file_name, pages, copies, color, sides, terminal, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (str(chat_id), path, file_name[:120], pages, 1, color, "single", H["DEFAULT_TERMINAL"], H["now"]()))
    conn.commit()
    d = dict(conn.execute("SELECT * FROM tg_drafts WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.close()
    if wait:
        tg("deleteMessage", chat_id=chat_id, message_id=wait["message_id"])
    m = send(chat_id, draft_text(d), draft_kb(d))
    if m:
        d["msg_id"] = m["message_id"]
        save_draft(d)


# ─────────────────────── Кнопки ───────────────────────

def on_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    msg_id = cq["message"]["message_id"]
    data = cq.get("data") or ""
    answer = ""
    try:
        kind, sid, action = data.split(":", 2)
        oid = int(sid)
    except ValueError:
        return tg("answerCallbackQuery", callback_query_id=cq["id"])

    if kind == "o":                                    # проверка оплаты
        conn = H["db"]()
        row = conn.execute("SELECT status, terminal_id FROM orders WHERE id=? AND tg_chat_id=?",
                           (oid, str(chat_id))).fetchone()
        conn.close()
        if not row:
            answer = "Заказ не найден"
        elif row["status"] == "pending_payment":
            answer = "Оплата ещё не пришла. Если оплатил — подожди минуту."
        elif row["status"] == "rejected":
            answer = "Оплата не прошла. Отправь файл заново."
        else:
            answer = "✅ Оплачено! " + H["STATUS_RU"].get(row["status"], row["status"])
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer, show_alert=True)

    d = get_draft(oid, chat_id)
    if not d or d.get("order_id"):
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text="Этот заказ уже оформлен или устарел")

    if action == "bw":
        d["color"] = 0
    elif action == "color" and H["COLOR_ENABLED"]:
        d["color"] = 1
    elif action == "plus":
        d["copies"] = min(20, d["copies"] + 1)
    elif action == "minus":
        d["copies"] = max(1, d["copies"] - 1)
    elif action.startswith("s_") and H["DUPLEX_ENABLED"] and action[2:] in H["SIDES"]:
        d["sides"] = action[2:]
    elif action == "term":
        terms = terminal_ids()
        i = terms.index(d["terminal"]) if d["terminal"] in terms else -1
        d["terminal"] = terms[(i + 1) % len(terms)]
    elif action == "cancel":
        H["remove"](d["file_path"])
        conn = H["db"]()
        conn.execute("DELETE FROM tg_drafts WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        edit(chat_id, msg_id, f"✖ Отменено: {esc(d['file_name'])}")
        return tg("answerCallbackQuery", callback_query_id=cq["id"])
    elif action == "pay":
        return pay(cq, d, chat_id, msg_id)
    elif action == "noop":
        return tg("answerCallbackQuery", callback_query_id=cq["id"])

    save_draft(d)
    edit(chat_id, msg_id, draft_text(d), draft_kb(d))
    tg("answerCallbackQuery", callback_query_id=cq["id"])


def pay(cq, d, chat_id, msg_id):
    total = draft_total(d)
    token = secrets.token_urlsafe(16)
    conn = H["db"]()
    cur = conn.execute(
        "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
        "group_id, terminal_id, order_type, access_token, sides, tg_chat_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["file_name"], d["file_path"], "color" if d["color"] else "bw", d["pages"], d["copies"], total, "gopay",
         "pending_payment", H["now"](), uuid.uuid4().hex, d["terminal"], "print", token, d["sides"], str(chat_id)))
    order_id = cur.lastrowid
    conn.execute("UPDATE tg_drafts SET order_id=? WHERE id=?", (order_id, d["id"]))
    conn.commit()
    conn.close()

    return_url = f"{H['SITE_URL']}/?order={order_id}&token={token}"
    payment = None
    try:
        payment = H["gopay"].create_payment(order_id, total, f"АкБарак заказ #{order_id}", return_url=return_url)
    except Exception as e:
        log.error("gopay: %s", e)
    if not payment or not payment.get("checkout_url"):
        conn = H["db"]()
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.execute("UPDATE tg_drafts SET order_id=NULL WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        return tg("answerCallbackQuery", callback_query_id=cq["id"],
                  text="Не удалось создать оплату. Попробуй через минуту.", show_alert=True)
    conn = H["db"]()
    conn.execute("UPDATE orders SET gopay_payment_id=? WHERE id=?", (payment.get("payment_id"), order_id))
    conn.commit()
    conn.close()
    edit(chat_id, msg_id,
         draft_text(d) + f"\n\n🧾 Заказ <b>#{order_id}</b>. Нажми «Оплатить» — откроется оплата.\n"
                         "После оплаты я сразу отправлю файл на терминал.",
         [[btn(f"💳 Оплатить {total} сом", url=payment["checkout_url"])],
          [btn("🔄 Я оплатил — проверить", f"o:{order_id}:check")]])
    tg("answerCallbackQuery", callback_query_id=cq["id"])


# ─────────────────────── Команды ───────────────────────

def my_orders(chat_id):
    conn = H["db"]()
    rows = conn.execute("SELECT id, file_name, total, status, terminal_id FROM orders WHERE tg_chat_id=? "
                        "ORDER BY id DESC LIMIT 8", (str(chat_id),)).fetchall()
    conn.close()
    if not rows:
        return send(chat_id, "Заказов пока нет. Отправь файл — и начнём 🙂")
    lines = [f"#{r['id']} · {esc(r['file_name'])} · {r['total']} сом · <b>{H['STATUS_RU'].get(r['status'], r['status'])}</b>"
             for r in rows]
    send(chat_id, "<b>Мои заказы</b>\n" + "\n".join(lines))


def on_update(u):
    if "callback_query" in u:
        return on_callback(u["callback_query"])
    m = u.get("message") or {}
    chat_id = (m.get("chat") or {}).get("id")
    if not chat_id or (m.get("chat") or {}).get("type") != "private":
        return
    if "document" in m:
        doc = m["document"]
        return on_file(chat_id, doc["file_id"], doc.get("file_name") or "файл", doc.get("file_size"))
    if "photo" in m:
        ph = m["photo"][-1]                             # самое большое
        return on_file(chat_id, ph["file_id"], f"фото_{m.get('message_id')}.jpg", ph.get("file_size"), True)
    text = (m.get("text") or "").strip()
    if text.startswith("/orders"):
        return my_orders(chat_id)
    if text.startswith("/help"):
        return send(chat_id, HELP)
    send(chat_id, hello())


# ─────────────────────── События заказа → сообщение студенту ───────────────────────

EVENT_TEXT = {
    "paid": "✅ Оплата получена! Заказ <b>#{id}</b> отправлен на терминал <b>{term}</b>.",
    "printing": "🖨 Печатаю заказ <b>#{id}</b>…",
    "printed": "📄 Готово! Заказ <b>#{id}</b> напечатан — забери лист на терминале <b>{term}</b>. Спасибо! 🙌",
    "failed": "⚠️ Не получилось напечатать заказ <b>#{id}</b>. Мы уже знаем: перепечатаем или вернём деньги.\n"
              "Вопросы: 0773 731 212.",
    "rejected": "Оплата заказа <b>#{id}</b> не прошла. Отправь файл заново, если хочешь напечатать.",
}


def order_event(order_ids, event):
    """Вызывается из server.py, когда заказ оплачен / печатается / напечатан / ошибка."""
    if not TOKEN or event not in EVENT_TEXT:
        return
    ids = list(order_ids) if isinstance(order_ids, (list, tuple, set)) else [order_ids]

    def _go():
        try:
            conn = H["db"]()
            q = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT id, tg_chat_id, terminal_id FROM orders WHERE id IN ({q}) "
                                "AND tg_chat_id IS NOT NULL", ids).fetchall()
            conn.close()
            for r in rows:
                send(r["tg_chat_id"], EVENT_TEXT[event].format(id=r["id"], term=esc(r["terminal_id"])))
        except Exception as e:
            log.error("order_event: %s", e)

    threading.Thread(target=_go, daemon=True).start()


def cleanup():
    """Старые черновики (файл прислали, но не оплатили) — удаляем вместе с файлом."""
    conn = H["db"]()
    old = conn.execute("SELECT id, file_path FROM tg_drafts WHERE order_id IS NULL AND created_at < ?",
                       (H["ago"](hours=DRAFT_HOURS),)).fetchall()
    for r in old:
        H["remove"](r["file_path"])
    conn.execute("DELETE FROM tg_drafts WHERE created_at < ?", (H["ago"](hours=DRAFT_HOURS),))
    conn.commit()
    conn.close()


# ─────────────────────── Регистрация в Flask ───────────────────────

def register(app, helpers):
    H.update(helpers)
    conn = H["db"]()
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id INTEGER, file_path TEXT, file_name TEXT,
        pages INTEGER, copies INTEGER DEFAULT 1, color INTEGER DEFAULT 0, sides TEXT DEFAULT 'single',
        terminal TEXT, created_at TEXT, order_id INTEGER)""")
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN tg_chat_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

    @app.route("/tg/webhook", methods=["POST"])
    def tg_webhook():
        if not TOKEN or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            return "", 403
        u = request.get_json(silent=True) or {}

        def _run():
            try:
                on_update(u)
            except Exception:
                log.exception("tg update")
        threading.Thread(target=_run, daemon=True).start()   # Telegram ждёт быстрый ответ
        return jsonify({"ok": True})

    @app.route("/tg/setup", methods=["GET", "POST"])
    def tg_setup():
        if not H["admin_ok"]():
            return jsonify({"error": "Нет доступа"}), 403
        if not TOKEN:
            return jsonify({"error": "На сервере нет токена бота: добавьте TG_CLIENT_BOT_TOKEN на Railway"}), 400
        base = os.getenv("SERVER_URL") or ("https://" + request.host)
        LAST_ERROR["text"] = ""
        me = tg("getMe")
        if not me:
            hint = LAST_ERROR["text"]
            if "Unauthorized" in hint or "Not Found" in hint:
                hint = "Telegram не принял токен. Проверьте TG_CLIENT_BOT_TOKEN на Railway: без пробелов и переносов строк, целиком."
            return jsonify({"success": False, "error": hint or "Telegram не отвечает"}), 400
        ok = tg("setWebhook", url=base.rstrip("/") + "/tg/webhook", secret_token=SECRET,
                allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        tg("setMyCommands", commands=[{"command": "start", "description": "Как печатать"},
                                      {"command": "orders", "description": "Мои заказы"},
                                      {"command": "help", "description": "Помощь"}])
        tg("setMyDescription", description="Печать с телефона на терминалах АкБарак. "
                                           "Отправь файл — посчитаю цену, оплатишь, и лист выйдет из терминала.")
        tg("setMyShortDescription", short_description="Печать с телефона 24/7 · терминалы АкБарак")
        if not ok:
            return jsonify({"success": False, "bot": me.get("username"), "error": LAST_ERROR["text"] or "setWebhook не прошёл"}), 400
        return jsonify({"success": True, "bot": me.get("username"), "webhook": base.rstrip("/") + "/tg/webhook"})

    @app.route("/tg/status", methods=["GET"])
    def tg_status():
        if not H["admin_ok"]():
            return jsonify({"error": "Нет доступа"}), 403
        if not TOKEN:
            return jsonify({"configured": False})
        LAST_ERROR["text"] = ""
        me = tg("getMe") or {}
        info = tg("getWebhookInfo") or {}
        return jsonify({"configured": True, "bot": me.get("username"), "token_error": "" if me else LAST_ERROR["text"], "webhook": bool(info.get("url")),
                        "pending": info.get("pending_update_count", 0), "last_error": info.get("last_error_message")})

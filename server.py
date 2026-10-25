"""
АкБарак — сервер заказов (Flask, Railway).

Переменные окружения на Railway:
    ADMIN_KEY          пароль админки (обязательно; без него админка выключена)
    WORKER_KEY         секрет для терминала (print_worker). Пока не задан — терминал
                       работает без ключа, как раньше (переходный режим).
    DATA_DIR           папка для базы и файлов. На Railway подключите Volume
                       и укажите путь, например /data — иначе заказы стираются
                       при каждом обновлении кода.
    TG_BOT_TOKEN       токен Telegram-бота для уведомлений (необязательно)
    TG_CHAT_ID         ваш chat_id в Telegram (необязательно)
    COLOR_ENABLED      "1" — разрешить цветную печать и фото (по умолчанию выключено:
                       Pantum M6500W печатает только ч/б)
    CASH_ENABLED       "1" — разрешить оплату наличными (по умолчанию выключено:
                       купюроприёмника нет, иначе печать бесплатная)
    FILE_TTL_HOURS     через сколько часов удалять файлы (по умолчанию 24)
    DEFAULT_TERMINAL   id терминала по умолчанию (по умолчанию kgma-1)
    GOPAY_*            см. gopay.py
"""

import io
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import gopay
import doc_templates

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("akbarak")

app = Flask(__name__)
CORS(app)

# ─────────────────────── Настройки ───────────────────────

DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "orders.db")
FILES_DIR = os.path.join(DATA_DIR, "order_files")
os.makedirs(FILES_DIR, exist_ok=True)

ADMIN_KEY = os.getenv("ADMIN_KEY", "")
WORKER_KEY = os.getenv("WORKER_KEY", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
CASH_ENABLED = os.getenv("CASH_ENABLED", "0") == "1"
COLOR_ENABLED = os.getenv("COLOR_ENABLED", "0") == "1"
NO_COLOR_MSG = "Цветная печать и фото пока недоступны — сейчас только ч/б"
FILE_TTL_HOURS = int(os.getenv("FILE_TTL_HOURS", "24"))
DEFAULT_TERMINAL = os.getenv("DEFAULT_TERMINAL", "kgma-1")
SITE_URL = os.getenv("SITE_URL", "https://akbarak.vercel.app").rstrip("/")

OFFLINE_ALERT_MIN = 5      # терминал молчит дольше — уведомление
STUCK_ALERT_MIN = 10       # оплаченный заказ не напечатан дольше — уведомление
MAX_UPLOAD_MB = 25
MAX_PAGES = 500
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".txt", ".jpg", ".jpeg", ".png"}

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Цены считает ТОЛЬКО сервер. То, что присылает браузер, игнорируется.
PRICES = {
    "print_bw": 5, "print_color": 10,
    "copy_bw": 6, "copy_color": 12,
    "scan_bw": 6, "scan_color": 12,
}
PHOTO_PRICES = [  # (подстрока в названии формата, цена)
    ("паспорт", 50),
    ("10×15", 15), ("10x15", 15),
    ("15×21", 25), ("15x21", 25),
]

ONLINE = ("gopay", "онлайн", "online")
TIME_FMT = "%Y-%m-%d %H:%M:%S"

STATUS_RU = {
    "pending_payment": "Ждём оплату", "new": "В очереди", "printing": "Печатается",
    "printed": "Готово", "scan_ready": "Скан готов", "failed": "Ошибка печати",
    "cancelled": "Отменён", "rejected": "Не оплачен",
}


def now():
    return datetime.now().strftime(TIME_FMT)


def ago(minutes=0, hours=0):
    return (datetime.now() - timedelta(minutes=minutes, hours=hours)).strftime(TIME_FMT)


def is_online_payment(payment: str) -> bool:
    return (payment or "").strip().lower() in ONLINE


# ─────────────────────── База данных ───────────────────────

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            file_path TEXT,
            print_type TEXT DEFAULT 'bw',
            pages INTEGER DEFAULT 1,
            copies INTEGER DEFAULT 1,
            total INTEGER DEFAULT 0,
            payment TEXT,
            page_range TEXT DEFAULT NULL,
            scan_data BLOB,
            status TEXT DEFAULT 'new',
            gopay_payment_id TEXT,
            created_at TEXT,
            paid_at TEXT
        )
    """)
    # Новые колонки — безопасно добавляются к уже существующей базе
    for stmt in [
        "ALTER TABLE orders ADD COLUMN scan_data BLOB",
        "ALTER TABLE orders ADD COLUMN page_range TEXT DEFAULT NULL",
        "ALTER TABLE orders ADD COLUMN gopay_payment_id TEXT",
        "ALTER TABLE orders ADD COLUMN paid_at TEXT",
        "ALTER TABLE orders ADD COLUMN group_id TEXT",
        "ALTER TABLE orders ADD COLUMN terminal_id TEXT",
        "ALTER TABLE orders ADD COLUMN order_type TEXT DEFAULT 'print'",
        "ALTER TABLE orders ADD COLUMN photo_format TEXT",
        "ALTER TABLE orders ADD COLUMN error TEXT",
        "ALTER TABLE orders ADD COLUMN printed_at TEXT",
        "ALTER TABLE orders ADD COLUMN refund TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN access_token TEXT",
        "ALTER TABLE orders ADD COLUMN alerted INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kiosk_sessions (
            id TEXT PRIMARY KEY,
            terminal_id TEXT,
            created_at TEXT,
            order_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terminals (
            id TEXT PRIMARY KEY,
            last_seen TEXT,
            printer_status TEXT,
            offline_alerted INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


init_db()

# Колонки, которые отдаём терминалу (без больших/секретных полей)
PUBLIC_COLS = ("id, file_name, print_type, pages, copies, total, payment, page_range, status, "
               "created_at, paid_at, group_id, terminal_id, order_type, photo_format, error, refund")


# ─────────────────────── Telegram-уведомления ───────────────────────

def notify(text: str):
    """Отправляет сообщение владельцу в Telegram (в фоне, не тормозит ответ)."""
    log.info("NOTIFY: %s", text)
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return

    def _send():
        try:
            requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
        except Exception as e:
            log.error("Telegram: %s", e)

    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────── Доступ ───────────────────────

def _given_key():
    return request.headers.get("X-Worker-Key") or request.headers.get("X-Admin-Key") or request.args.get("key", "")


def require_worker(f):
    """Только терминал (WORKER_KEY) или админ. Пока WORKER_KEY не задан — пускаем всех (переход)."""
    @wraps(f)
    def wrapper(*a, **kw):
        if WORKER_KEY:
            key = _given_key()
            if not key or key not in (WORKER_KEY, ADMIN_KEY):
                return jsonify({"error": "Нет доступа"}), 403
        return f(*a, **kw)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not ADMIN_KEY or _given_key() != ADMIN_KEY:
            return jsonify({"error": "Нет доступа"}), 403
        return f(*a, **kw)
    return wrapper


# ─────────────────────── Подсчёт страниц и цены ───────────────────────

def parse_range(text: str, max_page=None):
    """'1-3,5' -> [1,2,3,5]. Возвращает None, если формат неверный."""
    pages = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                if a < 1 or b < a:
                    return None
                pages.update(range(a, b + 1))
            else:
                n = int(part)
                if n < 1:
                    return None
                pages.add(n)
        except ValueError:
            return None
    if max_page:
        pages = {p for p in pages if p <= max_page}
    return sorted(pages) or None


def pdf_page_count(path: str):
    if not PdfReader:
        return None
    try:
        return len(PdfReader(path).pages)
    except Exception:
        return None


def clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def photo_price(fmt: str):
    f = (fmt or "").lower()
    for key, price in PHOTO_PRICES:
        if key.lower() in f:
            return price
    return None


# ─────────────────────── Публичные маршруты (сайт) ───────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "АкБарак сервер работает!"})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": f"Файл слишком большой (максимум {MAX_UPLOAD_MB} МБ)"}), 413


def check_payment(payment):
    if is_online_payment(payment):
        return None
    if CASH_ENABLED:
        return None
    return "Сейчас доступна только онлайн-оплата"


@app.route("/upload", methods=["POST"])
def upload():
    """Печать документов и фото. Цена и страницы считаются здесь, а не в браузере."""
    try:
        files = [f for f in request.files.getlist("file") if f and f.filename]
        if not files:
            return jsonify({"error": "Файл не найден"}), 400

        payment = request.form.get("payment", "")
        err = check_payment(payment)
        if err:
            return jsonify({"error": err}), 400

        photo_format = (request.form.get("photo_format") or "").strip()
        terminal = (request.form.get("terminal") or DEFAULT_TERMINAL)[:40]
        session_id = (request.form.get("session") or "").strip()[:64]
        if session_id:
            sess = _kiosk_session(session_id)
            if not sess:
                return jsonify({"error": "Код на экране устарел. Отсканируйте QR на экране терминала ещё раз."}), 400
            terminal = sess["terminal_id"]
        copies = clamp(request.form.get("copies"), 1, 50, 1)
        client_pages = clamp(request.form.get("pages"), 1, MAX_PAGES, 1)
        page_range = (request.form.get("page_range") or "").strip() or None

        if (photo_format or request.form.get("print_type") == "color") and not COLOR_ENABLED:
            return jsonify({"error": NO_COLOR_MSG}), 400
        if photo_format:
            unit = photo_price(photo_format)
            if unit is None:
                return jsonify({"error": "Неизвестный формат фото"}), 400
            print_type, order_type, copies = "color", "photo", 1
        else:
            print_type = "color" if request.form.get("print_type") == "color" else "bw"
            order_type = "print"
            unit = PRICES[f"print_{print_type}"]

        group_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(16)  # один ключ на весь заказ — для страницы статуса
        status = "pending_payment" if is_online_payment(payment) else "new"
        order_ids, grand_total, saved = [], 0, []

        conn = db()
        try:
            for file in files:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in ALLOWED_EXT:
                    return jsonify({"error": f"Формат {ext or '?'} не поддерживается"}), 400
                # Имя файла на диске — случайное (защита от ../ и подбора)
                file_path = os.path.join(FILES_DIR, f"{uuid.uuid4().hex}{ext}")
                file.save(file_path)
                saved.append(file_path)

                if order_type == "photo":
                    pages, rng = 1, None
                else:
                    real = pdf_page_count(file_path) if ext == ".pdf" else None
                    rng = page_range if len(files) == 1 else None
                    if rng:
                        selected = parse_range(rng, real)
                        if not selected:
                            raise ValueError("Неверный диапазон страниц. Пример: 1-5 или 1,3,5")
                        pages = len(selected)
                    else:
                        pages = real or client_pages
                    pages = max(1, min(MAX_PAGES, pages))

                total = (unit if order_type == "photo" else unit * pages * copies)
                grand_total += total
                cur = conn.execute(
                    "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, "
                    "page_range, status, created_at, group_id, terminal_id, order_type, photo_format, access_token) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (file.filename, file_path, print_type, pages, copies, total, payment, rng, status,
                     now(), group_id, terminal, order_type, photo_format or None, token))
                order_ids.append(cur.lastrowid)
            if session_id:
                conn.execute("UPDATE kiosk_sessions SET order_id=? WHERE id=?", (order_ids[0], session_id))
            conn.commit()
        except ValueError as e:
            conn.rollback()
            for p in saved:
                _remove(p)
            return jsonify({"error": str(e)}), 400
        finally:
            conn.close()

        return jsonify({"success": True, "order_ids": order_ids, "order_id": order_ids[0],
                        "group_id": group_id, "total": grand_total, "token": token})
    except Exception as e:
        log.exception("upload")
        return jsonify({"error": str(e)}), 500


# ─────────────────────── Экран терминала: заказ через экран ───────────────────────
# Экран создаёт сессию и показывает QR. Студент сканирует его, выбирает файл в телефоне,
# и заказ появляется на экране: там выбирают число копий и оплачивают по QR.

KIOSK_SESSION_MIN = 30


def _kiosk_session(session_id):
    conn = db()
    row = conn.execute("SELECT * FROM kiosk_sessions WHERE id=? AND created_at >= ?",
                       (session_id, ago(minutes=KIOSK_SESSION_MIN))).fetchone()
    conn.close()
    return row


def _group_by_token(order_id, token):
    conn = db()
    row = conn.execute("SELECT group_id, access_token FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or not row["access_token"] or not secrets.compare_digest(token or "", row["access_token"]):
        conn.close()
        return None, None
    return conn, row["group_id"]


@app.route("/kiosk/session", methods=["POST"])
def kiosk_session_new():
    data = request.get_json(silent=True) or {}
    terminal = (data.get("terminal") or DEFAULT_TERMINAL)[:40]
    sid = secrets.token_urlsafe(12)
    conn = db()
    conn.execute("DELETE FROM kiosk_sessions WHERE created_at < ?", (ago(hours=24),))
    conn.execute("INSERT INTO kiosk_sessions (id, terminal_id, created_at) VALUES (?,?,?)", (sid, terminal, now()))
    conn.commit()
    conn.close()
    return jsonify({"session": sid, "expires_min": KIOSK_SESSION_MIN})


@app.route("/kiosk/session/<session_id>", methods=["GET"])
def kiosk_session_get(session_id):
    sess = _kiosk_session(session_id[:64])
    if not sess:
        return jsonify({"state": "expired"})
    if not sess["order_id"]:
        return jsonify({"state": "waiting"})
    conn = db()
    first = conn.execute("SELECT group_id, access_token FROM orders WHERE id=?", (sess["order_id"],)).fetchone()
    items = conn.execute("SELECT id, file_name, pages, copies, total, status, print_type FROM orders "
                         "WHERE group_id=? ORDER BY id", (first["group_id"],)).fetchall()
    conn.close()
    return jsonify({
        "state": "uploaded",
        "order_id": sess["order_id"],
        "token": first["access_token"],
        "files": [{"name": i["file_name"], "pages": i["pages"]} for i in items],
        "copies": items[0]["copies"] if items else 1,
        "unit": PRICES["print_" + (items[0]["print_type"] if items else "bw")],
        "total": sum(i["total"] for i in items),
    })


@app.route("/kiosk/options/<int:order_id>", methods=["POST"])
def kiosk_options(order_id):
    """Изменить число копий на экране — только пока заказ не оплачен."""
    data = request.get_json(silent=True) or {}
    conn, group_id = _group_by_token(order_id, data.get("token"))
    if not conn:
        return jsonify({"error": "Заказ не найден"}), 404
    try:
        copies = clamp(data.get("copies"), 1, 50, 1)
        items = conn.execute("SELECT id, pages, print_type, order_type FROM orders "
                             "WHERE group_id=? AND status='pending_payment'", (group_id,)).fetchall()
        if not items:
            return jsonify({"error": "Заказ уже оплачен"}), 409
        total = 0
        for i in items:
            if i["order_type"] == "photo":
                continue
            unit = PRICES[f"{i['order_type'] if i['order_type'] in ('copy', 'scan') else 'print'}_{i['print_type']}"]
            item_total = unit * (i["pages"] or 1) * copies
            total += item_total
            conn.execute("UPDATE orders SET copies=?, total=? WHERE id=?", (copies, item_total, i["id"]))
        conn.commit()
        return jsonify({"success": True, "copies": copies, "total": total})
    finally:
        conn.close()


@app.route("/kiosk/cancel/<int:order_id>", methods=["POST"])
def kiosk_cancel(order_id):
    """Студент передумал на экране — отменяем неоплаченный заказ и удаляем файлы."""
    data = request.get_json(silent=True) or {}
    conn, group_id = _group_by_token(order_id, data.get("token"))
    if not conn:
        return jsonify({"error": "Заказ не найден"}), 404
    try:
        rows = conn.execute("SELECT id, file_path FROM orders WHERE group_id=? AND status='pending_payment'",
                            (group_id,)).fetchall()
        for r in rows:
            _remove(r["file_path"])
            conn.execute("UPDATE orders SET status='cancelled', file_path=NULL WHERE id=?", (r["id"],))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


# ─────────────────────── Шаблоны документов ───────────────────────

@app.route("/template_order", methods=["POST"])
def template_order():
    """Заявление/объяснительная/титульный лист: собираем DOCX по шаблону и создаём заказ."""
    try:
        data = request.get_json(silent=True) or {}
        payment = data.get("payment", "gopay")
        err = check_payment(payment)
        if err:
            return jsonify({"error": err}), 400
        copies = clamp(data.get("copies"), 1, 10, 1)
        terminal = (data.get("terminal") or DEFAULT_TERMINAL)[:40]
        file_path = os.path.join(FILES_DIR, f"{uuid.uuid4().hex}.docx")
        try:
            title = doc_templates.build_docx(str(data.get("template", "")), data.get("values") or {}, file_path)
        except doc_templates.TemplateError as e:
            return jsonify({"error": str(e)}), 400

        total = PRICES["print_bw"] * copies
        token = secrets.token_urlsafe(16)
        status = "pending_payment" if is_online_payment(payment) else "new"
        conn = db()
        cur = conn.execute(
            "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, "
            "created_at, group_id, terminal_id, order_type, access_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{title}.docx", file_path, "bw", 1, copies, total, payment, status, now(),
             uuid.uuid4().hex, terminal, "print", token))
        order_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"success": True, "order_id": order_id, "token": token, "total": total})
    except Exception as e:
        log.exception("template_order")
        return jsonify({"error": "Не удалось собрать документ"}), 500


@app.route("/order", methods=["POST"])
def order():
    """Копия и скан (без файла)."""
    try:
        data = request.get_json(silent=True) or {}
        order_type = data.get("type", "")
        if order_type not in ("copy", "scan"):
            return jsonify({"error": "Неизвестный тип заказа"}), 400
        payment = data.get("payment", "")
        err = check_payment(payment)
        if err:
            return jsonify({"error": err}), 400

        color = bool(data.get("color")) or clamp(data.get("price"), 0, 1000, 0) >= PRICES[f"{order_type}_color"]
        if color and not COLOR_ENABLED:
            return jsonify({"error": NO_COLOR_MSG}), 400
        print_type = "color" if color else "bw"
        pages = 1  # сканер берёт со стекла один лист за раз
        copies = clamp(data.get("copies"), 1, 20, 1) if order_type == "copy" else 1
        total = PRICES[f"{order_type}_{print_type}"] * pages * copies
        token = secrets.token_urlsafe(16)
        status = "pending_payment" if is_online_payment(payment) else "new"

        conn = db()
        cur = conn.execute(
            "INSERT INTO orders (file_name, print_type, pages, copies, total, payment, status, created_at, "
            "group_id, terminal_id, order_type, access_token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_type, print_type, pages, copies, total, payment, status, now(), uuid.uuid4().hex,
             (data.get("terminal") or DEFAULT_TERMINAL)[:40], order_type, token))
        order_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"success": True, "order_id": order_id, "total": total, "token": token})
    except Exception as e:
        log.exception("order")
        return jsonify({"error": str(e)}), 500


# ─────────────────────── GoPay ───────────────────────

@app.route("/create_payment/<int:order_id>", methods=["POST"])
def create_payment(order_id):
    """Один платёж на весь заказ (все файлы из одной загрузки)."""
    try:
        conn = db()
        row = conn.execute("SELECT group_id FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Заказ не найден"}), 404
        group_id = row["group_id"]
        if group_id:
            rows = conn.execute("SELECT id, total FROM orders WHERE group_id=? AND status='pending_payment'",
                                (group_id,)).fetchall()
        else:
            rows = conn.execute("SELECT id, total FROM orders WHERE id=? AND status='pending_payment'",
                                (order_id,)).fetchall()
        if not rows:
            conn.close()
            return jsonify({"error": "Заказ уже оплачен или отменён"}), 409

        amount = sum(r["total"] for r in rows)
        tok = conn.execute("SELECT access_token FROM orders WHERE id=?", (order_id,)).fetchone()
        return_url = f"{SITE_URL}/?order={order_id}&token={tok['access_token']}" if tok and tok["access_token"] else SITE_URL
        payment = gopay.create_payment(order_id, amount, f"АкБарак заказ #{order_id}", return_url=return_url)
        if not payment or not payment.get("payment_id"):
            conn.close()
            return jsonify({"error": "Не удалось создать платёж в GoPay"}), 502

        conn.executemany("UPDATE orders SET gopay_payment_id=? WHERE id=?",
                         [(payment["payment_id"], r["id"]) for r in rows])
        conn.commit()
        conn.close()
        return jsonify({"success": True, "amount": amount,
                        "checkout_url": payment.get("checkout_url"), "qr_url": payment.get("qr_url")})
    except Exception as e:
        log.exception("create_payment")
        return jsonify({"error": str(e)}), 500


def _amount_matches(paid, expected):
    """GoPay может прислать сумму в сомах или тыйынах — принимаем оба варианта."""
    if paid is None:
        return True
    try:
        paid = float(paid)
    except (TypeError, ValueError):
        return True
    return abs(paid - expected) < 0.01 or abs(paid - expected * 100) < 0.5


@app.route("/gopay_webhook", methods=["POST"])
def gopay_webhook():
    try:
        raw_body = request.get_data(as_text=True)
        if not gopay.verify_webhook(request.headers.get("GoPay-Nonce", ""), raw_body,
                                    request.headers.get("GoPay-Signature", "")):
            return jsonify({"error": "Неверная подпись"}), 403

        payload = request.get_json(silent=True) or {}
        if payload.get("event") == "test.ping":
            return jsonify({"success": True, "message": "pong"}), 200

        data = payload.get("data", payload)
        payment_id = data.get("payment_id")
        status = (data.get("status") or "").lower()
        if not payment_id:
            return jsonify({"error": "payment_id не найден"}), 400

        conn = db()
        rows = conn.execute("SELECT id, status, total FROM orders WHERE gopay_payment_id=?",
                            (payment_id,)).fetchall()
        if not rows:
            conn.close()
            return jsonify({"error": "Заказ для этого платежа не найден"}), 404

        ids = [r["id"] for r in rows]
        expected = sum(r["total"] for r in rows)

        if status in ("paid", "committed"):
            if not _amount_matches(data.get("amount"), expected):
                conn.close()
                notify(f"⚠️ Сумма оплаты не совпадает! Заказы {ids}: ждали {expected} сом, "
                       f"пришло {data.get('amount')}. Печать не запущена, проверьте вручную.")
                return jsonify({"success": True})
            to_queue = [r["id"] for r in rows if r["status"] in ("pending_payment", "rejected")]
            if to_queue:
                conn.executemany("UPDATE orders SET status='new', paid_at=? WHERE id=?",
                                 [(now(), i) for i in to_queue])
                conn.commit()
        elif status in ("failed", "cancelled", "expired"):
            conn.executemany("UPDATE orders SET status='rejected' WHERE id=? AND status='pending_payment'",
                             [(i,) for i in ids])
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        log.exception("webhook")
        return jsonify({"error": str(e)}), 500


@app.route("/payment_status/<int:order_id>", methods=["GET"])
def payment_status(order_id):
    conn = db()
    row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Заказ не найден"}), 404
    return jsonify({"status": row["status"], "paid": row["status"] not in ("pending_payment", "rejected")})


# ─────────────────────── Терминал (print_worker) ───────────────────────

def touch_terminal(terminal_id, printer_status=None):
    conn = db()
    old = conn.execute("SELECT offline_alerted FROM terminals WHERE id=?", (terminal_id,)).fetchone()
    if old and old["offline_alerted"]:
        notify(f"✅ Терминал {terminal_id} снова на связи")
    conn.execute(
        "INSERT INTO terminals (id, last_seen, printer_status, offline_alerted) VALUES (?,?,?,0) "
        "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, offline_alerted=0, "
        "printer_status=COALESCE(excluded.printer_status, terminals.printer_status)",
        (terminal_id, now(), printer_status))
    conn.commit()
    conn.close()


@app.route("/pending", methods=["GET"])
@require_worker
def pending():
    """Очередь на печать. Каждый опрос = «терминал жив»."""
    terminal = request.args.get("terminal") or DEFAULT_TERMINAL
    touch_terminal(terminal)
    conn = db()
    rows = conn.execute(
        f"SELECT {PUBLIC_COLS} FROM orders WHERE status='new' "
        "AND (terminal_id=? OR terminal_id IS NULL) ORDER BY id", (terminal,)).fetchall()
    conn.close()
    return jsonify({"orders": [dict(r) for r in rows]})


@app.route("/file/<int:order_id>", methods=["GET"])
@require_worker
def get_file(order_id):
    conn = db()
    row = conn.execute("SELECT file_path, file_name, status FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if row and row["status"] in ("new", "printing") and row["file_path"] and os.path.exists(row["file_path"]):
        return send_file(row["file_path"], as_attachment=True, download_name=row["file_name"])
    return jsonify({"error": "Файл не найден"}), 404


@app.route("/printing/<int:order_id>", methods=["POST"])
@require_worker
def printing(order_id):
    _set_status(order_id, "printing", only_from=("new",))
    return jsonify({"success": True})


@app.route("/done/<int:order_id>", methods=["POST"])
@require_worker
def done(order_id):
    conn = db()
    row = conn.execute("SELECT file_path, status FROM orders WHERE id=?", (order_id,)).fetchone()
    if row and row["status"] in ("new", "printing"):
        conn.execute("UPDATE orders SET status='printed', printed_at=?, file_path=NULL WHERE id=?",
                     (now(), order_id))
        conn.commit()
        _remove(row["file_path"])
    conn.close()
    return jsonify({"success": True})


@app.route("/failed/<int:order_id>", methods=["POST"])
@require_worker
def failed(order_id):
    """Терминал не смог напечатать: деньги нужно вернуть или перепечатать."""
    reason = ((request.get_json(silent=True) or {}).get("error") or request.form.get("error") or "")[:300]
    conn = db()
    row = conn.execute("SELECT status, payment, total, file_name FROM orders WHERE id=?", (order_id,)).fetchone()
    if row and row["status"] in ("new", "printing"):
        refund = "needed" if is_online_payment(row["payment"]) else ""
        conn.execute("UPDATE orders SET status='failed', error=?, refund=? WHERE id=?",
                     (reason, refund, order_id))
        conn.commit()
        notify(f"❌ Ошибка печати, заказ #{order_id} ({row['file_name']}, {row['total']} сом).\n"
               f"Причина: {reason or 'не указана'}\n"
               "Откройте админку: перепечатать или вернуть деньги.")
    conn.close()
    return jsonify({"success": True})


@app.route("/terminal_info/<terminal_id>", methods=["GET"])
def terminal_info(terminal_id):
    """Открытая информация для экрана терминала: на связи ли принтер и сколько напечатано сегодня."""
    offset = timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "6")))  # Бишкек UTC+6
    local_now = datetime.now() + offset
    day_start = (local_now.replace(hour=0, minute=0, second=0, microsecond=0) - offset).strftime(TIME_FMT)
    conn = db()
    try:
        term = conn.execute("SELECT last_seen, printer_status FROM terminals WHERE id=?",
                            (terminal_id[:40],)).fetchone()
        row = conn.execute(
            "SELECT COALESCE(SUM(pages * copies), 0) AS sheets, COUNT(*) AS orders FROM orders "
            "WHERE terminal_id=? AND status IN ('done','scan_ready') AND created_at >= ?",
            (terminal_id[:40], day_start)).fetchone()
    finally:
        conn.close()
    online = bool(term and term["last_seen"] and term["last_seen"] >= ago(minutes=2))
    printer = (term["printer_status"] if term else None) or "ok"
    return jsonify({"online": online, "printer_ok": online and printer == "ok",
                    "printer": printer if online else "терминал не на связи",
                    "sheets_today": row["sheets"], "orders_today": row["orders"]})


@app.route("/heartbeat", methods=["POST"])
@require_worker
def heartbeat():
    data = request.get_json(silent=True) or {}
    terminal = (data.get("terminal") or DEFAULT_TERMINAL)[:40]
    printer = (data.get("printer_status") or "")[:200] or None
    conn = db()
    prev = conn.execute("SELECT printer_status FROM terminals WHERE id=?", (terminal,)).fetchone()
    conn.close()
    touch_terminal(terminal, printer)
    if printer and printer != "ok" and (not prev or prev["printer_status"] != printer):
        notify(f"🖨 Терминал {terminal}: {printer}")
    return jsonify({"success": True})


@app.route("/scan_upload/<int:order_id>", methods=["POST"])
@require_worker
def scan_upload(order_id):
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "Файл не найден"}), 400
    conn = db()
    conn.execute("UPDATE orders SET status='scan_ready', scan_data=? WHERE id=?", (file.read(), order_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/order_status/<int:order_id>", methods=["GET"])
def order_status(order_id):
    """Статус заказа для страницы «Мой заказ». Доступ — только по ключу заказа."""
    token = request.args.get("token", "")
    conn = db()
    try:
        row = conn.execute("SELECT group_id, access_token, terminal_id FROM orders WHERE id=?",
                           (order_id,)).fetchone()
        if not row or not row["access_token"] or not secrets.compare_digest(token, row["access_token"]):
            return jsonify({"error": "Заказ не найден"}), 404
        items = conn.execute(
            "SELECT id, order_type, status, pages, copies, total, error, refund, "
            "(scan_data IS NOT NULL) AS has_scan FROM orders WHERE group_id=? ORDER BY id",
            (row["group_id"],)).fetchall()
        term = conn.execute("SELECT last_seen, printer_status FROM terminals WHERE id=?",
                            (row["terminal_id"],)).fetchone()
    finally:
        conn.close()

    statuses = [i["status"] for i in items]
    if any(s == "pending_payment" for s in statuses):
        overall = "pending_payment"
    elif any(s == "cancelled" for s in statuses):
        overall = "cancelled"
    elif any(s == "failed" for s in statuses):
        overall = "failed"
    elif all(s in ("done", "scan_ready") for s in statuses):
        overall = "done"
    elif any(s == "printing" for s in statuses):
        overall = "printing"
    else:
        overall = "paid"

    printer = None
    if term:
        printer = term["printer_status"] if term["printer_status"] not in (None, "ok") else "ok"
    return jsonify({
        "order_id": order_id,
        "status": overall,
        "type": items[0]["order_type"] if items else "print",
        "total": sum(i["total"] for i in items),
        "files": len(items),
        "scan_ready": any(i["has_scan"] for i in items),
        "refund": next((i["refund"] for i in items if i["refund"]), ""),
        "error": next((i["error"] for i in items if i["error"]), None),
        "printer": printer,
    })


def _scan_row(order_id):
    conn = db()
    row = conn.execute("SELECT status, scan_data, access_token FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    token = request.args.get("token", "")
    if not row or not row["access_token"] or not secrets.compare_digest(token, row["access_token"]):
        return None
    return row


@app.route("/scan_status/<int:order_id>", methods=["GET"])
def scan_status(order_id):
    row = _scan_row(order_id)
    if not row:
        return jsonify({"error": "Заказ не найден"}), 404
    return jsonify({"status": row["status"], "ready": row["status"] == "scan_ready"})


@app.route("/scan_download/<int:order_id>", methods=["GET"])
def scan_download(order_id):
    row = _scan_row(order_id)
    if row and row["scan_data"]:
        return send_file(io.BytesIO(row["scan_data"]), mimetype="image/jpeg",
                         as_attachment=True, download_name=f"scan_{order_id}.jpg")
    return jsonify({"error": "Файл не найден"}), 404


# ─────────────────────── Админка ───────────────────────

def _set_status(order_id, status, only_from=None):
    conn = db()
    if only_from:
        marks = ",".join("?" * len(only_from))
        conn.execute(f"UPDATE orders SET status=? WHERE id=? AND status IN ({marks})",
                     (status, order_id, *only_from))
    else:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()


@app.route("/orders", methods=["GET"])
@require_admin
def all_orders():
    conn = db()
    rows = conn.execute(f"SELECT {PUBLIC_COLS} FROM orders ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"orders": [dict(r) for r in rows]})


@app.route("/admin/cancel/<int:order_id>", methods=["POST"])
@require_admin
def admin_cancel(order_id):
    conn = db()
    row = conn.execute("SELECT status, payment, file_path FROM orders WHERE id=?", (order_id,)).fetchone()
    if row and row["status"] in ("pending_payment", "new", "printing", "failed"):
        paid = row["status"] != "pending_payment" and is_online_payment(row["payment"])
        conn.execute("UPDATE orders SET status='cancelled', refund=?, file_path=NULL WHERE id=?",
                     ("needed" if paid else "", order_id))
        conn.commit()
        _remove(row["file_path"])
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/reprint/<int:order_id>", methods=["POST"])
@require_admin
def admin_reprint(order_id):
    conn = db()
    row = conn.execute("SELECT status, file_path, order_type FROM orders WHERE id=?", (order_id,)).fetchone()
    has_file = row and (row["order_type"] in ("copy", "scan") or (row["file_path"] and os.path.exists(row["file_path"])))
    if not row or row["status"] not in ("failed", "printing", "printed") or not has_file:
        conn.close()
        return jsonify({"error": "Перепечатать нельзя: файл уже удалён или заказ не оплачен"}), 409
    conn.execute("UPDATE orders SET status='new', error=NULL, refund='' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/refunded/<int:order_id>", methods=["POST"])
@require_admin
def admin_refunded(order_id):
    conn = db()
    conn.execute("UPDATE orders SET refund='done' WHERE id=? AND refund='needed'", (order_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


def _esc(s):
    return (str(s if s is not None else "-").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@app.route("/admin", methods=["GET"])
def admin():
    if not ADMIN_KEY or request.args.get("key", "") != ADMIN_KEY:
        return "<h2>Нет доступа.</h2>", 403

    conn = db()
    orders = [dict(o) for o in conn.execute(f"SELECT {PUBLIC_COLS} FROM orders ORDER BY id DESC LIMIT 100")]
    terminals = [dict(t) for t in conn.execute("SELECT * FROM terminals ORDER BY id")]
    conn.close()

    earned = [o for o in orders if o["status"] in ("printed", "scan_ready")]
    today = datetime.now().strftime("%Y-%m-%d")
    today_orders = [o for o in orders if str(o["created_at"]).startswith(today)]
    stats = [
        (len(today_orders), "Сегодня", "#F5A623"),
        (sum(o["total"] for o in earned if str(o["created_at"]).startswith(today)), "Выручка сегодня, с", "#F5A623"),
        (sum(o["total"] for o in earned), "Выручка (100 посл.), с", "#F5A623"),
        (sum(1 for o in orders if o["status"] == "new"), "В очереди", "#F5A623"),
        (sum(1 for o in orders if o["status"] == "failed"), "Ошибки печати", "#ef4444"),
        (sum(1 for o in orders if o["refund"] == "needed"), "Нужен возврат", "#ef4444"),
    ]
    colors = {"new": "#F5A623", "printing": "#F5A623", "printed": "#22c55e", "scan_ready": "#22c55e",
              "pending_payment": "#7A9AB8", "rejected": "#7A9AB8", "cancelled": "#7A9AB8", "failed": "#ef4444"}

    def btn(action, oid, label, color):
        return (f'<button onclick="act(\'{action}\',{oid})" style="background:{color};color:#fff;border:none;'
                f'padding:6px 10px;border-radius:8px;font-weight:700;cursor:pointer;margin:2px">{label}</button>')

    rows = ""
    for o in orders:
        acts = ""
        if o["status"] in ("failed", "printing", "printed"):
            acts += btn("reprint", o["id"], "Перепечатать", "#2a4f7c")
        if o["status"] in ("pending_payment", "new", "printing", "failed"):
            acts += btn("cancel", o["id"], "Отменить", "#ef4444")
        if o["refund"] == "needed":
            acts += btn("refunded", o["id"], "Деньги вернул", "#22c55e")
        st = STATUS_RU.get(o["status"], o["status"])
        if o["refund"] == "needed":
            st += " · нужен возврат"
        elif o["refund"] == "done":
            st += " · возвращено"
        if o["error"]:
            st += f"<br><small>{_esc(o['error'])}</small>"
        rows += (f"<tr><td>#{o['id']}</td><td>{_esc(o['created_at'])}</td><td>{_esc(o['file_name'])}</td>"
                 f"<td>{_esc(o['order_type'])} {_esc(o['print_type'])}</td><td>{o['pages']}×{o['copies']}</td>"
                 f"<td><b>{o['total']} с</b></td><td>{_esc(o['terminal_id'])}</td>"
                 f"<td style='color:{colors.get(o['status'], '#aaa')}'>{st}</td><td>{acts or '-'}</td></tr>")

    term_html = ""
    for t in terminals:
        online = t["last_seen"] and t["last_seen"] >= ago(minutes=OFFLINE_ALERT_MIN)
        term_html += (f"<div class='sc'><div class='sn' style='color:{'#22c55e' if online else '#ef4444'};font-size:18px'>"
                      f"{_esc(t['id'])} · {'на связи' if online else 'НЕ НА СВЯЗИ'}</div>"
                      f"<div class='sl'>был: {_esc(t['last_seen'])} · принтер: {_esc(t['printer_status'] or '—')}</div></div>")

    stat_html = "".join(f"<div class='sc'><div class='sn' style='color:{c}'>{v}</div><div class='sl'>{l}</div></div>"
                        for v, l, c in stats)

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AkBarak Admin</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#0D2240;color:#fff}}
.hdr{{background:#1a3a5c;padding:20px 24px;display:flex;align-items:center;justify-content:space-between}}
.brand{{font-size:20px;font-weight:900}}.brand span{{color:#F5A623}}
.btn{{background:#F5A623;color:#000;border:none;padding:10px 20px;border-radius:10px;font-weight:800;cursor:pointer}}
.stats{{display:flex;gap:12px;padding:20px 20px 0;flex-wrap:wrap}}
.sc{{background:#1a3a5c;border-radius:16px;padding:18px 20px;flex:1;min-width:150px}}
.sn{{font-size:28px;font-weight:900}}
.sl{{font-size:11px;color:rgba(255,255,255,0.5);margin-top:4px;text-transform:uppercase;letter-spacing:1px}}
.tw{{padding:20px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;background:#1a3a5c;border-radius:14px;overflow:hidden}}
th{{padding:12px;text-align:left;font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase}}
td{{padding:10px 12px;font-size:13px;border-top:1px solid rgba(255,255,255,0.05);color:rgba(255,255,255,0.85)}}
</style></head><body>
<div class="hdr"><div class="brand">AK<span>BARAK</span> &mdash; Админ</div><button class="btn" onclick="location.reload()">Обновить</button></div>
<div class="stats">{term_html or "<div class='sc'><div class='sl'>Терминалы ещё не выходили на связь</div></div>"}</div>
<div class="stats">{stat_html}</div>
<div class="tw"><table>
<thead><tr><th>#</th><th>Время</th><th>Файл</th><th>Тип</th><th>Объём</th><th>Сумма</th><th>Терминал</th><th>Статус</th><th>Действия</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<script>
const KEY = new URLSearchParams(location.search).get("key");
const ASK = {{cancel:"Отменить заказ?", reprint:"Отправить на печать ещё раз?", refunded:"Отметить, что деньги вернули?"}};
async function act(a, id){{
  if(!window.confirm(ASK[a])) return;
  const r = await fetch("/admin/"+a+"/"+id, {{method:"POST", headers:{{"X-Admin-Key":KEY}}}});
  if(!r.ok){{ const d = await r.json().catch(()=>({{}})); alert(d.error || "Ошибка"); }}
  location.reload();
}}
setTimeout(function(){{location.reload();}}, 30000);
</script>
</body></html>"""


# ─────────────────────── Фоновые проверки ───────────────────────

def _remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            log.error("Не удалось удалить %s: %s", path, e)


def maintenance():
    """Раз в минуту: удаляет старые файлы, закрывает неоплаченные заказы, шлёт уведомления."""
    conn = db()
    try:
        # 1. Файлы старше FILE_TTL_HOURS удаляем всегда (личные документы студентов)
        old = conn.execute("SELECT id, file_path, status FROM orders WHERE file_path IS NOT NULL AND created_at < ?",
                           (ago(hours=FILE_TTL_HOURS),)).fetchall()
        for r in old:
            _remove(r["file_path"])
            conn.execute("UPDATE orders SET file_path=NULL WHERE id=?", (r["id"],))
        # 2. Неоплаченные заказы старше суток — закрываем
        conn.execute("UPDATE orders SET status='rejected' WHERE status='pending_payment' AND created_at < ?",
                     (ago(hours=24),))
        # 3. Оплаченный заказ давно не напечатан
        stuck = conn.execute(
            "SELECT id, terminal_id, total FROM orders WHERE status IN ('new','printing') AND alerted=0 "
            "AND COALESCE(paid_at, created_at) < ?", (ago(minutes=STUCK_ALERT_MIN),)).fetchall()
        for r in stuck:
            notify(f"⏳ Заказ #{r['id']} ({r['total']} сом) оплачен, но не напечатан уже {STUCK_ALERT_MIN} мин. "
                   f"Терминал: {r['terminal_id']}")
            conn.execute("UPDATE orders SET alerted=1 WHERE id=?", (r["id"],))
        # 4. Терминал пропал
        lost = conn.execute("SELECT id, last_seen FROM terminals WHERE offline_alerted=0 AND last_seen < ?",
                            (ago(minutes=OFFLINE_ALERT_MIN),)).fetchall()
        for t in lost:
            notify(f"🔴 Терминал {t['id']} не выходит на связь с {t['last_seen']}. "
                   "Проверьте свет, интернет и ноутбук.")
            conn.execute("UPDATE terminals SET offline_alerted=1 WHERE id=?", (t["id"],))
        conn.commit()
    finally:
        conn.close()


def _maintenance_loop():
    while True:
        try:
            maintenance()
        except Exception:
            log.exception("maintenance")
        time.sleep(60)


if os.getenv("DISABLE_BACKGROUND") != "1":
    threading.Thread(target=_maintenance_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

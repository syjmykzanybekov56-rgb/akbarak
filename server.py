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
    DUPLEX_ENABLED     "1" — разрешить двустороннюю печать и брошюры (включайте, когда
                       стоит принтер с автоматическим дуплексом)
    CASH_ENABLED       "1" — разрешить оплату наличными (по умолчанию выключено:
                       купюроприёмника нет, иначе печать бесплатная)
    FILE_TTL_HOURS     через сколько часов удалять файлы (по умолчанию 24)
    DEFAULT_TERMINAL   id терминала по умолчанию (по умолчанию kgma-1)
    GOPAY_*            см. gopay.py
    MKASSA_*           см. mkassa.py (МБанк QR; если задан MKASSA_API_KEY — оплата идёт через него)
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
import mkassa
import doc_templates

# Какой платёжный сервис: MKassa (МБанк QR), если на Railway есть MKASSA_API_KEY, иначе GoPay
PAY = mkassa if mkassa.enabled() else gopay
PAY_NAME = "МБанк" if PAY is mkassa else "GoPay"

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
COLOR_ENABLED = os.getenv("COLOR_ENABLED", "1") == "1"
NO_COLOR_MSG = "Цветная печать и фото пока недоступны — сейчас только ч/б"
DUPLEX_ENABLED = os.getenv("DUPLEX_ENABLED", "0") == "1"
NO_DUPLEX_MSG = "Двусторонняя печать и брошюры пока недоступны — сейчас только односторонняя"
FILE_TTL_HOURS = int(os.getenv("FILE_TTL_HOURS", "24"))
DEFAULT_TERMINAL = os.getenv("DEFAULT_TERMINAL", "kgma-1")
SITE_URL = os.getenv("SITE_URL", "https://akbarak.vercel.app").rstrip("/")

OFFLINE_ALERT_MIN = 5      # терминал молчит дольше — уведомление
STUCK_ALERT_MIN = 10       # оплаченный заказ не напечатан дольше — уведомление
MAX_UPLOAD_MB = 25
MAX_PAGES = 500
MAX_SCAN_SHEETS = 30       # сколько листов можно отсканировать/скопировать за один заказ
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".txt", ".jpg", ".jpeg", ".png"}

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Цены считает ТОЛЬКО сервер. То, что присылает браузер, игнорируется.
PRICES = {
    "print_bw": 5, "print_color": 10,
    "copy_bw": 6, "copy_color": 12,
    "scan_bw": 6, "scan_color": 12,
}
# Двусторонняя печать и брошюра — цена за ЛИСТ (обе стороны). Поменяйте здесь.
SHEET_PRICES = {"bw": 8, "color": 18}
SIDES = ("single", "duplex", "booklet")   # односторонняя / двусторонняя / брошюра
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
        "ALTER TABLE orders ADD COLUMN sides TEXT DEFAULT 'single'",
        "ALTER TABLE orders ADD COLUMN scan_done INTEGER DEFAULT 0",   # сколько листов уже отсканировано
        "ALTER TABLE orders ADD COLUMN scan_go INTEGER DEFAULT 1",     # до какого листа студент разрешил сканировать
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
    try:   # цветной ли принтер у терминала: сообщает программа печати (NULL — неизвестно)
        conn.execute("ALTER TABLE terminals ADD COLUMN color INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def color_ok(terminal_id=None):
    """Цвет доступен: включён на сервере и принтер этого терминала не чёрно-белый."""
    if not COLOR_ENABLED:
        return False
    conn = db()
    row = conn.execute("SELECT color FROM terminals WHERE id=?", ((terminal_id or DEFAULT_TERMINAL)[:40],)).fetchone()
    conn.close()
    return not (row and row["color"] == 0)


NO_COLOR_TERMINAL_MSG = "На этом терминале сейчас чёрно-белый принтер — цветная печать временно недоступна"

# «Ак сынак» — честный входной контроль на занятии (прототип)
import sinak  # noqa: E402
sinak.register(app, db)

# Колонки, которые отдаём терминалу (без больших/секретных полей)
PUBLIC_COLS = ("id, file_name, print_type, pages, copies, total, payment, page_range, status, "
               "created_at, paid_at, group_id, terminal_id, order_type, photo_format, error, refund, sides")


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


_DOCX_EM_PER_CHAR = 0.52     # средняя ширина буквы в долях кегля (подобрано по реальным рефератам)
_DOCX_LINE_K = 1.26          # высота «одинарной» строки в долях кегля


def _docx_layout(z, xml):
    """Кегль, межстрочный интервал и поля страницы из самого документа (по умолчанию — ГОСТ-реферат)."""
    import re
    size, mult = 14.0, 1.5
    try:
        st = z.read("word/styles.xml").decode("utf-8", "ignore")
        blocks = re.findall(r"<w:docDefaults>.*?</w:docDefaults>", st, flags=re.S) + \
            re.findall(r'<w:style [^>]*w:styleId="(?:Normal|a)"[^>]*>.*?</w:style>', st, flags=re.S)
        for b in blocks:
            m = re.search(r'<w:sz w:val="(\d+)"', b)
            if m:
                size = int(m.group(1)) / 2
            m = re.search(r'<w:spacing [^>]*w:line="(\d+)"(?: [^>]*w:lineRule="(\w+)")?', b)
            if m and (m.group(2) or "auto") == "auto":
                mult = int(m.group(1)) / 240
    except KeyError:
        pass
    pw, ph, ml, mr, mt, mb = 11906, 16838, 1701, 850, 1134, 1134       # А4, поля 3/1.5/2/2 см (в twip)
    m = re.search(r"<w:pgSz [^>]*>", xml)
    if m:
        pw = int((re.search(r'w:w="(\d+)"', m.group(0)) or [0, pw])[1]); ph = int((re.search(r'w:h="(\d+)"', m.group(0)) or [0, ph])[1])
    m = re.search(r"<w:pgMar [^>]*>", xml)
    if m:
        g = lambda k, d: abs(int((re.search(rf'w:{k}="(-?\d+)"', m.group(0)) or [0, d])[1]))
        ml, mr, mt, mb = g("left", ml), g("right", mr), g("top", mt), g("bottom", mb)
    width_pt, height_pt = (pw - ml - mr) / 20, (ph - mt - mb) / 20
    cpl = max(20, width_pt / (size * _DOCX_EM_PER_CHAR))
    lpp = max(10, height_pt / (size * _DOCX_LINE_K * max(1.0, mult)))
    return cpl, lpp


def docx_page_count(path: str):
    """Страницы Word-файла (.docx) без Word: число из свойств документа (его пишет Word при сохранении),
    иначе — оценка по тексту, таблицам, картинкам и разрывам с учётом шрифта и полей."""
    import re
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "docProps/app.xml" in names:
                m = re.search(r"<(?:\w+:)?Pages>(\d+)</(?:\w+:)?Pages>", z.read("docProps/app.xml").decode("utf-8", "ignore"))
                if m and int(m.group(1)) > 1:      # «1» часто бывает у файлов не из Word — тогда считаем сами
                    return int(m.group(1))
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
            cpl, lpp = _docx_layout(z, xml)
    except Exception:
        return None
    breaks = len(re.findall(r'<w:br [^>]*w:type="page"', xml)) + len(re.findall(r"<w:pageBreakBefore(?: [^>]*)?/>", xml)) \
        + len(re.findall(r'<w:type w:val="nextPage"', xml))
    lines = 0.0
    for tbl in re.findall(r"<w:tbl>.*?</w:tbl>", xml, flags=re.S):
        for tr in re.findall(r"<w:tr[ >].*?</w:tr>", tbl, flags=re.S):
            cells = re.findall(r"<w:tc>.*?</w:tc>", tr, flags=re.S) or [tr]
            n = len(cells)
            lines += max(1, max(len("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", c))) * n // cpl + 1 for c in cells))
    body = re.sub(r"<w:tbl>.*?</w:tbl>", "", xml, flags=re.S)
    for par in re.findall(r"<w:p[ >].*?</w:p>", body, flags=re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", par))
        lines += max(1, -(-len(text) // cpl))
        lines += 0.4 * lpp * (par.count("<w:drawing") + par.count("<w:pict"))
    # разрывы страниц оставляют недозаполненные страницы: добавляем по полстраницы на каждый
    return max(breaks + 1, int(lines / lpp + 0.5 * breaks + 0.4 + 0.5), 1)


def txt_page_count(path: str):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = sum(max(1, -(-len(l.rstrip()) // 90)) for l in f)
        return max(1, -(-lines // 55))
    except Exception:
        return None


def clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def order_total(order_type, print_type, sides, pages, copies):
    """Единственное место, где считается цена печати документа.
    single  — каждая страница по цене PRICES;
    duplex  — по 2 страницы на лист по цене SHEET_PRICES, нечётная последняя — как одна страница;
    booklet — по 4 страницы на лист (2 на каждой стороне, сгиб пополам), цена листа SHEET_PRICES."""
    pages = max(1, int(pages or 1))
    copies = max(1, int(copies or 1))
    kind = order_type if order_type in ("copy", "scan") else "print"
    if kind != "print" or sides not in ("duplex", "booklet"):
        return PRICES[f"{kind}_{print_type}"] * pages * copies
    sheet = SHEET_PRICES[print_type]
    if sides == "duplex":
        per_copy = (pages // 2) * sheet + (pages % 2) * PRICES[f"print_{print_type}"]
    else:
        per_copy = -(-pages // 4) * sheet   # округление вверх
    return per_copy * copies


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


@app.route("/config", methods=["GET"])
def config():
    """Что сейчас умеет терминал — сайт прячет недоступные режимы."""
    return jsonify({"color": color_ok(request.args.get("t")), "duplex": DUPLEX_ENABLED,
                    "prices": PRICES, "sheet_prices": SHEET_PRICES})


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
        if (photo_format or request.form.get("print_type") == "color") and not color_ok(terminal):
            return jsonify({"error": NO_COLOR_TERMINAL_MSG}), 400
        sides = request.form.get("sides") or "single"
        if sides not in SIDES:
            sides = "single"
        if photo_format:
            sides = "single"
        if sides != "single" and not DUPLEX_ENABLED:
            return jsonify({"error": NO_DUPLEX_MSG}), 400
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

                file_sides = sides
                if ext in (".jpg", ".jpeg", ".png"):
                    file_sides = "single"   # картинка — это одна страница
                if order_type == "photo":
                    pages, rng = 1, None
                else:
                    real = (pdf_page_count(file_path) if ext == ".pdf"
                            else docx_page_count(file_path) if ext == ".docx"
                            else txt_page_count(file_path) if ext == ".txt" else None)
                    rng = page_range if len(files) == 1 else None
                    if rng:
                        selected = parse_range(rng, real)
                        if not selected:
                            raise ValueError("Неверный диапазон страниц. Пример: 1-5 или 1,3,5")
                        pages = len(selected)
                    else:
                        pages = real or client_pages
                    pages = max(1, min(MAX_PAGES, pages))

                total = (unit if order_type == "photo"
                         else order_total(order_type, print_type, file_sides, pages, copies))
                grand_total += total
                cur = conn.execute(
                    "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, "
                    "page_range, status, created_at, group_id, terminal_id, order_type, photo_format, access_token, "
                    "sides) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (file.filename, file_path, print_type, pages, copies, total, payment, rng, status,
                     now(), group_id, terminal, order_type, photo_format or None, token, file_sides))
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
        "files": [{"name": i["file_name"], "pages": i["pages"],
                   "image": os.path.splitext(i["file_name"] or "")[1].lower() in (".jpg", ".jpeg", ".png")}
                  for i in items],
        "color": color_ok(sess["terminal_id"]),
        "print_type": items[0]["print_type"] if items else "bw",
        "preview": f"/kiosk/preview/{sess['order_id']}",
        "duplex": DUPLEX_ENABLED,
        "copies": items[0]["copies"] if items else 1,
        "unit": PRICES["print_" + (items[0]["print_type"] if items else "bw")],
        "total": sum(i["total"] for i in items),
    })


@app.route("/kiosk/preview/<int:order_id>/<int:idx>", methods=["GET"])
def kiosk_preview(order_id, idx):
    """Файл заказа для предпросмотра на экране терминала — только с ключом заказа
    и только пока заказ не напечатан (потом файл удаляется)."""
    conn, group_id = _group_by_token(order_id, request.args.get("token"))
    if not conn:
        return jsonify({"error": "Заказ не найден"}), 404
    try:
        items = conn.execute("SELECT file_path, file_name, status FROM orders WHERE group_id=? ORDER BY id",
                             (group_id,)).fetchall()
    finally:
        conn.close()
    if not 0 <= idx < len(items):
        return jsonify({"error": "Файл не найден"}), 404
    row = items[idx]
    if row["status"] not in ("pending_payment", "new", "printing") or not row["file_path"] \
            or not os.path.exists(row["file_path"]):
        return jsonify({"error": "Файл уже недоступен"}), 404
    resp = send_file(row["file_path"], download_name=row["file_name"])
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/kiosk/options/<int:order_id>", methods=["POST"])
def kiosk_options(order_id):
    """Изменить число копий и стороны печати на экране — только пока заказ не оплачен."""
    data = request.get_json(silent=True) or {}
    sides = data.get("sides")
    if sides is not None and sides not in SIDES:
        return jsonify({"error": "Неизвестный режим печати"}), 400
    if sides not in (None, "single") and not DUPLEX_ENABLED:
        return jsonify({"error": NO_DUPLEX_MSG}), 400
    conn, group_id = _group_by_token(order_id, data.get("token"))
    if not conn:
        return jsonify({"error": "Заказ не найден"}), 404
    try:
        copies = clamp(data.get("copies"), 1, 50, 1)
        items = conn.execute("SELECT id, pages, print_type, order_type, sides, file_name, file_path, terminal_id, "
                             "page_range FROM orders WHERE group_id=? AND status='pending_payment' ORDER BY id",
                             (group_id,)).fetchall()
        if not items:
            return jsonify({"error": "Заказ уже оплачен"}), 409
        color = data.get("color")
        if color and not color_ok(items[0]["terminal_id"]):
            return jsonify({"error": NO_COLOR_TERMINAL_MSG}), 400
        # диапазон страниц: {"files": [{"from": 2, "to": 5}, ...]} — по одному на файл, по порядку
        ranges = data.get("ranges") if isinstance(data.get("ranges"), list) else None
        total, out = 0, []
        for n, i in enumerate(items):
            if i["order_type"] == "photo":
                continue
            is_image = os.path.splitext(i["file_name"] or "")[1].lower() in (".jpg", ".jpeg", ".png")
            item_sides = i["sides"] or "single"
            if sides is not None:
                item_sides = "single" if is_image else sides   # картинка — одна страница
            ptype = i["print_type"] if color is None else ("color" if color else "bw")
            pages, rng = i["pages"] or 1, i["page_range"]
            real = _real_pages(i["file_path"], i["file_name"]) or pages
            if ranges is not None and n < len(ranges) and not is_image and isinstance(ranges[n], dict):
                a = clamp(ranges[n].get("from"), 1, real, 1)
                b = clamp(ranges[n].get("to"), a, real, real)
                rng = None if (a == 1 and b == real) else (str(a) if a == b else f"{a}-{b}")
                pages = b - a + 1
            pages = max(1, min(MAX_PAGES, pages))
            item_total = order_total(i["order_type"], ptype, item_sides, pages, copies)
            total += item_total
            conn.execute("UPDATE orders SET copies=?, total=?, sides=?, print_type=?, pages=?, page_range=? WHERE id=?",
                         (copies, item_total, item_sides, ptype, pages, rng, i["id"]))
            out.append({"pages": pages, "real": real, "range": rng})
        conn.commit()
        return jsonify({"success": True, "copies": copies, "total": total, "sides": sides or "single",
                        "files": out})
    finally:
        conn.close()


def _real_pages(path, name):
    """Сколько страниц в файле на самом деле (без учёта выбранного диапазона)."""
    ext = os.path.splitext(name or path or "")[1].lower()
    if not path or not os.path.exists(path):
        return None
    try:
        return (pdf_page_count(path) if ext == ".pdf" else docx_page_count(path) if ext == ".docx"
                else txt_page_count(path) if ext == ".txt" else 1)
    except Exception:
        return None


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
        # Цветной скан делает сам сканер — цветной принтер нужен только для цветной копии
        if color and order_type == "copy" and not COLOR_ENABLED:
            return jsonify({"error": NO_COLOR_MSG}), 400
        if color and order_type == "copy" and not color_ok(data.get("terminal")):
            return jsonify({"error": NO_COLOR_TERMINAL_MSG}), 400
        print_type = "color" if color else "bw"
        # Листы кладут на стекло по одному: после каждого студент нажимает «следующий лист»
        pages = clamp(data.get("pages"), 1, MAX_SCAN_SHEETS, 1)
        copies = clamp(data.get("copies"), 1, 20, 1) if order_type == "copy" else 1
        idcard = bool(data.get("idcard"))
        if idcard:
            # ID-карта/паспорт: две стороны сканируются по очереди, а на выходе один лист —
            # и цена как за один лист
            pages = 2
            total = PRICES[f"{order_type}_{print_type}"] * copies
        else:
            total = PRICES[f"{order_type}_{print_type}"] * pages * copies
        token = secrets.token_urlsafe(16)
        status = "pending_payment" if is_online_payment(payment) else "new"

        conn = db()
        cur = conn.execute(
            "INSERT INTO orders (file_name, print_type, pages, copies, total, payment, status, created_at, "
            "group_id, terminal_id, order_type, access_token, sides) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_type, print_type, pages, copies, total, payment, status, now(), uuid.uuid4().hex,
             (data.get("terminal") or DEFAULT_TERMINAL)[:40], order_type, token, "idcard" if idcard else "single"))
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
        if PAY is mkassa:
            prev = conn.execute("SELECT gopay_payment_id FROM orders WHERE id=?", (order_id,)).fetchone()
            if prev and prev["gopay_payment_id"]:
                conn.close()
                if _mkassa_sync_tx(prev["gopay_payment_id"]):      # старый QR уже оплачен
                    return jsonify({"error": "Заказ уже оплачен"}), 409
                conn = db()
        payment = PAY.create_payment(order_id, amount, f"АкБарак заказ #{order_id}", return_url=return_url)
        if not payment or not payment.get("payment_id"):
            conn.close()
            return jsonify({"error": f"Не удалось создать платёж в {PAY_NAME}"}), 502

        conn.executemany("UPDATE orders SET gopay_payment_id=? WHERE id=?",
                         [(payment["payment_id"], r["id"]) for r in rows])
        conn.commit()
        conn.close()
        return jsonify({"success": True, "amount": amount, "provider": "mkassa" if PAY is mkassa else "gopay",
                        "expires_in": payment.get("expires_in", 0),
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
                tgbot.order_event(to_queue, "paid")
        elif status in ("failed", "cancelled", "expired"):
            conn.executemany("UPDATE orders SET status='rejected' WHERE id=? AND status='pending_payment'",
                             [(i,) for i in ids])
            conn.commit()
            tgbot.order_event(ids, "rejected")
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        log.exception("webhook")
        return jsonify({"error": str(e)}), 500


# ─────────────────────── MKassa (МБанк QR) ───────────────────────

def _mkassa_apply(tx):
    """Применяет статус транзакции, полученной из API MKassa. True — если заказ оплачен."""
    if not tx or (tx.get("status") or "").lower() not in mkassa.PAID:
        return False
    oid = mkassa.order_id_of(tx)
    if not oid:
        return False
    conn = db()
    try:
        row = conn.execute("SELECT group_id FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False
        rows = conn.execute("SELECT id, status, total FROM orders WHERE group_id=?", (row["group_id"],)).fetchall() \
            if row["group_id"] else conn.execute("SELECT id, status, total FROM orders WHERE id=?", (oid,)).fetchall()
        expected = sum(r["total"] for r in rows if r["status"] != "cancelled")
        to_queue = [r["id"] for r in rows if r["status"] in ("pending_payment", "rejected")]
        if not to_queue:
            return True
        paid = mkassa.amount_som(tx)
        if abs(paid - expected) > 0.01:
            notify(f"⚠️ МБанк: сумма не совпадает! Заказ #{oid}: ждали {expected} сом, пришло {paid}. "
                   f"Транзакция {tx.get('id')}. Печать не запущена, проверьте вручную.")
            return False
        conn.executemany("UPDATE orders SET status='new', paid_at=?, gopay_payment_id=? WHERE id=?",
                         [(now(), tx.get("id"), i) for i in to_queue])
        conn.commit()
    finally:
        conn.close()
    tgbot.order_event(to_queue, "paid")
    return True


def _mkassa_sync_tx(tx_id):
    return _mkassa_apply(mkassa.get_transaction(tx_id))


_SYNC = {}          # order_id -> время последней проверки
_SYNC_LOCK = threading.Lock()


def sync_payment(order_id, wait=False):
    """Запасной путь, если колбэк МБанка не дошёл: сами спрашиваем статус (не чаще раза в 4 с)."""
    if PAY is not mkassa:
        return
    with _SYNC_LOCK:
        if time.time() - _SYNC.get(order_id, 0) < 4:
            return
        _SYNC[order_id] = time.time()
        if len(_SYNC) > 500:
            for k in [k for k, v in _SYNC.items() if time.time() - v > 600]:
                _SYNC.pop(k, None)
    conn = db()
    row = conn.execute("SELECT gopay_payment_id, status FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not row or row["status"] != "pending_payment" or not row["gopay_payment_id"]:
        return
    if wait:
        _mkassa_sync_tx(row["gopay_payment_id"])
    else:
        threading.Thread(target=_mkassa_sync_tx, args=(row["gopay_payment_id"],), daemon=True).start()


@app.route("/mkassa_webhook", methods=["POST"])
def mkassa_webhook():
    """Колбэк от MKassa. Не верим телу — перепроверяем транзакцию по API.
    500 → MKassa повторит отправку (так написано в их доке)."""
    data = request.get_json(silent=True) or {}
    tx_id = str(data.get("id") or "").strip()
    if not tx_id or not mkassa.enabled():
        return jsonify({"success": False}), 200
    tx = mkassa.get_transaction(tx_id)
    if tx is None:
        return jsonify({"error": "MKassa недоступна, повторите"}), 500
    try:
        _mkassa_apply(tx)
    except Exception:
        log.exception("mkassa_webhook")
        return jsonify({"error": "internal"}), 500
    return jsonify({"success": True}), 200


@app.route("/admin/api/mkassa", methods=["GET"])
def admin_mkassa():
    if not _admin_key_ok():
        return jsonify({"error": "нет доступа"}), 403
    out = {"enabled": mkassa.enabled(), "active_provider": PAY_NAME, "branch": mkassa.BRANCH,
           "cashier": mkassa.CASHIER, "long_living": mkassa.LONG_LIVING, "last_error": mkassa.LAST_ERROR,
           "webhook_url": request.host_url.rstrip("/").replace("http://", "https://") + "/mkassa_webhook"}
    if mkassa.enabled() and request.args.get("branches"):
        try:
            out["branches_status"], out["branches"] = mkassa.branches()
        except Exception as e:
            out["branches_error"] = str(e)
    return jsonify(out)


@app.route("/payment_status/<int:order_id>", methods=["GET"])
def payment_status(order_id):
    sync_payment(order_id, wait=True)
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
    tgbot.order_event(order_id, "printing")
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
        tgbot.order_event(order_id, "printed")
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
        tgbot.order_event(order_id, "failed")
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
            "WHERE terminal_id=? AND status IN ('printed','done','scan_ready') AND created_at >= ?",
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
    if data.get("color") is not None:
        conn = db()
        conn.execute("UPDATE terminals SET color=? WHERE id=?", (1 if data["color"] else 0, terminal))
        conn.commit()
        conn.close()
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


# ─────────────────────── Скан и копия нескольких листов ───────────────────────
# Терминал сканирует лист, сообщает /scan_progress и ждёт, пока студент положит
# следующий лист и нажмёт кнопку (/scan_next) на экране терминала или в телефоне.

@app.route("/scan_next/<int:order_id>", methods=["POST"])
def scan_next(order_id):
    data = request.get_json(silent=True) or {}
    conn = db()
    try:
        row = conn.execute("SELECT access_token, status, pages, scan_done, scan_go FROM orders WHERE id=?",
                           (order_id,)).fetchone()
        if not row or not row["access_token"] or not secrets.compare_digest(data.get("token") or "", row["access_token"]):
            return jsonify({"error": "Заказ не найден"}), 404
        done = row["scan_done"] or 0
        if row["status"] != "printing" or done >= (row["pages"] or 1):
            return jsonify({"error": "Сейчас нечего сканировать"}), 409
        conn.execute("UPDATE orders SET scan_go=? WHERE id=?", (done + 1, order_id))
        conn.commit()
        return jsonify({"success": True, "sheet": done + 1})
    finally:
        conn.close()


@app.route("/scan_go/<int:order_id>", methods=["GET"])
@require_worker
def scan_go(order_id):
    conn = db()
    row = conn.execute("SELECT status, scan_go FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Заказ не найден"}), 404
    return jsonify({"go": row["scan_go"] or 1, "status": row["status"]})


@app.route("/scan_progress/<int:order_id>", methods=["POST"])
@require_worker
def scan_progress(order_id):
    done = clamp((request.get_json(silent=True) or {}).get("done"), 0, MAX_SCAN_SHEETS, 0)
    conn = db()
    conn.execute("UPDATE orders SET scan_done=? WHERE id=?", (done, order_id))
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
            "SELECT id, order_type, status, pages, copies, total, error, refund, scan_done, scan_go, sides, "
            "(scan_data IS NOT NULL) AS has_scan FROM orders WHERE group_id=? ORDER BY id",
            (row["group_id"],)).fetchall()
        term = conn.execute("SELECT last_seen, printer_status FROM terminals WHERE id=?",
                            (row["terminal_id"],)).fetchone()
    finally:
        conn.close()

    statuses = [i["status"] for i in items]
    if any(s == "pending_payment" for s in statuses):
        overall = "pending_payment"
        sync_payment(order_id)
    elif any(s == "cancelled" for s in statuses):
        overall = "cancelled"
    elif any(s == "failed" for s in statuses):
        overall = "failed"
    elif all(s in ("printed", "done", "scan_ready") for s in statuses):   # /done ставит 'printed'
        overall = "done"
    elif any(s == "printing" for s in statuses):
        overall = "printing"
    else:
        overall = "paid"

    printer = None
    if term:
        printer = term["printer_status"] if term["printer_status"] not in (None, "ok") else "ok"
    first = items[0] if items else None
    sheets = scanned = 0
    waiting_next = False
    if first and first["order_type"] in ("scan", "copy"):
        sheets = first["pages"] or 1
        scanned = first["scan_done"] or 0
        waiting_next = (first["status"] == "printing" and 0 < scanned < sheets
                        and (first["scan_go"] or 1) <= scanned)
    return jsonify({
        "order_id": order_id,
        "status": overall,
        "sheets": sheets,
        "scanned": scanned,
        "waiting_next": waiting_next,
        "idcard": bool(first and first["sides"] == "idcard"),
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
        data = bytes(row["scan_data"])
        is_pdf = data[:4] == b"%PDF"            # несколько листов приходят одним PDF
        return send_file(io.BytesIO(data), mimetype="application/pdf" if is_pdf else "image/jpeg",
                         as_attachment=True, download_name=f"scan_{order_id}.{'pdf' if is_pdf else 'jpg'}")
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


@app.route("/admin/free/<int:order_id>", methods=["POST"])
@require_admin
def admin_free(order_id):
    """Владелец печатает для себя: неоплаченный заказ уходит в печать бесплатно (сумма 0)."""
    conn = db()
    cur = conn.execute("UPDATE orders SET status='new', paid_at=?, total=0, payment='free' "
                       "WHERE id=? AND status IN ('pending_payment','rejected')", (now(), order_id))
    conn.commit()
    conn.close()
    if not cur.rowcount:
        return jsonify({"error": "Этот заказ уже оплачен или закрыт"}), 409
    tgbot.order_event(order_id, "paid")
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


TZ_OFF = timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "6")))   # Бишкек UTC+6
PAID_STATUSES = ("new", "printing", "printed", "scan_ready", "failed")
SERVICE_RU = {"print": "Печать", "copy": "Копия", "scan": "Скан", "photo": "Фото", "template": "Шаблон"}
HERE_DIR = os.path.dirname(os.path.abspath(__file__))


def _local(ts):
    """Время из базы (UTC сервера) → местное время Бишкека."""
    try:
        return datetime.strptime(ts, TIME_FMT) + TZ_OFF
    except (TypeError, ValueError):
        return None


def _admin_key_ok():
    return bool(ADMIN_KEY) and secrets.compare_digest(_given_key() or "", ADMIN_KEY)


@app.route("/admin", methods=["GET"])
def admin():
    if not _admin_key_ok():
        return "<h2>Нет доступа.</h2>", 403
    resp = send_file(os.path.join(HERE_DIR, "server_assets", "admin.html"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def _order_out(o):
    d = dict(o)
    lc, lp = _local(d.get("created_at")), _local(d.get("printed_at"))
    d["created_local"] = lc.strftime("%d.%m %H:%M") if lc else ""
    d["printed_local"] = lp.strftime("%H:%M") if lp else ""
    d["status_ru"] = STATUS_RU.get(d["status"], d["status"])
    d["service_ru"] = SERVICE_RU.get(d.get("order_type") or "print", d.get("order_type") or "")
    return d


@app.route("/admin/api/summary", methods=["GET"])
@require_admin
def admin_summary():
    local_now = datetime.now() + TZ_OFF
    today = local_now.date()
    conn = db()
    rows = conn.execute(
        "SELECT id, total, pages, copies, status, refund, payment, order_type, print_type, terminal_id, created_at "
        "FROM orders WHERE created_at >= ?", (ago(hours=24 * 31),)).fetchall()
    terms = [dict(t) for t in conn.execute("SELECT * FROM terminals ORDER BY id")]
    queue = conn.execute("SELECT status, COUNT(*) c FROM orders WHERE status IN "
                         "('pending_payment','new','printing','failed') GROUP BY status").fetchall()
    refunds = conn.execute("SELECT COUNT(*) c FROM orders WHERE refund='needed'").fetchone()["c"]
    total_all = conn.execute("SELECT COALESCE(SUM(total),0) s, COUNT(*) c FROM orders WHERE status IN "
                             "('new','printing','printed','scan_ready','failed') AND COALESCE(refund,'')!='done'").fetchone()
    conn.close()

    def bucket():
        return {"revenue": 0, "orders": 0, "sheets": 0, "free": 0}
    periods = {"today": bucket(), "yesterday": bucket(), "week": bucket(), "month": bucket()}
    days = {(today - timedelta(days=i)).isoformat(): 0 for i in range(13, -1, -1)}
    hours = [0] * 24
    services, terminals_rev = {}, {}
    for r in rows:
        lc = _local(r["created_at"])
        if not lc:
            continue
        paid = r["status"] in PAID_STATUSES and (r["refund"] or "") != "done"
        if not paid:
            continue
        money = r["total"] or 0
        sheets = (r["pages"] or 0) * (r["copies"] or 1) if r["status"] in ("printed", "scan_ready") else 0
        age = (today - lc.date()).days
        for name, ok in (("today", age == 0), ("yesterday", age == 1), ("week", age < 7), ("month", age < 30)):
            if ok:
                b = periods[name]
                b["revenue"] += money; b["orders"] += 1; b["sheets"] += sheets
                b["free"] += 1 if r["payment"] == "free" else 0
        if lc.date().isoformat() in days:
            days[lc.date().isoformat()] += money
        if age == 0:
            hours[lc.hour] += 1
        if age < 30:
            key = SERVICE_RU.get(r["order_type"] or "print", r["order_type"] or "—")
            if (r["order_type"] or "print") in ("print", "copy"):
                key += " цвет" if r["print_type"] == "color" else " ч/б"
            sv = services.setdefault(key, {"orders": 0, "revenue": 0})
            sv["orders"] += 1; sv["revenue"] += money
            tv = terminals_rev.setdefault(r["terminal_id"] or "—", {"orders": 0, "revenue": 0, "today": 0})
            tv["orders"] += 1; tv["revenue"] += money; tv["today"] += 1 if age == 0 else 0

    term_out = []
    for t in terms:
        online = bool(t["last_seen"] and t["last_seen"] >= ago(minutes=2))
        ls = _local(t["last_seen"])
        st = terminals_rev.get(t["id"], {"orders": 0, "revenue": 0, "today": 0})
        term_out.append({"id": t["id"], "online": online, "printer": t["printer_status"] or "ok", "color": t.get("color"),
                         "last_seen": ls.strftime("%d.%m %H:%M:%S") if ls else "—", **st})
    q = {r["status"]: r["c"] for r in queue}
    return jsonify({
        "now": local_now.strftime("%d.%m.%Y %H:%M"),
        "periods": periods,
        "days": [{"date": d[8:10] + "." + d[5:7], "revenue": v} for d, v in days.items()],
        "hours": hours,
        "services": sorted(({"name": k, **v} for k, v in services.items()), key=lambda x: -x["orders"]),
        "terminals": term_out,
        "queue": {"pending": q.get("pending_payment", 0), "new": q.get("new", 0),
                  "printing": q.get("printing", 0), "failed": q.get("failed", 0), "refunds": refunds},
        "all_time": {"revenue": total_all["s"], "orders": total_all["c"]},
        "color_enabled": COLOR_ENABLED,
    })


@app.route("/admin/api/orders", methods=["GET"])
@require_admin
def admin_orders():
    status = request.args.get("status", "")
    term = request.args.get("terminal", "")
    q = (request.args.get("q") or "").strip()
    limit = clamp(request.args.get("limit"), 1, 500, 150)
    where, args = [], []
    groups = {"queue": ("new", "printing"), "pending": ("pending_payment",), "failed": ("failed",),
              "done": ("printed", "scan_ready"), "closed": ("cancelled", "rejected")}
    if status in groups:
        where.append("status IN (%s)" % ",".join("?" * len(groups[status]))); args += groups[status]
    elif status == "refund":
        where.append("refund='needed'")
    if term:
        where.append("terminal_id=?"); args.append(term)
    if q:
        if q.lstrip("#").isdigit():
            where.append("id=?"); args.append(int(q.lstrip("#")))
        else:
            where.append("file_name LIKE ?"); args.append(f"%{q}%")
    sql = f"SELECT {PUBLIC_COLS}, printed_at FROM orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    conn = db()
    rows = conn.execute(sql, (*args, limit)).fetchall()
    conn.close()
    return jsonify({"orders": [_order_out(r) for r in rows]})


@app.route("/admin/api/export.csv", methods=["GET"])
@require_admin
def admin_export():
    days = clamp(request.args.get("days"), 1, 3650, 30)
    conn = db()
    rows = conn.execute(f"SELECT {PUBLIC_COLS}, printed_at FROM orders WHERE created_at >= ? ORDER BY id",
                        (ago(hours=24 * days),)).fetchall()
    conn.close()
    out = io.StringIO()
    out.write("\ufeff№;Дата;Время;Услуга;Цвет;Страниц;Копий;Сумма, сом;Оплата;Статус;Терминал;Файл\n")
    for r in rows:
        lc = _local(r["created_at"])
        cells = [r["id"], lc.strftime("%d.%m.%Y") if lc else "", lc.strftime("%H:%M") if lc else "",
                 SERVICE_RU.get(r["order_type"] or "print", r["order_type"]), "цвет" if r["print_type"] == "color" else "ч/б",
                 r["pages"], r["copies"], r["total"], r["payment"] or "", STATUS_RU.get(r["status"], r["status"]),
                 r["terminal_id"] or "", (r["file_name"] or "").replace(";", ",")]
        out.write(";".join(str(c) for c in cells) + "\n")
    name = f"akbarak_orders_{(datetime.now() + TZ_OFF).strftime('%Y-%m-%d')}.csv"
    return app.response_class(out.getvalue().encode("utf-8"), mimetype="text/csv",
                              headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/admin/test/<terminal_id>", methods=["POST"])
@require_admin
def admin_test(terminal_id):
    """Тестовая страница на терминал — бесплатно, сразу в очередь."""
    src = os.path.join(HERE_DIR, "server_assets", "test_page.pdf")
    if not os.path.exists(src):
        return jsonify({"error": "Нет тестовой страницы на сервере"}), 500
    color = request.args.get("color") == "1"
    if color and not color_ok(terminal_id):
        return jsonify({"error": NO_COLOR_TERMINAL_MSG}), 400
    dst = os.path.join(FILES_DIR, f"{uuid.uuid4().hex}.pdf")
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())
    conn = db()
    cur = conn.execute(
        "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
        "paid_at, group_id, terminal_id, order_type, access_token, sides) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Тестовая страница.pdf", dst, "color" if color else "bw", 1, 1, 0, "free", "new", now(), now(),
         uuid.uuid4().hex, terminal_id[:40], "print", secrets.token_urlsafe(16), "single"))
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "order_id": oid})


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
        tgbot.cleanup()
    finally:
        conn.close()


def _maintenance_loop():
    while True:
        try:
            maintenance()
        except Exception:
            log.exception("maintenance")
        time.sleep(60)


# Telegram-бот для студентов (tgbot.py)
import tgbot  # noqa: E402
tgbot.register(app, {
    "db": db, "FILES_DIR": FILES_DIR, "now": now, "ago": ago, "remove": _remove,
    "order_total": order_total, "pdf_page_count": pdf_page_count, "docx_page_count": docx_page_count,
    "txt_page_count": txt_page_count, "gopay": PAY, "sync_payment": sync_payment, "PRICES": PRICES, "SIDES": SIDES,
    "COLOR_ENABLED": COLOR_ENABLED, "DUPLEX_ENABLED": DUPLEX_ENABLED, "DEFAULT_TERMINAL": DEFAULT_TERMINAL,
    "MAX_PAGES": MAX_PAGES, "SITE_URL": SITE_URL, "STATUS_RU": STATUS_RU, "admin_ok": _admin_key_ok, "color_ok": color_ok,
})


if os.getenv("DISABLE_BACKGROUND") != "1":
    threading.Thread(target=_maintenance_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

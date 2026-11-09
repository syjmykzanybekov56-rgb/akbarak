"""
АкБарак — авто-печать на терминале.
Запускается на компьютере с принтером (лучше через start_worker.bat).
Каждые 5 секунд забирает оплаченные заказы с сервера и печатает.

Настройка (один раз):
  1. pip install requests pywin32 pillow pypdf
  2. Рядом с этим файлом создайте файл worker_key.txt и вставьте в него
     значение WORKER_KEY из Railway (если ключ на сервере ещё не задан — файл не нужен).
  3. Для печати DOC/DOCX/TXT нужен Microsoft Word или бесплатный LibreOffice.
  4. Двусторонняя печать и брошюры: на сервере включите DUPLEX_ENABLED=1.
     Если принтер не умеет печатать с двух сторон сам — MANUAL_DUPLEX = True
     (печатаются лицевые стороны, вы переворачиваете пачку и жмёте Enter).
"""

import logging
import os
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler

import requests

# ─────────────────────── Настройки ───────────────────────

SERVER = "https://akbarak-production.up.railway.app"
def _default_printer():
    """Принтер берётся тот, что стоит «по умолчанию» в Windows (зелёная галочка).
    Можно задать явно переменной окружения AKBARAK_PRINTER."""
    name = os.getenv("AKBARAK_PRINTER")
    if name:
        return name
    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except Exception:
        return "Pantum M6500W series"

PRINTER_NAME = _default_printer()


def refresh_printer():
    """Перед каждым заказом перечитываем принтер «по умолчанию»: если его поменяли
    в Windows, программу не нужно перезапускать."""
    global PRINTER_NAME
    name = _default_printer()
    if name and name != PRINTER_NAME:
        log.info("Принтер по умолчанию сменился: %s → %s", PRINTER_NAME, name)
        PRINTER_NAME = name
def _find_sumatra():
    """SumatraPDF ищем рядом с программой и в обычных местах установки — путь не зависит от имени пользователя."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "SumatraPDF.exe"),
              os.path.join(here, "SumatraPDF", "SumatraPDF.exe"),
              os.path.join(os.getenv("LOCALAPPDATA", ""), "SumatraPDF", "SumatraPDF.exe"),
              os.path.join(os.getenv("ProgramFiles", r"C:\Program Files"), "SumatraPDF", "SumatraPDF.exe"),
              os.path.join(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"), "SumatraPDF", "SumatraPDF.exe"),
              r"C:\Users\user\AppData\Local\SumatraPDF\SumatraPDF.exe"):
        if p and os.path.exists(p):
            return p
    return os.path.join(os.getenv("LOCALAPPDATA", ""), "SumatraPDF", "SumatraPDF.exe")

SUMATRA = _find_sumatra()
TERMINAL_ID = os.getenv("AKBARAK_TERMINAL", "kgma-1")
CHECK_INTERVAL = 5          # секунд между проверками очереди
HEARTBEAT_INTERVAL = 30     # секунд между отчётами о состоянии принтера
VERSION = "2026-11-09 v10 ч/б принтер — без цвета"   # видно в окне при запуске — так проверяете, что стоит новая версия
SCAN_DPI = 300
SCAN_WAIT_MIN = 5            # сколько ждать, пока студент положит следующий лист

# Двусторонняя печать на принтере БЕЗ автоматического дуплекса (сейчас Pantum M6500W).
# True  — принтер печатает лицевые стороны, программа ждёт, вы переворачиваете пачку,
#         кладёте её обратно в лоток и нажимаете Enter в этом окне — печатаются обороты.
# False — принтер сам печатает с двух сторон (когда купите принтер с дуплексом).
MANUAL_DUPLEX = True
# Настройки ручного переворота — подбираются тестовой печатью (akbarak_test_8.pdf):
BACK_REVERSE = True          # печатать обороты в обратном порядке
BACK_ROTATE_DUPLEX = 0       # повернуть оборот на 180°, если он вверх ногами (0 или 180)
BACK_ROTATE_BOOKLET = 180    # то же для брошюры
MANUAL_WAIT_MIN = 15         # сколько ждать переворота, потом заказ — ошибка
COPY_DPI = 300

LIBREOFFICE_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_key():
    key = os.getenv("AKBARAK_WORKER_KEY", "").strip()
    path = os.path.join(HERE, "worker_key.txt")
    if not key and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    return key


WORKER_KEY = _load_key()

# ─────────────────────── Журнал ───────────────────────

log = logging.getLogger("akbarak")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s - %(message)s")
for _h in (logging.StreamHandler(sys.stdout),
           RotatingFileHandler(os.path.join(HERE, "akbarak_worker.log"), maxBytes=2_000_000,
                               backupCount=3, encoding="utf-8")):
    _h.setFormatter(_fmt)
    log.addHandler(_h)

session = requests.Session()
if WORKER_KEY:
    session.headers["X-Worker-Key"] = WORKER_KEY

# ─────────────────────── Связь с сервером ───────────────────────


def api(method, path, **kw):
    kw.setdefault("timeout", 15)
    try:
        resp = session.request(method, f"{SERVER}{path}", **kw)
        if resp.status_code == 403:
            log.error("Сервер отказал в доступе: проверьте worker_key.txt")
        return resp
    except Exception as e:
        log.error("Нет связи с сервером (%s): %s", path, e)
        return None


def get_new_orders():
    resp = api("GET", "/pending", params={"terminal": TERMINAL_ID})
    if resp is not None and resp.status_code == 200:
        return resp.json().get("orders", [])
    return []


def report(order_id, action, error=None):
    """action: printing | done | failed. Повторяем, чтобы сервер точно узнал."""
    for attempt in range(3):
        resp = api("POST", f"/{action}/{order_id}", json={"error": error} if error else None)
        if resp is not None and resp.ok:
            return True
        time.sleep(2 * (attempt + 1))
    return False


def printer_is_color():
    """True — цветной принтер, False — чёрно-белый (например, Pantum), None — не удалось узнать.
    Сервер по этому прячет цветную печать на этом терминале."""
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME)
        try:
            port = win32print.GetPrinter(h, 2)["pPortName"]
        finally:
            win32print.ClosePrinter(h)
        return bool(win32print.DeviceCapabilities(PRINTER_NAME, port, 32))   # DC_COLORDEVICE
    except Exception:
        return None


def send_heartbeat(status):
    api("POST", "/heartbeat", json={"terminal": TERMINAL_ID, "printer_status": status,
                                     "color": printer_is_color()}, timeout=10)


# ─────────────────────── Состояние принтера ───────────────────────

PRINTER_FLAGS = [  # флаги Windows PRINTER_STATUS_*
    (0x00000002, "ошибка принтера"),
    (0x00000008, "замялась бумага"),
    (0x00000010, "закончилась бумага"),
    (0x00000040, "нужна бумага (ручная подача)"),
    (0x00000080, "принтер не на связи"),
    (0x00001000, "принтер недоступен"),          # 0x400 — это «печатает», не ошибка
    (0x00000800, "переполнен выходной лоток"),
    (0x00040000, "закончился тонер"),
    (0x00400000, "открыта крышка"),
    (0x00100000, "нужно вмешательство"),
]


def printer_status():
    """'ok' или описание проблемы на русском."""
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME)
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
        problems = [text for flag, text in PRINTER_FLAGS if info["Status"] & flag]
        if info["Attributes"] & 0x00000400:  # PRINTER_ATTRIBUTE_WORK_OFFLINE
            problems.append("в Windows включён режим «Работать автономно»")
        return ", ".join(problems) or "ok"
    except ImportError:
        return "ok"  # pywin32 не установлен — проверить нельзя
    except Exception as e:
        return f"принтер «{PRINTER_NAME}» не найден: {e}"


# ─────────────────────── Файлы ───────────────────────


def download_file(order_id, file_name):
    resp = api("GET", f"/file/{order_id}", timeout=60, stream=True)
    if resp is None or resp.status_code != 200:
        return None
    ext = os.path.splitext(file_name or "")[1].lower() or ".pdf"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    for chunk in resp.iter_content(8192):
        tmp.write(chunk)
    tmp.close()
    return tmp.name


def to_printable(path):
    """PDF и картинки печатаются как есть; DOC/DOCX/TXT переводим в PDF."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".pdf", ".jpg", ".jpeg", ".png"):
        return path
    pdf_path = os.path.splitext(path)[0] + ".pdf"

    # 1) Microsoft Word, если установлен
    try:
        import win32com.client
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            doc = word.Documents.Open(path, ReadOnly=True, ConfirmConversions=False)
            doc.ExportAsFixedFormat(pdf_path, 17)  # 17 = PDF
            doc.Close(False)
        finally:
            word.Quit()
        if os.path.exists(pdf_path):
            return pdf_path
    except Exception as e:
        log.info("Word недоступен (%s), пробую LibreOffice", e)

    # 2) LibreOffice (бесплатный)
    for soffice in LIBREOFFICE_PATHS:
        if os.path.exists(soffice):
            subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir",
                            os.path.dirname(path), path], timeout=120)
            if os.path.exists(pdf_path):
                return pdf_path
    return None


def _page_list(page_range, total):
    """«1-3,5» → [0, 1, 2, 4] (номера с нуля). Пусто — все страницы."""
    if not page_range:
        return list(range(total))
    pages = []
    for part in str(page_range).replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            pages += range(int(a), int(b) + 1)
        elif part:
            pages.append(int(part))
    return [p - 1 for p in pages if 1 <= p <= total]


def make_booklet(pdf_path, page_range=None):
    """Собирает брошюру: на каждом листе А4 (альбомном) по 2 страницы А5 с каждой стороны,
    в таком порядке, чтобы после сгиба пополам получилась книжечка. Возвращает путь к новому PDF."""
    from pypdf import PdfReader, PdfWriter, Transformation
    # pypdf 7: страницы можно менять только если они привязаны к writer —
    # поэтому сначала копируем исходник в отдельный writer
    srcw = PdfWriter(clone_from=PdfReader(pdf_path))
    src = [srcw.pages[i] for i in _page_list(page_range, len(srcw.pages))]
    if not src:
        raise ValueError("нет страниц для брошюры")
    while len(src) % 4:
        src.append(None)              # пустые страницы в конце
    n = len(src)
    W, H = 842, 595                   # А4 альбомный, в пунктах
    half = W / 2

    def place(sheet, page, left):
        if page is None:
            return
        page.transfer_rotation_to_content()
        box = page.mediabox
        pw, ph = float(box.width), float(box.height)
        k = min(half / pw, H / ph)
        x = (0 if left else half) + (half - pw * k) / 2 - float(box.left) * k
        y = (H - ph * k) / 2 - float(box.bottom) * k
        sheet.merge_transformed_page(page, Transformation().scale(k).translate(x, y))

    writer = PdfWriter()
    for i in range(n // 4):
        front = writer.add_blank_page(width=W, height=H)
        place(front, src[n - 1 - 2 * i], True)
        place(front, src[2 * i], False)
        back = writer.add_blank_page(width=W, height=H)
        place(back, src[2 * i + 1], True)
        place(back, src[n - 2 - 2 * i], False)
    out = os.path.splitext(pdf_path)[0] + "_booklet.pdf"
    with open(out, "wb") as f:
        writer.write(f)
    return out


def split_for_manual_duplex(pdf_path, page_range, copies, rotate):
    """Делит PDF на два файла: лицевые стороны и обороты (для ручного переворота).
    Копии раскладываются листами, нечётное число страниц дополняется пустой."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(pdf_path)
    pages = [reader.pages[i] for i in _page_list(page_range, len(reader.pages))]
    if not pages:
        raise ValueError("нет страниц для печати")
    if len(pages) % 2:
        pages.append(None)
    sheets = [(pages[i], pages[i + 1]) for i in range(0, len(pages), 2)] * copies
    fronts, backs = PdfWriter(), PdfWriter()
    w = float(pages[0].mediabox.width)
    h = float(pages[0].mediabox.height)
    for front, _ in sheets:
        fronts.add_page(front) if front is not None else fronts.add_blank_page(w, h)
    order = list(reversed(sheets)) if BACK_REVERSE else sheets
    for _, back in order:
        if back is None:
            backs.add_blank_page(w, h)
        else:
            backs.add_page(back)
            if rotate:
                backs.pages[-1].rotate(rotate)
    base = os.path.splitext(pdf_path)[0]
    paths = (base + "_fronts.pdf", base + "_backs.pdf")
    for writer, path in zip((fronts, backs), paths):
        with open(path, "wb") as f:
            writer.write(f)
    return paths[0], paths[1], len(sheets)


def wait_for_flip(oid, sheets):
    """Ждёт, пока человек перевернёт пачку и нажмёт Enter. True — можно печатать обороты."""
    log.info("=" * 60)
    log.info("📄 Заказ #%s: лицевые стороны напечатаны (%s л.).", oid, sheets)
    log.info("   Возьмите пачку из лотка, НЕ меняя порядок листов, переверните")
    log.info("   и положите в лоток подачи. Затем нажмите Enter в этом окне.")
    log.info("   (ждём %s мин.)", MANUAL_WAIT_MIN)
    log.info("=" * 60)
    print("\a", end="", flush=True)                 # звуковой сигнал
    deadline = time.time() + MANUAL_WAIT_MIN * 60
    try:
        import msvcrt                                # Windows
        while msvcrt.kbhit():
            msvcrt.getwch()                          # убрать старые нажатия
        while time.time() < deadline:
            if msvcrt.kbhit() and msvcrt.getwch() in ("\r", "\n"):
                return True
            time.sleep(0.2)
        return False
    except ImportError:
        try:
            input()
            return True
        except EOFError:
            return False


def print_manual_duplex(oid, pdf_path, page_range, copies, color, booklet):
    """Двусторонняя печать на принтере без дуплекса: лицо → переворот → оборот."""
    rotate = BACK_ROTATE_BOOKLET if booklet else BACK_ROTATE_DUPLEX
    try:
        fronts, backs, sheets = split_for_manual_duplex(pdf_path, page_range, copies, rotate)
    except Exception as e:
        return f"не удалось подготовить двустороннюю печать: {e}"
    try:
        err = print_file(fronts, 1, None, color, "single")
        if err:
            return err
        if not wait_for_flip(oid, sheets):
            return "не перевернули листы вовремя — обороты не напечатаны"
        log.info("Печатаю обороты заказа #%s", oid)
        return print_file(backs, 1, None, color, "single")
    finally:
        _remove(fronts, backs)


def expand_copies(pdf_path, copies, page_range=None, even=False):
    """PDF, в котором весь документ повторён copies раз подряд (1-2-3, 1-2-3…).
    even=True — каждая копия дополняется до чётного числа страниц, чтобы при
    двусторонней печати следующая копия начиналась с нового листа."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(pdf_path)
    pages = [reader.pages[i] for i in _page_list(page_range, len(reader.pages))]
    if not pages:
        raise ValueError("нет страниц для печати")
    writer = PdfWriter()
    box = pages[0].mediabox
    for _ in range(copies):
        for page in pages:
            writer.add_page(page)
        if even and len(pages) % 2:
            writer.add_blank_page(float(box.width), float(box.height))
    out = os.path.splitext(pdf_path)[0] + f"_x{copies}.pdf"
    with open(out, "wb") as f:
        writer.write(f)
    return out


def print_file(path, copies=1, page_range=None, color=False, sides="single"):
    """Возвращает None при успехе или текст ошибки.
    sides: single — с одной стороны; duplex — переворот по длинному краю;
    booklet — уже собранная брошюра, переворот по короткому краю.
    Копии печатаем сами: SumatraPDF и некоторые драйверы игнорируют copies
    (например, для JPG выходила одна копия вместо семи)."""
    if not os.path.exists(SUMATRA):
        return "SumatraPDF не найден"
    copies = max(1, int(copies or 1))
    if copies > 1:
        if path.lower().endswith(".pdf"):
            try:
                many = expand_copies(path, copies, page_range, even=sides != "single")
            except Exception as e:
                return f"не удалось подготовить копии: {e}"
            try:
                return print_file(many, 1, None, color, sides)
            finally:
                _remove(many)
        for n in range(copies):                    # картинка: отдельное задание на каждую копию
            err = print_file(path, 1, page_range, color, sides)
            if err:
                return f"копия {n + 1} из {copies}: {err}"
        return None
    settings = f"copies=1,{'color' if color else 'monochrome'}"
    settings += {"duplex": ",duplexlong", "booklet": ",duplexshort"}.get(sides, ",simplex")
    if page_range:
        settings += f",{page_range}"
    cmd = [SUMATRA, "-print-to", PRINTER_NAME, "-print-settings", settings, "-silent", path]
    for attempt in (1, 2):
        log.info("Печать (попытка %s): %s", attempt, " ".join(cmd))
        try:
            result = subprocess.run(cmd, timeout=180)
        except subprocess.TimeoutExpired:
            return "SumatraPDF не ответил за 3 минуты"
        if result.returncode == 0:
            return None
        log.warning("SumatraPDF вернул код %s", result.returncode)
        time.sleep(3)
    st = printer_status()
    why = f" Windows сообщает: {st}." if st != "ok" else ""
    return (f"Windows не принял задание на принтер «{PRINTER_NAME}» (SumatraPDF, код {result.returncode}).{why} "
            "Проверьте: принтер включён, в Windows «Готов», пробная страница печатается.")


def _remove(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


# ─────────────────────── Сканер ───────────────────────


SCAN_PAGE_MAX_MB = 3          # один лист скана
SCAN_UPLOAD_MAX_MB = 22       # весь скан (сервер принимает до 25 МБ)


def _wia_set(item, prop_id, name, value):
    """Ставит настройку сканера по номеру (не зависит от языка Windows), иначе по имени."""
    for key in (prop_id, name):
        try:
            item.Properties(key).Value = value
            return True
        except Exception:
            pass
    log.warning("Сканер не принял настройку %s = %s", name, value)
    return False


def scan_to_jpeg(order_id, dpi, color=False, sheet=1):
    """Сканирует лист со стекла (цвет или оттенки серого). Возвращает путь к JPEG или None."""
    try:
        import win32com.client
        from PIL import Image

        dm = win32com.client.Dispatch("WIA.DeviceManager")
        # Сканеров в Windows может быть несколько (старый Pantum остаётся в системе,
        # даже когда отключён). Сначала пробуем сканер той же марки, что принтер
        # по умолчанию, потом остальные; отключённые («не в режиме ONLINE») пропускаем.
        infos = []
        for i in range(dm.DeviceInfos.Count):
            info = dm.DeviceInfos.Item(i + 1)
            if info.Type == 1:
                try:
                    name = str(info.Properties("Name").Value)
                except Exception:
                    name = ""
                infos.append((name, info))
        brand = (PRINTER_NAME.split() or [""])[0].lower()
        infos.sort(key=lambda t: 0 if brand and brand in t[0].lower() else 1)
        scanner = None
        for name, info in infos:
            try:
                scanner = info.Connect()
                log.info("Сканер: %s", name or "без имени")
                break
            except Exception as e:
                log.warning("Сканер «%s» недоступен, пробую следующий: %s", name, e)
        if not scanner:
            log.error("Сканер не найден! Проверьте, что принтер со сканером включён и подключён по USB.")
            return None

        item = scanner.Items(1)
        # Порядок важен: смена режима сбрасывает разрешение, поэтому сначала режим, потом dpi.
        # Режимы WIA: 1 — цвет, 2 — оттенки серого, 4 — чёрно-белый текст (1 бит, плохо для документов).
        _wia_set(item, "6146", "Current Intent", 1 if color else 2)
        _wia_set(item, "4103", "Data Type", 3 if color else 2)        # 3 — цвет, 2 — серый
        _wia_set(item, "4104", "Bits Per Pixel", 24 if color else 8)
        _wia_set(item, "6147", "Horizontal Resolution", dpi)
        _wia_set(item, "6148", "Vertical Resolution", dpi)

        image = item.Transfer("{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}")
        tmp_path = os.path.join(tempfile.gettempdir(), f"scan_{order_id}_{sheet}.jpg")
        _remove(tmp_path)
        image.SaveFile(tmp_path)
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            return None

        img = Image.open(tmp_path).convert("RGB" if color else "L")
        # Хорошее качество; жмём сильнее, только если лист вышел очень тяжёлым
        quality = 90
        while True:
            img.save(tmp_path, "JPEG", quality=quality, dpi=(dpi, dpi))
            if os.path.getsize(tmp_path) <= SCAN_PAGE_MAX_MB * 1024 * 1024 or quality <= 70:
                break
            quality -= 10
        return tmp_path
    except Exception as e:
        log.exception("Ошибка сканирования: %s", e)
        return None


# ─────────────────────── Обработка заказов ───────────────────────

finished = set()  # заказы, которые уже выполнены, но сервер мог не узнать об этом


def kind_of(order):
    t = order.get("order_type")
    if t:
        return t
    name = order.get("file_name") or ""
    return name if name in ("scan", "copy") else "print"


def wait_next_sheet(oid, sheet):
    """Ждёт, пока студент положит лист № sheet и нажмёт кнопку на экране или в телефоне."""
    deadline = time.time() + SCAN_WAIT_MIN * 60
    log.info("Заказ #%s: ждём лист %s (кнопка «Сканировать» на экране или в телефоне)", oid, sheet)
    while time.time() < deadline:
        resp = api("GET", f"/scan_go/{oid}", timeout=10)
        if resp is not None and resp.ok:
            d = resp.json()
            if d.get("status") not in (None, "printing"):
                return False                          # заказ отменили
            if int(d.get("go") or 1) >= sheet:
                return True
        time.sleep(1.5)
    return False


def scan_sheets(oid, sheets, dpi, color):
    """Сканирует листы по одному. Возвращает список путей к JPEG (может быть меньше sheets,
    если студент ушёл, не положив следующий лист)."""
    paths = []
    for sheet in range(1, sheets + 1):
        if sheet > 1:
            api("POST", f"/scan_progress/{oid}", json={"done": sheet - 1}, timeout=10)
            if not wait_next_sheet(oid, sheet):
                log.warning("Заказ #%s: следующий лист не положили — отсканировано %s из %s",
                            oid, len(paths), sheets)
                break
        path = scan_to_jpeg(oid, dpi, color, sheet)
        if not path:
            break
        paths.append(path)
        log.info("Заказ #%s: лист %s из %s отсканирован", oid, sheet, sheets)
    api("POST", f"/scan_progress/{oid}", json={"done": len(paths)}, timeout=10)
    return paths


def images_to_pdf(paths, out_path, dpi, repeat=1, max_mb=None):
    """Склеивает сканы в один PDF. repeat — сколько раз повторить весь комплект (для копий).
    max_mb — если файл вышел тяжелее, пересобираем с меньшим размером (для загрузки на сервер)."""
    from PIL import Image
    base = [Image.open(p) for p in paths]
    base = [im if im.mode in ("RGB", "L") else im.convert("RGB") for im in base]
    scale, quality = 1.0, 88
    while True:
        images = base if scale == 1.0 else [im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
                                            for im in base]
        images = images * repeat
        images[0].save(out_path, "PDF", save_all=True, append_images=images[1:],
                       resolution=dpi * scale, quality=quality)
        if not max_mb or os.path.getsize(out_path) <= max_mb * 1024 * 1024 or scale < 0.4:
            return out_path
        scale, quality = scale * 0.75, 75


def _span(prof, lo, hi, gap):
    """Самый длинный непрерывный участок профиля, где «чернила» есть, но это не сплошная полоса."""
    best, start, last = None, None, None
    for i, v in enumerate(prof):
        if lo < v < hi:
            if start is None or i - last > gap:
                start = i
            last = i
            if best is None or last - start > best[1] - best[0]:
                best = (start, last)
    return best


def _crop_document(img):
    """Находит документ (карту) на белом фоне скана и вырезает его.
    Тёмные полосы по краям стекла и тени крышки не мешают: они тянутся через весь лист,
    а карта — нет. Если карту найти не удалось, возвращает None."""
    from PIL import Image, ImageFilter, ImageOps
    k = 4
    small = ImageOps.grayscale(img).resize((img.width // k, img.height // k)).filter(ImageFilter.MedianFilter(5))
    mask = small.point(lambda v: 255 if v < 238 else 0)
    w, h = mask.size

    def only(box):                                        # маска, где оставлен только участок box
        m = Image.new("L", (w, h), 0)
        m.paste(mask.crop(box), box[:2])
        return m

    def cols(m):
        return list(m.resize((w, 1), Image.BOX).getdata())

    def rows(m):
        return list(m.resize((1, h), Image.BOX).getdata())

    lo, hi = 0.03 * 255, 0.75 * 255
    xs = _span(cols(mask), lo, hi, w // 30)
    if not xs:
        return None
    ys = _span(rows(only((xs[0], 0, xs[1] + 1, h))), 0.02 * 255, hi, h // 30)
    if not ys:
        return None
    xs2 = _span(cols(only((0, ys[0], w, ys[1] + 1))), 0.01 * 255, hi, w // 30)
    if xs2:
        xs = xs2
    bw, bh = xs[1] - xs[0], ys[1] - ys[0]
    if bw < w * 0.12 or bh < h * 0.06 or (bw > w * 0.9 and bh > h * 0.9):
        return None
    pad = 3
    box = (max(0, (xs[0] - pad) * k), max(0, (ys[0] - pad) * k),
           min(img.width, (xs[1] + pad) * k), min(img.height, (ys[1] + pad) * k))
    return img.crop(box)


def compose_idcard(paths, dpi, color, out_path):
    """Лицевая и обратная сторона на одном листе А4: в верхней части, одна под другой,
    в натуральную величину, отступ сверху 2 см, между сторонами 1 см."""
    from PIL import Image
    mode = "RGB" if color else "L"
    cm = dpi / 2.54
    page_w, page_h = int(21.0 * cm), int(29.7 * cm)       # А4
    sheet = Image.new(mode, (page_w, page_h), "white")
    top, gap, side_margin = int(2 * cm), int(1 * cm), int(1.5 * cm)
    slot_h = (page_h - 2 * top - gap) // 2
    y = top
    for p in paths[:2]:
        src = Image.open(p).convert(mode)
        side = _crop_document(src)
        if side is None:
            log.warning("ID-карта: не нашёл края документа на скане — кладу скан целиком")
            side = src
        f = min(1.0, (page_w - 2 * side_margin) / side.width, slot_h / side.height)
        if f < 1.0:
            side = side.resize((int(side.width * f), int(side.height * f)), Image.LANCZOS)
        sheet.paste(side, ((page_w - side.width) // 2, y))
        y += side.height + gap
    sheet.save(out_path, "JPEG", quality=90, dpi=(dpi, dpi))
    return out_path


def handle_scan(oid, order):
    sheets = int(order.get("pages") or 1)
    color = order.get("print_type") == "color"
    if order.get("sides") == "idcard":
        paths = scan_sheets(oid, 2, SCAN_DPI, color)
        if not paths:
            return "не удалось отсканировать"
        sheet = compose_idcard(paths, SCAN_DPI, color, os.path.join(tempfile.gettempdir(), f"id_{oid}.jpg"))
        pdf = images_to_pdf([sheet], os.path.join(tempfile.gettempdir(), f"scan_{oid}.pdf"), SCAN_DPI,
                            max_mb=SCAN_UPLOAD_MAX_MB)
        try:
            with open(pdf, "rb") as f:
                resp = api("POST", f"/scan_upload/{oid}", files={"file": f}, timeout=180)
            if resp is None or not resp.ok:
                return "не удалось загрузить скан на сервер"
            return None
        finally:
            _remove(*paths, sheet, pdf)
    paths = scan_sheets(oid, sheets, SCAN_DPI, color)
    if not paths:
        return "не удалось отсканировать"
    upload = paths[0]
    if len(paths) > 1:
        upload = images_to_pdf(paths, os.path.join(tempfile.gettempdir(), f"scan_{oid}.pdf"), SCAN_DPI,
                               max_mb=SCAN_UPLOAD_MAX_MB)
    try:
        with open(upload, "rb") as f:
            resp = api("POST", f"/scan_upload/{oid}", files={"file": f}, timeout=180)
        if resp is None or not resp.ok:
            return "не удалось загрузить скан на сервер"
        return None
    finally:
        _remove(*paths, upload if upload not in paths else None)


def handle_copy(oid, order):
    sheets = int(order.get("pages") or 1)
    copies = int(order.get("copies") or 1)
    color = order.get("print_type") == "color"
    if order.get("sides") == "idcard":
        paths = scan_sheets(oid, 2, COPY_DPI, color)
        if not paths:
            return "не удалось отсканировать документ"
        sheet = compose_idcard(paths, COPY_DPI, color, os.path.join(tempfile.gettempdir(), f"id_{oid}.jpg"))
        try:
            return print_file(sheet, copies, color=color)
        finally:
            _remove(*paths, sheet)
    paths = scan_sheets(oid, sheets, COPY_DPI, color)
    if not paths:
        return "не удалось отсканировать оригинал"
    pdf = None
    try:
        if len(paths) == 1:
            return print_file(paths[0], copies, color=color)
        # весь комплект по порядку, столько раз, сколько копий
        pdf = images_to_pdf(paths, os.path.join(tempfile.gettempdir(), f"copy_{oid}.pdf"), COPY_DPI, copies)
        return print_file(pdf, 1, color=color)
    finally:
        _remove(*paths, pdf)


def handle_print(oid, order):
    src = download_file(oid, order.get("file_name"))
    if not src:
        return "не удалось скачать файл с сервера"
    printable = booklet = None
    color = order.get("print_type") == "color"
    sides = order.get("sides") or "single"
    copies = int(order.get("copies") or 1)
    try:
        printable = to_printable(src)
        if not printable:
            return "не удалось открыть DOC/DOCX: установите Word или LibreOffice"
        if not printable.lower().endswith(".pdf"):
            sides = "single"              # картинка — одна страница
        if sides == "booklet":
            try:
                booklet = make_booklet(printable, order.get("page_range"))
            except Exception as e:
                return f"не удалось собрать брошюру: {e}"
            if MANUAL_DUPLEX:
                return print_manual_duplex(oid, booklet, None, copies, color, True)
            return print_file(booklet, copies, None, color, "booklet")
        if sides == "duplex" and MANUAL_DUPLEX:
            return print_manual_duplex(oid, printable, order.get("page_range"), copies, color, False)
        return print_file(printable, copies, order.get("page_range"), color, sides)
    finally:
        _remove(src, printable if printable != src else None, booklet)


def process(order):
    oid = order["id"]
    refresh_printer()
    if oid in finished:  # уже напечатан, просто сообщаем серверу ещё раз
        if report(oid, "done"):
            finished.discard(oid)
        return

    kind = kind_of(order)
    log.info("Заказ #%s: %s, %s, %s коп., %s сом", oid, kind, order.get("file_name"),
             order.get("copies"), order.get("total"))
    report(oid, "printing")

    if kind == "scan":
        error = handle_scan(oid, order)
    elif kind == "copy":
        error = handle_copy(oid, order)
    else:
        error = handle_print(oid, order)

    if error:
        log.error("❌ Заказ #%s: %s", oid, error)
        report(oid, "failed", error)
    else:
        log.info("✅ Заказ #%s выполнен", oid)
        if kind != "scan":  # скан сервер отмечает сам при загрузке
            finished.add(oid)
            if report(oid, "done"):
                finished.discard(oid)


def main():
    log.info("🖨 АкБарак авто-печать запущена (версия %s). Терминал: %s, принтер: %s", VERSION, TERMINAL_ID, PRINTER_NAME)
    log.info("SumatraPDF: %s", "найден" if os.path.exists(SUMATRA) else "НЕ НАЙДЕН!")
    log.info("Ключ терминала: %s", "задан" if WORKER_KEY else "не задан")
    c = printer_is_color()
    log.info("Цветной принтер: %s", {True: "да", False: "НЕТ — цветная печать на этом терминале скрыта", None: "не удалось узнать"}[c])

    last_beat, last_status = 0, None
    while True:
        try:
            status = printer_status()
            if time.time() - last_beat > HEARTBEAT_INTERVAL or status != last_status:
                refresh_printer()
                send_heartbeat(status)
                last_beat, last_status = time.time(), status

            if status != "ok":
                # Принтер не готов — заказы не берём, они спокойно ждут в очереди.
                log.warning("Принтер не готов: %s", status)
                get_new_orders()  # отмечаемся на сервере, что терминал жив
            else:
                for order in get_new_orders():
                    process(order)
        except Exception as e:
            log.exception("Непредвиденная ошибка: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

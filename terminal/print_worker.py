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
PRINTER_NAME = "Pantum M6500W series"
SUMATRA = r"C:\Users\user\AppData\Local\SumatraPDF\SumatraPDF.exe"
TERMINAL_ID = os.getenv("AKBARAK_TERMINAL", "kgma-1")
CHECK_INTERVAL = 5          # секунд между проверками очереди
HEARTBEAT_INTERVAL = 30     # секунд между отчётами о состоянии принтера
SCAN_DPI = 150

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
COPY_DPI = 200

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


def send_heartbeat(status):
    api("POST", "/heartbeat", json={"terminal": TERMINAL_ID, "printer_status": status}, timeout=10)


# ─────────────────────── Состояние принтера ───────────────────────

PRINTER_FLAGS = [  # флаги Windows PRINTER_STATUS_*
    (0x00000002, "ошибка принтера"),
    (0x00000008, "замялась бумага"),
    (0x00000010, "закончилась бумага"),
    (0x00000040, "нужна бумага (ручная подача)"),
    (0x00000080, "принтер не на связи"),
    (0x00000400, "принтер выключен или не на связи"),
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
    from pypdf import PdfReader, PdfWriter, PageObject, Transformation
    reader = PdfReader(pdf_path)
    src = [reader.pages[i] for i in _page_list(page_range, len(reader.pages))]
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
        front = PageObject.create_blank_page(width=W, height=H)
        place(front, src[n - 1 - 2 * i], True)
        place(front, src[2 * i], False)
        back = PageObject.create_blank_page(width=W, height=H)
        place(back, src[2 * i + 1], True)
        place(back, src[n - 2 - 2 * i], False)
        writer.add_page(front)
        writer.add_page(back)
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


def print_file(path, copies=1, page_range=None, color=False, sides="single"):
    """Возвращает None при успехе или текст ошибки.
    sides: single — с одной стороны; duplex — переворот по длинному краю;
    booklet — уже собранная брошюра, переворот по короткому краю."""
    if not os.path.exists(SUMATRA):
        return "SumatraPDF не найден"
    settings = f"copies={copies},{'color' if color else 'monochrome'}"
    settings += {"duplex": ",duplexlong", "booklet": ",duplexshort"}.get(sides, ",simplex")
    if page_range:
        settings += f",{page_range}"
    cmd = [SUMATRA, "-print-to", PRINTER_NAME, "-print-settings", settings, "-silent", path]
    log.info("Печать: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=180)
    except subprocess.TimeoutExpired:
        return "SumatraPDF не ответил за 3 минуты"
    return None if result.returncode == 0 else f"SumatraPDF вернул код {result.returncode}"


def _remove(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


# ─────────────────────── Сканер ───────────────────────


def scan_to_jpeg(order_id, dpi):
    """Сканирует лист со стекла. Возвращает путь к JPEG (до 1 МБ) или None."""
    try:
        import win32com.client
        from PIL import Image

        dm = win32com.client.Dispatch("WIA.DeviceManager")
        scanner = None
        for i in range(dm.DeviceInfos.Count):
            info = dm.DeviceInfos.Item(i + 1)
            if info.Type == 1:
                scanner = info.Connect()
                break
        if not scanner:
            log.error("Сканер не найден!")
            return None

        item = scanner.Items(1)
        try:
            item.Properties("Horizontal Resolution").Value = dpi
            item.Properties("Vertical Resolution").Value = dpi
            item.Properties("Current Intent").Value = 4  # оттенки серого
        except Exception as e:
            log.warning("Настройки сканера: %s", e)

        image = item.Transfer("{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}")
        tmp_path = os.path.join(tempfile.gettempdir(), f"scan_{order_id}.jpg")
        _remove(tmp_path)
        image.SaveFile(tmp_path)
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            return None

        img = Image.open(tmp_path).convert("RGB")
        quality = 85
        while quality > 20:
            img.save(tmp_path, "JPEG", quality=quality)
            if os.path.getsize(tmp_path) <= 1024 * 1024:
                break
            quality -= 15
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


def handle_scan(oid):
    path = scan_to_jpeg(oid, SCAN_DPI)
    if not path:
        return "не удалось отсканировать"
    try:
        with open(path, "rb") as f:
            resp = api("POST", f"/scan_upload/{oid}", files={"file": f}, timeout=120)
        if resp is None or not resp.ok:
            return "не удалось загрузить скан на сервер"
        return None
    finally:
        _remove(path)


def handle_copy(oid, copies, color=False):
    path = scan_to_jpeg(oid, COPY_DPI)
    if not path:
        return "не удалось отсканировать оригинал"
    try:
        return print_file(path, copies, color=color)
    finally:
        _remove(path)


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
    if oid in finished:  # уже напечатан, просто сообщаем серверу ещё раз
        if report(oid, "done"):
            finished.discard(oid)
        return

    kind = kind_of(order)
    log.info("Заказ #%s: %s, %s, %s коп., %s сом", oid, kind, order.get("file_name"),
             order.get("copies"), order.get("total"))
    report(oid, "printing")

    if kind == "scan":
        error = handle_scan(oid)
    elif kind == "copy":
        error = handle_copy(oid, int(order.get("copies") or 1), order.get("print_type") == "color")
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
    log.info("🖨 АкБарак авто-печать запущена. Терминал: %s, принтер: %s", TERMINAL_ID, PRINTER_NAME)
    log.info("SumatraPDF: %s", "найден" if os.path.exists(SUMATRA) else "НЕ НАЙДЕН!")
    log.info("Ключ терминала: %s", "задан" if WORKER_KEY else "не задан")

    last_beat, last_status = 0, None
    while True:
        try:
            status = printer_status()
            if time.time() - last_beat > HEARTBEAT_INTERVAL or status != last_status:
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

"""
АкБарак — авто-печать на терминале.
Запускается на компьютере с принтером (лучше через start_worker.bat).
Каждые 5 секунд забирает оплаченные заказы с сервера и печатает.

Настройка (один раз):
  1. pip install requests pywin32 pillow
  2. Рядом с этим файлом создайте файл worker_key.txt и вставьте в него
     значение WORKER_KEY из Railway (если ключ на сервере ещё не задан — файл не нужен).
  3. Для печати DOC/DOCX/TXT нужен Microsoft Word или бесплатный LibreOffice.
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


# ─────────────────────── Очередь печати Windows ───────────────────────
# SumatraPDF возвращает «успех», как только отдал задание в очередь Windows.
# Многие принтеры (в том числе Pantum) не сообщают Windows, что кончилась бумага:
# задание просто висит в очереди. Поэтому после отправки следим за самим заданием
# и считаем заказ выполненным только когда задание ушло из очереди.

JOB_WAIT_MAX = 10 * 60      # сколько ждать, пока задание напечатается (сек)
JOB_PROBLEM_FLAGS = [       # флаги Windows JOB_STATUS_*
    (0x00000002, "ошибка"),
    (0x00000020, "принтер не на связи"),
    (0x00000040, "нет бумаги"),
    (0x00000200, "задание заблокировано"),
    (0x00000400, "нужно вмешательство"),
]


def _open_printer():
    import win32print
    return win32print.OpenPrinter(PRINTER_NAME)


def queue_jobs():
    """Список заданий в очереди Windows или None, если проверить нельзя."""
    try:
        import win32print
        h = _open_printer()
        try:
            return list(win32print.EnumJobs(h, 0, 999, 1))
        finally:
            win32print.ClosePrinter(h)
    except ImportError:
        return None
    except Exception as e:
        log.warning("Не удалось прочитать очередь печати: %s", e)
        return None


def job_problem(job):
    return ", ".join(text for flag, text in JOB_PROBLEM_FLAGS if job.get("Status", 0) & flag)


def cancel_jobs(job_ids):
    try:
        import win32print
        h = _open_printer()
        try:
            for jid in job_ids:
                try:
                    win32print.SetJob(h, jid, 0, None, win32print.JOB_CONTROL_DELETE)
                except Exception as e:
                    log.warning("Не удалось удалить задание %s: %s", jid, e)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        log.warning("Не удалось открыть очередь для удаления: %s", e)


def queue_problem():
    """Описание проблемы, если в очереди Windows висят старые или ошибочные задания."""
    jobs = queue_jobs()
    if not jobs:
        return None
    bad = [job_problem(j) for j in jobs if job_problem(j)]
    if bad:
        return f"в очереди Windows задание с ошибкой ({bad[0]})"
    return f"в очереди Windows висит заданий: {len(jobs)}"


def wait_for_jobs(before_ids, doc_name):
    """Ждём, пока наше задание напечатается. None — успех, иначе текст ошибки."""
    # 1) находим своё задание (новое по сравнению со снимком до печати)
    ours = []
    for _ in range(10):
        jobs = queue_jobs()
        if jobs is None:
            return None  # pywin32 нет — проверить нельзя, верим SumatraPDF
        ours = [j["JobId"] for j in jobs
                if j["JobId"] not in before_ids or doc_name in (j.get("pDocument") or "")]
        if ours:
            break
        time.sleep(0.5)
    if not ours:
        return None  # задание сразу ушло в принтер

    # 2) ждём, пока оно уйдёт из очереди
    start, last_problem = time.time(), None
    while time.time() - start < JOB_WAIT_MAX:
        jobs = {j["JobId"]: j for j in (queue_jobs() or [])}
        left = [jid for jid in ours if jid in jobs]
        if not left:
            return None
        problem = job_problem(jobs[left[0]]) or printer_status()
        if problem == "ok":
            problem = None
        waited = int(time.time() - start)
        if problem != last_problem or (waited and waited % 60 < 2):
            msg = problem or "задание ещё в очереди"
            log.warning("⏳ Ждём печать (%s с): %s", waited, msg)
            send_heartbeat(f"задание ждёт: {msg}")
            last_problem = problem
        time.sleep(2)

    cancel_jobs(ours)
    return f"не напечатано за {JOB_WAIT_MAX // 60} мин (нет бумаги или принтер не отвечает), задание снято"


def print_file(path, copies=1, page_range=None):
    """Возвращает None при успехе или текст ошибки."""
    if not os.path.exists(SUMATRA):
        return "SumatraPDF не найден"
    settings = f"copies={copies},monochrome"
    if page_range:
        settings += f",{page_range}"
    cmd = [SUMATRA, "-print-to", PRINTER_NAME, "-print-settings", settings, "-silent", path]
    before_ids = {j["JobId"] for j in (queue_jobs() or [])}
    log.info("Печать: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=180)
    except subprocess.TimeoutExpired:
        return "SumatraPDF не ответил за 3 минуты"
    if result.returncode != 0:
        return f"SumatraPDF вернул код {result.returncode}"
    return wait_for_jobs(before_ids, os.path.basename(path))


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


def handle_copy(oid, copies):
    path = scan_to_jpeg(oid, COPY_DPI)
    if not path:
        return "не удалось отсканировать оригинал"
    try:
        return print_file(path, copies)
    finally:
        _remove(path)


def handle_print(oid, order):
    src = download_file(oid, order.get("file_name"))
    if not src:
        return "не удалось скачать файл с сервера"
    printable = None
    try:
        printable = to_printable(src)
        if not printable:
            return "не удалось открыть DOC/DOCX: установите Word или LibreOffice"
        return print_file(printable, int(order.get("copies") or 1), order.get("page_range"))
    finally:
        _remove(src, printable if printable != src else None)


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
        error = handle_copy(oid, int(order.get("copies") or 1))
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
            if status == "ok":
                status = queue_problem() or "ok"
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

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
     Умеет ли принтер печатать с двух сторон сам, программа узнаёт у Windows. Если не умеет —
     печатаются лицевые стороны, вы переворачиваете пачку и жмёте Enter.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
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
        log.info("Двусторонняя печать: %s", "вручную" if manual_duplex() else "автоматически — принтер сам")
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
VERSION = "2026-11-22 v26 LibreOffice ищется в любой папке"   # видно в окне при запуске — так проверяете, что стоит новая версия
SCAN_DPI = 200          # 200 dpi: текст чёткий, а сканирует почти вдвое быстрее, чем 300
SCAN_WAIT_MIN = 5            # сколько ждать, пока студент положит следующий лист

# Двусторонняя печать. Программа сама спрашивает у Windows, умеет ли принтер печатать
# с двух сторон (Canon G5080 — умеет, Pantum M6500W — нет):
#   умеет   → принтер сам переворачивает лист (двусторонняя и брошюра печатаются сразу);
#   не умеет → печатаются лицевые стороны, вы переворачиваете пачку и жмёте Enter.
# Принудительно: переменная окружения AKBARAK_MANUAL_DUPLEX=1 (ручной) или =0 (автоматический).
MANUAL_DUPLEX = None          # None — определить автоматически
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


def find_soffice():
    """Все найденные soffice.exe: стандартные папки, любые «LibreOffice*» в Program Files,
    путь из реестра (App Paths) и портативная версия рядом с программой."""
    import glob
    found = list(LIBREOFFICE_PATHS)
    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
               os.environ.get("LOCALAPPDATA", ""), HERE, r"C:\akbarak"):
        if pf:
            found += glob.glob(os.path.join(pf, "LibreOffice*", "program", "soffice.exe"))
            found += glob.glob(os.path.join(pf, "LibreOffice*", "App", "libreoffice", "program", "soffice.exe"))
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe") as k:
                    found.append(winreg.QueryValue(k, None))
            except OSError:
                pass
    except ImportError:
        pass
    out = []
    for f in found:
        f = (f or "").strip('"')
        if f and f not in out and os.path.exists(f):
            out.append(f)
    return out


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


def get_new_orders(wait=0):
    """wait>0 — сервер держит запрос до wait секунд и отвечает сразу, как появился заказ."""
    resp = api("GET", "/pending", params={"terminal": TERMINAL_ID, "wait": wait}, timeout=wait + 15)
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


def printer_has_duplex():
    """True — принтер сам печатает с двух сторон, False — нет, None — не удалось узнать."""
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME)
        try:
            port = win32print.GetPrinter(h, 2)["pPortName"]
        finally:
            win32print.ClosePrinter(h)
        return bool(win32print.DeviceCapabilities(PRINTER_NAME, port, 7))    # DC_DUPLEX
    except Exception:
        return None


# ── Лотки: обычная A4 — нижняя кассета, фотобумага — задний лоток ──────────
# Слова, по которым узнаём лоток в списке драйвера (английский и русский драйвер Canon).
PLAIN_BIN_WORDS = ("cassette", "кассет")
PHOTO_BIN_WORDS = ("rear", "задн", "тыл")
# Можно задать вручную точным именем лотка: AKBARAK_PLAIN_BIN / AKBARAK_PHOTO_BIN.
# Отдельный «фото-принтер» в Windows (копия того же Canon с настройками
# «Задний лоток + глянцевая фотобумага») находится сам по слову «фото»/«photo»
# в имени, или задаётся точно: AKBARAK_PHOTO_PRINTER.


def printer_bins(name):
    """Названия лотков принтера, как их отдаёт драйвер."""
    try:
        import win32print
        h = win32print.OpenPrinter(name)
        try:
            port = win32print.GetPrinter(h, 2)["pPortName"]
        finally:
            win32print.ClosePrinter(h)
        return [str(b).strip("\x00 ") for b in (win32print.DeviceCapabilities(name, port, 12) or [])]  # DC_BINNAMES
    except Exception:
        return []


def pick_bin(name, words, env_key):
    forced = os.getenv(env_key)
    if forced:
        return forced
    for b in printer_bins(name):
        if any(w in b.lower() for w in words):
            return b
    return None


def photo_printer():
    """Отдельно настроенный в Windows принтер для фото, если он есть."""
    forced = os.getenv("AKBARAK_PHOTO_PRINTER")
    if forced:
        return forced
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for p in win32print.EnumPrinters(flags, None, 2):
            n = p["pPrinterName"]
            if n != PRINTER_NAME and ("фото" in n.lower() or "photo" in n.lower()):
                return n
    except Exception:
        pass
    return None


def photo_target():
    """(принтер, лоток) для фото: отдельный фото-принтер или задний лоток основного."""
    p = photo_printer()
    if p:
        return p, pick_bin(p, PHOTO_BIN_WORDS, "AKBARAK_PHOTO_BIN")
    return PRINTER_NAME, pick_bin(PRINTER_NAME, PHOTO_BIN_WORDS, "AKBARAK_PHOTO_BIN")


def manual_duplex():
    """Нужно ли переворачивать листы вручную для двусторонней печати и брошюры."""
    env = os.getenv("AKBARAK_MANUAL_DUPLEX")
    if env in ("0", "1"):
        return env == "1"
    if MANUAL_DUPLEX is not None:
        return MANUAL_DUPLEX
    return printer_has_duplex() is not True


CMD_RESULT = None


def send_heartbeat(status):
    global CMD_RESULT
    body = {"terminal": TERMINAL_ID, "printer_status": status, "color": printer_is_color()}
    if CMD_RESULT:
        body["cmd_result"] = CMD_RESULT
    resp = api("POST", "/heartbeat", json=body, timeout=10)
    if resp is None or resp.status_code != 200:
        return
    CMD_RESULT = None
    try:
        cmd = resp.json().get("cmd")
    except Exception:
        cmd = None
    if cmd in ("kiosk_off", "kiosk_on"):
        CMD_RESULT = kiosk_control(cmd)
    elif cmd:
        CMD_RESULT = printer_service(cmd)


KIOSK_OFF_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiosk_off.txt")


def kiosk_control(cmd):
    """Свернуть/вернуть экран терминала по кнопке в админке.
    start_kiosk.bat сам перезапускает экран, если его закрыть (студент не выйдет на рабочий стол),
    но пока лежит kiosk_off.txt — не перезапускает."""
    try:
        if cmd == "kiosk_off":
            with open(KIOSK_OFF_FLAG, "w", encoding="utf-8") as f:
                f.write("Экран свёрнут из админки. Удалите этот файл или нажмите «Вернуть экран».\n")
            for exe in ("chrome.exe", "msedge.exe"):
                subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
            log.info("Экран терминала свёрнут из админки")
            return "✅ экран свёрнут — открыт рабочий стол"
        if os.path.exists(KIOSK_OFF_FLAG):
            os.remove(KIOSK_OFF_FLAG)
        log.info("Экран терминала возвращён из админки")
        return "✅ экран вернётся в течение 5 секунд"
    except Exception as e:
        log.warning("Команда экрана %s не выполнена: %s", cmd, e)
        return f"❌ не получилось: {e}"


# ─────────────────────── Обслуживание Epson: проверка и прочистка дюз ───────────────────────
# Те же служебные команды, что шлёт утилита Epson (формат ESC/P «remote mode», как в escputil).
# Отправляются «сырым» заданием в очередь принтера. Работает на струйных Epson (L-серия).

def _epson_remote(cmd: bytes) -> bytes:
    init = b"\x00\x00\x00\x1b\x01@EJL 1284.4\n@EJL     \n\x1b@\x1b@"
    return init + b"\x1b(R\x08\x00\x00REMOTE1" + cmd + b"\x1b\x00\x00\x00"


EPSON_CMDS = {
    "nozzle": ("проверка дюз", b"NC\x02\x00\x00\x00"),   # печатает тестовый шаблон
    "clean": ("прочистка дюз", b"CH\x02\x00\x00\x00"),    # цикл прочистки головки (~1 мин)
}


def printer_service(cmd):
    name, code = EPSON_CMDS.get(cmd, (cmd, None))
    if code is None:
        return f"❌ неизвестная команда {cmd}"
    if "EPSON" not in (PRINTER_NAME or "").upper():
        log.warning("%s: принтер %s не Epson — команда не поддерживается", name, PRINTER_NAME)
        return f"❌ {name}: только для Epson (сейчас {PRINTER_NAME})"
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME)
        try:
            win32print.StartDocPrinter(h, 1, (f"AkBarak {name}", None, "RAW"))
            try:
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, _epson_remote(code))
                win32print.EndPagePrinter(h)
            finally:
                win32print.EndDocPrinter(h)
        finally:
            win32print.ClosePrinter(h)
        log.info("🧽 %s: команда отправлена на %s", name, PRINTER_NAME)
        return f"✅ {name}: отправлено на принтер " + time.strftime("%H:%M")
    except Exception as e:
        log.error("%s: %s", name, e)
        return f"❌ {name}: {str(e)[:120]}"


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


_LAST_UNOFFLINE = [0.0]
_OFFLINE_LOGGED = [0.0]


def clear_work_offline():
    """Windows сама ставит принтеру «Работать автономно» (часто у USB-принтеров на стандартном
    драйвере, в названии которых «HTTP»), хотя принтер подключён и печатает.
    Снимаем эту галочку сами, не чаще раза в минуту. True — получилось."""
    if time.time() - _LAST_UNOFFLINE[0] < 10:
        return False
    _LAST_UNOFFLINE[0] = time.time()
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
        try:
            info = win32print.GetPrinter(h, 2)
            if not info["Attributes"] & 0x00000400:
                return True
            info["Attributes"] &= ~0x00000400
            info["pSecurityDescriptor"] = None
            win32print.SetPrinter(h, 2, info, 0)
        finally:
            win32print.ClosePrinter(h)
        if time.time() - _OFFLINE_LOGGED[0] > 600:
            _OFFLINE_LOGGED[0] = time.time()
            log.info("Снял с принтера «%s» режим «Работать автономно»", PRINTER_NAME)
        return True
    except Exception as e:
        log.warning("Не удалось снять «Работать автономно»: %s (снимите галочку вручную: "
                    "Принтеры и сканеры → принтер → Открыть очередь → меню «Принтер»)", e)
        return False


STATUS_HOLD = 45   # секунд: столько проблема должна держаться, чтобы считаться настоящей
_PROBLEM_SINCE = {"status": None, "t": 0.0}


def stable_printer_status():
    """Как printer_status(), но мигающие ошибки не считаются.
    Драйвер Canon на пару секунд выставляет «ошибка принтера» (прогрев, проверка чернил,
    выход из сна) и сразу снимает — из-за этого было «ошибка / готов / ошибка» в окне и в Telegram.
    Проблема засчитывается, только если держится STATUS_HOLD секунд подряд; «готов» — сразу."""
    raw = printer_status()
    if raw == "ok":
        _PROBLEM_SINCE["status"] = None
        return "ok"
    if _PROBLEM_SINCE["status"] != raw:
        _PROBLEM_SINCE["status"], _PROBLEM_SINCE["t"] = raw, time.time()
        log.info("Принтер сообщил: %s — проверяю, не кратковременно ли", raw)
    if time.time() - _PROBLEM_SINCE["t"] < STATUS_HOLD:
        return "ok"
    return raw


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
            # Галочку «Работать автономно» стандартный драйвер Windows ставит сам, хотя принтер
            # подключён и печатает, — печать из-за неё НЕ останавливаем, только снимаем её.
            # Если принтер правда выключен, Windows сообщит это флагом «не на связи» выше.
            clear_work_offline()
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


def _convert_with_wps(path, pdf_path):
    """DOC/DOCX/TXT → PDF через WPS Writer (COM). None, если WPS нет или не вышло."""
    try:
        import win32com.client
    except Exception:
        return None
    for prog_id in ("KWps.Application", "Kwps.Application", "wps.Application"):
        app = None
        try:
            app = win32com.client.DispatchEx(prog_id)
        except Exception:
            continue
        try:
            try:
                app.Visible = False
            except Exception:
                pass
            try:
                app.DisplayAlerts = 0
            except Exception:
                pass
            doc = app.Documents.Open(path, False, True)  # (файл, ConfirmConversions, ReadOnly)
            try:
                for export in (lambda: doc.ExportAsFixedFormat(pdf_path, 17),
                               lambda: doc.ExportPdf(pdf_path),
                               lambda: doc.SaveAs2(pdf_path, 17),
                               lambda: doc.SaveAs(pdf_path, 17)):
                    try:
                        export()
                    except Exception:
                        continue
                    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                        log.info("DOCX → PDF через WPS (%s)", prog_id)
                        return pdf_path
            finally:
                try:
                    doc.Close(False)
                except Exception:
                    pass
        except Exception as e:
            log.info("WPS (%s) не смог открыть файл: %s", prog_id, e)
        finally:
            try:
                app.Quit()
            except Exception:
                pass
    return None


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
        log.info("Word недоступен (%s), пробую WPS Office", e)

    # 2) WPS Office (часто уже стоит на ноутбуках; API совместим с Word)
    pdf = _convert_with_wps(path, pdf_path)
    if pdf:
        return pdf

    # 3) LibreOffice (бесплатный)
    for soffice in find_soffice():
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


def print_file(path, copies=1, page_range=None, color=False, sides="single", printer=None, tray=None, fit=False):
    """Возвращает None при успехе или текст ошибки.
    printer/tray — куда печатать; по умолчанию основной принтер и кассета с обычной A4.
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
                return print_file(many, 1, None, color, sides, printer, tray, fit)
            finally:
                _remove(many)
        for n in range(copies):                    # картинка: отдельное задание на каждую копию
            err = print_file(path, 1, page_range, color, sides, printer, tray, fit)
            if err:
                return f"копия {n + 1} из {copies}: {err}"
        return None
    printer = printer or PRINTER_NAME
    plain = tray is None and printer == PRINTER_NAME     # обычный лист из кассеты (не фото)
    if plain:
        tray = pick_bin(printer, PLAIN_BIN_WORDS, "AKBARAK_PLAIN_BIN")
    settings = f"copies=1,{'color' if color else 'monochrome'}"
    if plain:
        # Кассета Canon берёт только обычную бумагу A4/A5/B5/Letter. Если у файла нестандартный
        # размер страницы, драйвер выбирал «бумагу не для кассеты» и печать вставала с ошибкой
        # «Была выбрана бумага, которую нельзя подавать из кассеты». Всегда печатаем на A4.
        settings += ",paper=A4"
    settings += {"duplex": ",duplexlong", "booklet": ",duplexshort"}.get(sides, ",simplex")
    if tray and "," not in tray:
        settings += f",bin={tray}"
    if fit:
        settings += ",fit"
    if page_range:
        settings += f",{page_range}"
    cmd = [SUMATRA, "-print-to", printer, "-print-settings", settings, "-silent", path]
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
    return (f"Windows не принял задание на принтер «{printer}» (SumatraPDF, код {result.returncode}).{why} "
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
    """Сканирует лист со стекла. Сканер после печати или сна часто отвечает «занят» —
    поэтому до 3 попыток с паузой, прежде чем сдаться."""
    for attempt in range(1, 4):
        path = _scan_once(order_id, dpi, color, sheet)
        if path:
            return path
        if attempt < 3:
            log.warning("Заказ #%s: сканер не ответил (попытка %s из 3), жду и пробую снова…", order_id, attempt)
            time.sleep(4)
    return None


def _scan_once(order_id, dpi, color=False, sheet=1):
    """Одна попытка сканирования (цвет или оттенки серого). Возвращает путь к JPEG или None."""
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
        # Порядок: 1) сканер из scanner.txt / AKBARAK_SCANNER (часть названия);
        # 2) отдельный сканер (не той марки, что принтер) — его ставят специально, в нишу терминала;
        # 3) сканер, встроенный в принтер.
        want = (os.environ.get("AKBARAK_SCANNER") or "").strip().lower()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner.txt"), encoding="utf-8") as f:
                want = f.read().strip().lower() or want
        except OSError:
            pass
        brand = (PRINTER_NAME.split() or [""])[0].lower()

        def rank(t):
            n = t[0].lower()
            if want and want in n:
                return 0
            return 2 if brand and brand in n else 1
        infos.sort(key=rank)
        if sheet == 1:
            log.info("Сканеры в Windows: %s", ", ".join(n or "без имени" for n, _ in infos) or "нет")
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
            return "не удалось открыть DOC/DOCX: установите Word, WPS Office или LibreOffice"
        if order.get("order_type") == "photo":
            printer, tray = photo_target()
            log.info("Заказ #%s: фото %s → «%s», лоток: %s", oid, order.get("photo_format"),
                     printer, tray or "по настройкам принтера")
            return print_file(printable, copies, None, True, "single", printer, tray or "", fit=True)
        if not printable.lower().endswith(".pdf"):
            sides = "single"              # картинка — одна страница
        if sides == "booklet":
            try:
                booklet = make_booklet(printable, order.get("page_range"))
            except Exception as e:
                return f"не удалось собрать брошюру: {e}"
            if manual_duplex():
                return print_manual_duplex(oid, booklet, None, copies, color, True)
            return print_file(booklet, copies, None, color, "booklet")
        if sides == "duplex" and manual_duplex():
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


# ─────────────────────── Канал к МБанку (MKassa) ───────────────────────
# MKassa не пускает зарубежные IP, а наш сервер за рубежом. Ноутбук терминала в КР
# забирает у сервера готовый запрос, отправляет его в MKassa и возвращает ответ.
# Работает в отдельном потоке и не мешает печати. Ходит только на api.mkassa.kg.

MKASSA_PREFIX = "https://api.mkassa.kg/api/partners/"


def mkassa_relay_loop():
    s = requests.Session()
    s.headers["X-Worker-Key"] = WORKER_KEY
    warned = False
    while True:
        try:
            r = s.get(f"{SERVER}/mkassa/relay/next", params={"terminal": TERMINAL_ID}, timeout=40)
            if r.status_code != 200:
                if r.status_code == 403 and not warned:
                    log.warning("МБанк-канал: сервер не принял ключ терминала (worker_key.txt)")
                    warned = True
                time.sleep(2 if r.status_code == 204 else 15)
                continue
            job = r.json()
            out = {"id": job.get("id")}
            url = str(job.get("url", ""))
            if not url.startswith(MKASSA_PREFIX) or job.get("method") not in ("GET", "POST", "PUT"):
                out["error"] = "запрещённый адрес"
            else:
                try:
                    m = requests.request(job["method"], url, headers=job.get("headers") or {},
                                         json=job.get("json"), timeout=18)
                    out.update(status=m.status_code, text=m.text[:20000])
                except Exception as e:
                    out["error"] = str(e)[:300]
            s.post(f"{SERVER}/mkassa/relay/result", json=out, timeout=15)
            log.info("МБанк: %s %s → %s", job.get("method"), url.replace(MKASSA_PREFIX, ""),
                     out.get("status", out.get("error")))
        except Exception as e:
            log.debug("МБанк-канал: %s", e)
            time.sleep(5)


# ─────────────────────── Экран терминала всегда сверху ───────────────────────
# При печати драйвер (монитор состояния Canon) и другие окна выскакивают поверх экрана,
# появляется панель задач Windows — студент может свернуть экран. Раз в 2 секунды
# возвращаем окно киоска наверх. Не трогаем, пока экран свёрнут из админки (kiosk_off.txt).

def _kiosk_window():
    import win32api, win32con, win32gui, win32process
    found = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h) or win32gui.GetClassName(h) != "Chrome_WidgetWin_1":
                return True
            if not win32gui.GetWindowText(h):
                return True
            # Экран терминала (--kiosk) — окно без заголовка; обычный Chrome (скачать драйвер) не трогаем
            if win32gui.GetWindowLong(h, win32con.GWL_STYLE) & win32con.WS_CAPTION:
                return True
            _, pid = win32process.GetWindowThreadProcessId(h)
            hp = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
            try:
                exe = win32process.GetModuleFileNameEx(hp, 0).lower()
            finally:
                win32api.CloseHandle(hp)
            if exe.endswith(("chrome.exe", "msedge.exe")):
                found.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def kiosk_guard_loop():
    try:
        import win32con, win32gui, win32com.client
    except ImportError:
        return
    last_focus = 0.0
    while True:
        time.sleep(2)
        try:
            if os.path.exists(KIOSK_OFF_FLAG):
                continue
            h = _kiosk_window()
            if not h:
                continue
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
                log.info("Экран терминала был свёрнут — вернул")
            win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            if win32gui.GetForegroundWindow() != h and time.time() - last_focus > 8:
                last_focus = time.time()
                win32com.client.Dispatch("WScript.Shell").SendKeys("%")   # Windows разрешает сменить фокус
                win32gui.SetForegroundWindow(h)
        except Exception as e:
            log.debug("kiosk guard: %s", e)


def main():
    log.info("🖨 АкБарак авто-печать запущена (версия %s). Терминал: %s, принтер: %s", VERSION, TERMINAL_ID, PRINTER_NAME)
    log.info("SumatraPDF: %s", "найден" if os.path.exists(SUMATRA) else "НЕ НАЙДЕН!")
    _so = find_soffice()
    log.info("LibreOffice (печать Word): %s", _so[0] if _so else "НЕ НАЙДЕН — DOC/DOCX печататься не будут")
    log.info("Лотки принтера: %s", ", ".join(printer_bins(PRINTER_NAME)) or "не удалось узнать")
    log.info("Обычная A4 → лоток: %s", pick_bin(PRINTER_NAME, PLAIN_BIN_WORDS, "AKBARAK_PLAIN_BIN") or "по настройкам принтера")
    _pp, _pb = photo_target()
    log.info("Фото → принтер «%s», лоток: %s", _pp, _pb or "по настройкам принтера")
    log.info("Ключ терминала: %s", "задан" if WORKER_KEY else "не задан")
    c = printer_is_color()
    log.info("Цветной принтер: %s", {True: "да", False: "НЕТ — цветная печать на этом терминале скрыта", None: "не удалось узнать"}[c])
    log.info("Двусторонняя печать: %s", "вручную (переворачивать пачку)" if manual_duplex() else "автоматически — принтер сам")

    if WORKER_KEY:
        threading.Thread(target=mkassa_relay_loop, daemon=True).start()
        log.info("Канал оплаты МБанк: включён")
    else:
        log.info("Канал оплаты МБанк: выключен (нет worker_key.txt)")

    threading.Thread(target=kiosk_guard_loop, daemon=True).start()
    log.info("Защита экрана: окно терминала держится поверх всех окон")

    last_beat, last_status = 0, None
    while True:
        try:
            refresh_printer()                 # сменили принтер по умолчанию — подхватываем сразу
            status = stable_printer_status()
            if time.time() - last_beat > HEARTBEAT_INTERVAL or status != last_status:
                send_heartbeat(status)
                last_beat, last_status = time.time(), status

            if status != "ok":
                # Принтер не готов — заказы не берём, они спокойно ждут в очереди.
                log.warning("Принтер не готов: %s", status)
                get_new_orders()  # отмечаемся на сервере, что терминал жив
            else:
                t_poll = time.time()
                orders = get_new_orders(wait=20)      # ждём заказ на сервере, без пауз
                if not orders and time.time() - t_poll < 2:
                    time.sleep(CHECK_INTERVAL)        # сервер не ждал (нет сети / старый сервер) — не долбим его
                for order in orders:
                    t0 = time.time()
                    process(order)
                    log.info("Заказ #%s обработан за %.0f с", order.get("id"), time.time() - t0)
                if orders:
                    continue                          # сразу смотрим, нет ли следующего
        except Exception as e:
            log.exception("Непредвиденная ошибка: %s", e)
            time.sleep(CHECK_INTERVAL)
            continue
        if status != "ok":
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

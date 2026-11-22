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
import time
import uuid

import requests
from flask import jsonify, request

import tg_widget

log = logging.getLogger("akbarak.tg")

# Владелец печатает бесплатно: его chat_id из TG_CHAT_ID (+ можно добавить ещё через TG_FREE_CHAT_IDS, через запятую)
FREE_CHATS = {x.strip() for x in (os.getenv("TG_CHAT_ID", "") + "," + os.getenv("TG_FREE_CHAT_IDS", "")).split(",") if x.strip()}
FREE_CHATS.add("8568663749")   # владелец (Сыймык)


def is_owner(chat_id):
    if not chat_id:
        return False
    if str(chat_id) in FREE_CHATS:
        return True
    try:
        conn = H["db"]()
        row = conn.execute("SELECT 1 FROM tg_owners WHERE chat_id=?", (str(chat_id),)).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


TOKEN = "".join((os.getenv("TG_CLIENT_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN", "")).split())   # без пробелов/переносов
API = f"https://api.telegram.org/bot{TOKEN}"
SECRET = hashlib.sha256(("akbarak-tg:" + TOKEN).encode()).hexdigest()[:40]
MAX_TG_MB = 20                      # Telegram отдаёт ботам файлы до 20 МБ
DRAFT_HOURS = 6

H = {}   # заполняется в register(): помощники из server.py
LAST_ERROR = {"text": ""}


# ─────────────────────── Telegram API ───────────────────────

def _strip_new(data):
    """Убирает из клавиатуры поля style/disabled. True — если было что убрать."""
    changed = False
    for row in (data.get("reply_markup") or {}).get("inline_keyboard") or []:
        for b in row:
            if b.pop("style", None):
                changed = True
            if "disabled" in b:
                b.pop("disabled")
                b.setdefault("callback_data", "noop:0:noop")
                changed = True
    return changed


def tg(method, **data):
    if not TOKEN:
        return None
    try:
        r = requests.post(f"{API}/{method}", json=data, timeout=20)
        j = r.json()
        if not j.get("ok") and "reply_markup" in data and _strip_new(data):
            # Новые поля кнопок (цвет, неактивная) не приняли — повторяем с обычными кнопками
            log.warning("TG %s: %s — повторяю без цветных кнопок", method, j.get("description"))
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


PREVIEW_MAX = 10   # сколько страниц показывать альбомом


def render_pages(path, idxs, width=900):
    """PNG-картинки страниц PDF (номера с 0). Пустой список — не получилось."""
    try:
        import io
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        out = []
        for i in idxs:
            if i >= len(pdf):
                break
            page = pdf[i]
            scale = width / page.get_width()
            img = page.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            out.append(buf.getvalue())
        pdf.close()
        return out
    except Exception as e:
        log.warning("preview: %s", e)
        return []


def send_photos(chat_id, images, caption=None, kb=None):
    """Одна картинка — sendPhoto (можно с кнопкой), несколько — альбом."""
    if not TOKEN or not images:
        return None
    try:
        if len(images) == 1:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            if kb:
                import json
                data["reply_markup"] = json.dumps({"inline_keyboard": kb})
            r = requests.post(f"{API}/sendPhoto", data=data, files={"photo": ("p.jpg", images[0], "image/jpeg")},
                              timeout=60)
        else:
            import json
            media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(images))]
            if caption:
                media[0]["caption"] = caption
            files = {f"p{i}": (f"p{i}.jpg", b, "image/jpeg") for i, b in enumerate(images)}
            r = requests.post(f"{API}/sendMediaGroup", data={"chat_id": chat_id, "media": json.dumps(media)},
                              files=files, timeout=90)
        j = r.json()
        if not j.get("ok"):
            log.warning("TG photo: %s", j.get("description"))
        return j.get("result")
    except Exception as e:
        log.error("TG photo: %s", e)
        return None


def btn(text, data=None, url=None, web_app=None, style=None, off=False):
    """style: "success" — зелёная, "danger" — красная, "primary" — синяя (выбранный вариант).
    off=True — неактивная кнопка (серая, ничего не делает): счётчик, подсказка, «−» при 1 копии."""
    b = {"text": text}
    if style in ("success", "danger", "primary"):
        b["style"] = style
    if off:
        b["disabled"] = {}
    elif web_app:
        b["web_app"] = {"url": web_app}
    elif url:
        b["url"] = url
    else:
        b["callback_data"] = data
    return b


# ─────────────────────── Языки ───────────────────────
# Язык студента: выбранный (/lang или ссылка с сайта t.me/PrintKGMA_bot?start=lang_en),
# иначе — язык его Telegram. Тексты: TX[ключ] = (русский, кыргызский, английский).

LANGS = ("ru", "ky", "en")

TX = {
    "hello": (
        "👋 Привет! Это <b>АкБарак</b> — печать с телефона.\n\n"
        "<b>Как напечатать:</b>\n"
        "1️⃣ Нажми 📎 внизу → <b>Файл</b> и выбери документ\n"
        "2️⃣ Выбери ч/б или цвет и сколько копий\n"
        "3️⃣ Оплати — лист выйдет из терминала\n\n"
        "📄 PDF · Word (.docx) · TXT · фото\n"
        "💰 {prices}\n"
        "📍 Терминал: КГМА",
        "👋 Салам! Бул <b>АкБарак</b> — телефондон басып чыгаруу.\n\n"
        "<b>Кантип басып чыгаруу керек:</b>\n"
        "1️⃣ Төмөндөгү 📎 → <b>Файл</b> басып, документти танда\n"
        "2️⃣ Ак-кара же түстүү жана канча нуска экенин танда\n"
        "3️⃣ Төлө — барак терминалдан чыгат\n\n"
        "📄 PDF · Word (.docx) · TXT · сүрөт\n"
        "💰 {prices}\n"
        "📍 Терминал: КММА",
        "👋 Hi! This is <b>AkBarak</b> — print from your phone.\n\n"
        "<b>How to print:</b>\n"
        "1️⃣ Tap 📎 below → <b>File</b> and pick a document\n"
        "2️⃣ Choose B/W or colour and the number of copies\n"
        "3️⃣ Pay — the sheet comes out of the terminal\n\n"
        "📄 PDF · Word (.docx) · TXT · photo\n"
        "💰 {prices}\n"
        "📍 Terminal: KSMA"),
    "help": (
        "📎 <b>Как отправить файл</b>\n\n"
        "<b>Из телефона:</b> скрепка 📎 внизу → <b>Файл</b>. Telegram сразу покажет последние загрузки, "
        "искать по папкам не нужно.\n\n"
        "<b>Из WhatsApp или другого чата:</b> зажми файл → <b>Поделиться</b> → Telegram → <b>АкБарак</b>.\n\n"
        "<b>Фото:</b> 📎 → Галерея. Лучше отправить «как файл» — так качество будет выше.\n\n"
        "Можно отправить несколько файлов по очереди — на каждый придёт свой чек.\n"
        "Старый Word (.doc) пересохрани в .docx или PDF.\n\n"
        "❓ Вопросы: 0773 731 212 (WhatsApp)",
        "📎 <b>Файлды кантип жиберүү керек</b>\n\n"
        "<b>Телефондон:</b> төмөндөгү 📎 → <b>Файл</b>. Telegram акыркы жүктөлгөн файлдарды дароо көрсөтөт, "
        "папкалардан издөөнүн кереги жок.\n\n"
        "<b>WhatsApp же башка чаттан:</b> файлды басып кармап тур → <b>Бөлүшүү</b> → Telegram → <b>АкБарак</b>.\n\n"
        "<b>Сүрөт:</b> 📎 → Галерея. «Файл катары» жиберсең, сапаты жакшы болот.\n\n"
        "Бир нече файлды кезек менен жиберсе болот — ар бирине өз чеги келет.\n"
        "Эски Word (.doc) файлын .docx же PDF кылып сакта.\n\n"
        "❓ Суроолор: 0773 731 212 (WhatsApp)",
        "📎 <b>How to send a file</b>\n\n"
        "<b>From your phone:</b> tap 📎 below → <b>File</b>. Telegram shows your recent downloads right away — "
        "no digging through folders.\n\n"
        "<b>From WhatsApp or another chat:</b> long-press the file → <b>Share</b> → Telegram → <b>AkBarak</b>.\n\n"
        "<b>Photos:</b> 📎 → Gallery. Send it “as a file” for better quality.\n\n"
        "You can send several files one by one — each gets its own receipt.\n"
        "Old Word files (.doc): save as .docx or PDF first.\n\n"
        "❓ Questions: +996 773 731 212 (WhatsApp)"),
    "hint": ("📎 Просто отправь сюда файл: скрепка внизу → <b>Файл</b>. Я посчитаю страницы и цену.",
             "📎 Бул жакка файл жибер: төмөндөгү 📎 → <b>Файл</b>. Барактарды жана баасын эсептейм.",
             "📎 Just send me a file: 📎 below → <b>File</b>. I'll count the pages and the price."),
    "kb_send": ("📎 Как отправить файл", "📎 Файлды кантип жиберүү", "📎 How to send a file"),
    "kb_orders": ("🧾 Мои заказы", "🧾 Буйрутмаларым", "🧾 My orders"),
    "kb_prices": ("💰 Цены", "💰 Баалар", "💰 Prices"),
    "kb_lang": ("🌐 Язык", "🌐 Тил", "🌐 Language"),
    "kb_placeholder": ("Отправь файл через 📎", "📎 аркылуу файл жибер", "Send a file with 📎"),
    "price_list": ("💰 <b>Цены</b>\n\n"
                   "🖨 Печать ч/б — <b>{pbw} сом</b>/стр\n{pcolor}"
                   "📠 Копия — <b>{cbw} сом</b>/стр\n"
                   "🔍 Скан — <b>{sbw} сом</b>/стр (скан приходит в телефон)\n"
                   "📸 Фото на документы — <b>50 сом</b>\n\n"
                   "Оплата: МБанк и другие банки.",
                   "💰 <b>Баалар</b>\n\n"
                   "🖨 Ак-кара басуу — <b>{pbw} сом</b>/бет\n{pcolor}"
                   "📠 Көчүрмө — <b>{cbw} сом</b>/бет\n"
                   "🔍 Скан — <b>{sbw} сом</b>/бет (скан телефонго келет)\n"
                   "📸 Документке сүрөт — <b>50 сом</b>\n\n"
                   "Төлөм: МБанк жана башка банктар.",
                   "💰 <b>Prices</b>\n\n"
                   "🖨 B/W print — <b>{pbw} som</b>/page\n{pcolor}"
                   "📠 Copy — <b>{cbw} som</b>/page\n"
                   "🔍 Scan — <b>{sbw} som</b>/page (sent to your phone)\n"
                   "📸 ID photo — <b>50 som</b>\n\n"
                   "Payment: MBank and other banks."),
    "price_color": ("🎨 Печать цвет — <b>{c} сом</b>/стр\n", "🎨 Түстүү басуу — <b>{c} сом</b>/бет\n",
                    "🎨 Colour print — <b>{c} som</b>/page\n"),
    "receipt": ("ЧЕК", "ЧЕК", "RECEIPT"),
    "pg": ("стр.", "бет", "p."),
    "all_pages": ("📑 Все страницы", "📑 Бардык беттер", "📑 All pages"),
    "docs_btn": ("🪪 Документы · {p}", "🪪 Документке · {p}", "🪪 ID · {p}"),
    "free_short": ("🆓 Бесплатно", "🆓 Акысыз", "🆓 Free"),
    "cancel_short": ("✖ Отмена", "✖ Жокко чыгаруу", "✖ Cancel"),
    "ph_as": ("Как печатать картинку?", "Сүрөттү кантип басуу керек?", "How to print the picture?"),
    "ph_doc": ("📄 Документ A4", "📄 Документ A4", "📄 A4 document"),
    "ph_10x15": ("🖼 Фото 10×15", "🖼 Сүрөт 10×15", "🖼 Photo 10×15"),
    "ph_passport": ("🪪 Фото на документы", "🪪 Документке сүрөт", "🪪 ID photo"),
    "ph_line_10x15": ("🖼 Фото 10×15 · глянцевая бумага", "🖼 Сүрөт 10×15 · жылтырак кагаз", "🖼 Photo 10×15 · glossy paper"),
    "ph_line_doc": ("🪪 Фото на документы {s} · {n} шт", "🪪 Документке сүрөт {s} · {n} даана", "🪪 ID photos {s} · {n} pcs"),
    "ph_pick_doc": ("🪪 Фото на документы (50 сом) — выберите размер:", "🪪 Документке сүрөт (50 сом) — өлчөмдү тандаңыз:",
                    "🪪 ID photo (50 som) — choose a size:"),
    "ph_hint": ("<i>Лицо по центру, светлый фон — обрежем сами.</i>", "<i>Жүз ортодо, ачык фон — өзүбүз кесебиз.</i>",
                "<i>Face centred, light background — we crop it.</i>"),
    "sheets_x": ("{p} × {c} лист.", "{p} × {c} барак", "{p} × {c} sheets"),
    "pages_x": ("{p} стр × {c} коп.", "{p} бет × {c} нуска", "{p} pages × {c} copies"),
    "st_paid": ("Оплата", "Төлөм", "Paid"), "st_print": ("Печать", "Басуу", "Printing"),
    "st_done": ("Готово", "Даяр", "Done"),
    "prices": ("ч/б {bw} сом/стр", "ак-кара {bw} сом/бет", "B/W {bw} som/page"),
    "prices_c": (", цвет {c} сом/стр", ", түстүү {c} сом/бет", ", colour {c} som/page"),
    "lang_pick": ("Выбери язык:", "Тилди танда:", "Choose a language:"),
    "lang_set": ("Готово! Буду писать по-русски.", "Даяр! Кыргызча жазам.", "Done! I'll write in English."),
    "doc_old": ("Это старый формат Word (.doc) — я не могу точно посчитать страницы.\n"
                "Открой файл и сохрани как <b>.docx</b> или <b>PDF</b>, потом пришли снова.",
                "Бул эски Word (.doc) форматы — барактарды так эсептей албайм.\n"
                "Файлды ачып, <b>.docx</b> же <b>PDF</b> кылып сакта да, кайра жибер.",
                "This is an old Word format (.doc) — I can't count its pages exactly.\n"
                "Open it and save as <b>.docx</b> or <b>PDF</b>, then send it again."),
    "bad_ext": ("Формат {ext} не печатаю. Подойдут PDF, Word (.docx), TXT и фото.",
                "{ext} форматын басып чыгарбайм. PDF, Word (.docx), TXT жана сүрөт болот.",
                "I can't print {ext}. PDF, Word (.docx), TXT and photos are fine."),
    "too_big": ("Файл больше {mb} МБ — Telegram не даёт мне его скачать.\nЗагрузи его на сайте: akbarak.vercel.app/app.html",
                "Файл {mb} МБдан чоң — Telegram аны жүктөп алууга бербейт.\nСайтка жүктө: akbarak.vercel.app/app.html",
                "The file is over {mb} MB — Telegram won't let me download it.\nUpload it on the website: akbarak.vercel.app/app.html"),
    "counting": ("⏳ Получил файл, считаю страницы…", "⏳ Файл келди, барактарды эсептеп жатам…", "⏳ Got it, counting pages…"),
    "dl_fail": ("Не получилось скачать файл. Попробуй ещё раз.", "Файлды жүктөй албадым. Дагы бир жолу аракет кыл.",
                "Couldn't download the file. Please try again."),
    "broken": ("Файл не открывается — возможно, он повреждён или защищён паролем.",
               "Файл ачылбай жатат — бузулган же сырсөз менен корголгон болушу мүмкүн.",
               "The file won't open — it may be damaged or password-protected."),
    "pages": ("Страниц", "Барактар", "Pages"),
    "print": ("Печать", "Басып чыгаруу", "Print"),
    "bw": ("ч/б", "ак-кара", "B/W"), "color": ("цвет", "түстүү", "colour"),
    "BW": ("Ч/Б", "Ак-кара", "B/W"), "COLOR": ("Цвет", "Түстүү", "Colour"),
    "single": ("с одной стороны", "бир жагына", "one-sided"),
    "duplex": ("с двух сторон", "эки жагына", "two-sided"),
    "booklet": ("брошюра", "брошюра", "booklet"),
    "s_single": ("1 сторона", "1 жагы", "1 side"), "s_duplex": ("2 стороны", "2 жагы", "2 sides"),
    "s_booklet": ("Брошюра", "Брошюра", "Booklet"),
    "copies": ("Копий", "Нуска", "Copies"),
    "terminal": ("Терминал", "Терминал", "Terminal"),
    "change": ("сменить", "алмаштыруу", "change"),
    "total": ("Итого", "Жалпы", "Total"),
    "som": ("сом", "сом", "som"),
    "pay": ("Оплатить", "Төлөө", "Pay"),
    "cancel": ("✖ Отменить", "✖ Жокко чыгаруу", "✖ Cancel"),
    "cancelled": ("✖ Отменено", "✖ Жокко чыгарылды", "✖ Cancelled"),
    "only_bw": ("На этом терминале только ч/б", "Бул терминалда ак-кара гана", "This terminal prints B/W only"),
    "only_bw_pay": ("На этом терминале сейчас только ч/б. Цену пересчитал — нажми «Оплатить» ещё раз.",
                    "Бул терминалда азыр ак-кара гана. Бааны кайра эсептедим — «Төлөө» баскычын дагы бас.",
                    "This terminal prints B/W only right now. I've updated the price — tap “Pay” again."),
    "stale": ("Этот заказ уже оформлен или устарел", "Бул буйрутма мурун түзүлгөн же эскирген",
              "This order is already placed or expired"),
    "pay_fail": ("Не удалось создать оплату. Попробуй через минуту.", "Төлөм түзүлгөн жок. Бир мүнөттөн кийин аракет кыл.",
                 "Couldn't create the payment. Try again in a minute."),
    "pay_go": ("🧾 Заказ <b>#{id}</b>. Нажми «Оплатить» — откроется оплата.\nПосле оплаты я сразу отправлю файл на терминал.",
               "🧾 Буйрутма <b>#{id}</b>. «Төлөө» баскычын бас — төлөм ачылат.\nТөлөгөндөн кийин файлды дароо терминалга жиберем.",
               "🧾 Order <b>#{id}</b>. Tap “Pay” to open the payment page.\nAs soon as you pay, I'll send the file to the terminal."),
    "cart": ("🧺 <b>Корзина</b> · одна оплата за все файлы", "🧺 <b>Себет</b> · бардык файлдарга бир төлөм",
             "🧺 <b>Cart</b> · one payment for all files"),
    "cart_hint": ("Настроить цвет и копии можно в карточке каждого файла выше.",
                  "Түсүн жана нускасын жогорудагы ар бир файлдын карточкасынан тандаса болот.",
                  "Set colour and copies on each file's card above."),
    "pay_all": ("Оплатить всё", "Баарын төлөө", "Pay for all"),
    "free_btn": ("🆓 Печать бесплатно (владелец)", "🆓 Акысыз басып чыгаруу (ээси)", "🆓 Print free (owner)"),
    "free_ok": ("✅ Отправлено в печать бесплатно. Заказ #{id}, терминал {term}.",
                "✅ Акысыз басып чыгарууга жөнөтүлдү. Заказ #{id}, терминал {term}.",
                "✅ Sent to print for free. Order #{id}, terminal {term}."),
    "clear": ("🗑 Очистить корзину", "🗑 Себетти тазалоо", "🗑 Clear cart"),
    "cleared": ("🗑 Корзина очищена", "🗑 Себет тазаланды", "🗑 Cart cleared"),
    "remove": ("🗑 Убрать файл", "🗑 Файлды алып салуу", "🗑 Remove file"),
    "in_cart": ("🧺 В корзине · оплата внизу", "🧺 Себетте · төлөм ылдыйда", "🧺 In cart · pay below"),
    "files_n": ("Файлов", "Файлдар", "Files"),
    "range_btn_all": ("📑 Страницы: все", "📑 Барактар: баары", "📑 Pages: all"),
    "range_btn": ("📑 Страницы: {r}", "📑 Барактар: {r}", "📑 Pages: {r}"),
    "range_all": ("Все страницы", "Бардык барактар", "All pages"),
    "range_ask": ("📑 Напиши, какие страницы печатать.\nНапример: <b>1-3</b> или <b>2,5,7</b> или <b>1-4,8</b>\n"
                  "Всего в файле <b>{n}</b> стр.",
                  "📑 Кайсы барактарды басуу керек, жаз.\nМисалы: <b>1-3</b> же <b>2,5,7</b> же <b>1-4,8</b>\n"
                  "Файлда бардыгы <b>{n}</b> бет.",
                  "📑 Type which pages to print.\nFor example: <b>1-3</b> or <b>2,5,7</b> or <b>1-4,8</b>\n"
                  "The file has <b>{n}</b> pages."),
    "range_bad": ("Не понял диапазон 🙈 Напиши так: <b>1-3</b> или <b>2,5,7</b> (страницы от 1 до {n}).",
                  "Диапазонду түшүнгөн жокмун 🙈 Мындай жаз: <b>1-3</b> же <b>2,5,7</b> (1ден {n}ге чейин).",
                  "I didn't get that range 🙈 Type it like <b>1-3</b> or <b>2,5,7</b> (pages 1 to {n})."),
    "range_ok": ("✅ Печатаю страницы: {r}", "✅ Бул барактарды басам: {r}", "✅ Printing pages: {r}"),
    "range_line": ("📑 Страницы: <b>{r}</b> ({p} из {n})", "📑 Барактар: <b>{r}</b> ({n} ичинен {p})",
                   "📑 Pages: <b>{r}</b> ({p} of {n})"),
    "preview_cap": ("👁 Предпросмотр: стр. 1 из {n}", "👁 Алдын ала көрүү: {n} беттин 1-би", "👁 Preview: page 1 of {n}"),
    "preview_all": ("👁 Показать все страницы", "👁 Бардык беттерди көрсөтүү", "👁 Show all pages"),
    "preview_more": ("Показаны первые {k} из {n} стр.", "{n} беттин алгачкы {k} бети көрсөтүлдү", "Showing the first {k} of {n} pages"),
    "check": ("🔄 Я оплатил — проверить", "🔄 Төлөдүм — текшерүү", "🔄 I've paid — check"),
    "not_found": ("Заказ не найден", "Буйрутма табылган жок", "Order not found"),
    "not_paid": ("Оплата ещё не пришла. Если оплатил — подожди минуту.", "Төлөм али келе элек. Төлөсөң — бир мүнөт күт.",
                 "Payment hasn't arrived yet. If you've paid, wait a minute."),
    "rejected_now": ("Оплата не прошла. Отправь файл заново.", "Төлөм өткөн жок. Файлды кайра жибер.",
                     "Payment failed. Please send the file again."),
    "paid_now": ("✅ Оплачено! ", "✅ Төлөндү! ", "✅ Paid! "),
    "no_orders": ("Заказов пока нет. Отправь файл — и начнём 🙂", "Азырынча буйрутма жок. Файл жибер — баштайбыз 🙂",
                  "No orders yet. Send a file to get started 🙂"),
    "my_orders": ("Мои заказы", "Буйрутмаларым", "My orders"),
    "ev_paid": ("✅ Оплата получена! Заказ <b>#{id}</b> отправлен на терминал <b>{term}</b>.",
                "✅ Төлөм келди! <b>#{id}</b> буйрутма <b>{term}</b> терминалына жиберилди.",
                "✅ Payment received! Order <b>#{id}</b> has been sent to terminal <b>{term}</b>."),
    "ev_printing": ("🖨 Печатаю заказ <b>#{id}</b>…", "🖨 <b>#{id}</b> буйрутманы басып жатам…", "🖨 Printing order <b>#{id}</b>…"),
    "ev_printed": ("📄 Готово! Заказ <b>#{id}</b> напечатан — забери лист на терминале <b>{term}</b>. Спасибо! 🙌",
                   "📄 Даяр! <b>#{id}</b> буйрутма басылды — баракты <b>{term}</b> терминалынан ал. Рахмат! 🙌",
                   "📄 Done! Order <b>#{id}</b> is printed — pick it up at terminal <b>{term}</b>. Thank you! 🙌"),
    "ev_failed": ("⚠️ Не получилось напечатать заказ <b>#{id}</b>. Мы уже знаем: перепечатаем или вернём деньги.\nВопросы: 0773 731 212.",
                  "⚠️ <b>#{id}</b> буйрутманы басып чыгаруу мүмкүн болгон жок. Билебиз: кайра басабыз же акчаңды кайтарабыз.\nСуроолор: 0773 731 212.",
                  "⚠️ Order <b>#{id}</b> couldn't be printed. We already know: we'll reprint it or refund you.\nQuestions: +996 773 731 212."),
    "ev_rejected": ("Оплата заказа <b>#{id}</b> не прошла. Отправь файл заново, если хочешь напечатать.",
                    "<b>#{id}</b> буйрутманын төлөмү өткөн жок. Басып чыгаргың келсе, файлды кайра жибер.",
                    "Payment for order <b>#{id}</b> failed. Send the file again if you'd like to print it."),
}
STATUS_TX = {
    "pending_payment": ("Ждём оплату", "Төлөм күтүлүүдө", "Awaiting payment"),
    "new": ("В очереди", "Кезекте", "In queue"), "printing": ("Печатается", "Басылып жатат", "Printing"),
    "printed": ("Готово", "Даяр", "Done"), "scan_ready": ("Скан готов", "Скан даяр", "Scan ready"),
    "failed": ("Ошибка печати", "Басып чыгаруу катасы", "Print error"),
    "cancelled": ("Отменён", "Жокко чыгарылды", "Cancelled"), "rejected": ("Не оплачен", "Төлөнгөн жок", "Not paid"),
}


def T(key, lang, **kw):
    v = TX[key][LANGS.index(lang) if lang in LANGS else 0]
    return v.format(**kw) if kw else v


def status_name(st, lang):
    return STATUS_TX.get(st, (st, st, st))[LANGS.index(lang) if lang in LANGS else 0]


def lang_from_tg(code):
    code = (code or "").lower()[:2]
    if code == "ky":
        return "ky"
    if code in ("ru", "uk", "be", "kk", "uz", "tg"):
        return "ru"
    return "en" if code else "ru"


def get_lang(chat_id, tg_code=None):
    conn = H["db"]()
    row = conn.execute("SELECT lang FROM tg_users WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    if row and row["lang"] in LANGS:
        return row["lang"]
    lang = lang_from_tg(tg_code)
    set_lang(chat_id, lang)
    return lang


def set_lang(chat_id, lang):
    conn = H["db"]()
    conn.execute("INSERT INTO tg_users (chat_id, lang) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET lang=excluded.lang",
                 (str(chat_id), lang))
    conn.commit()
    conn.close()


WELCOME_IMG = os.getenv("TG_WELCOME_IMG", "https://akbarak.vercel.app/icons/bot-welcome.jpg")


def main_kb(lang):
    """Постоянные кнопки под полем ввода."""
    return {"keyboard": [[{"text": T("kb_send", lang)}],
                         [{"text": T("kb_orders", lang)}, {"text": T("kb_prices", lang)}, {"text": T("kb_lang", lang)}]],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": T("kb_placeholder", lang)}


def kb_action(text):
    """Какая постоянная кнопка нажата (на любом языке)."""
    for key in ("kb_send", "kb_orders", "kb_prices", "kb_lang"):
        if text in TX[key]:
            return key
    return None


def send_reply_kb(chat_id, text, lang):
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True,
              reply_markup=main_kb(lang))


def send_hello(chat_id, lang):
    """Приветствие с картинкой АкБарака и постоянными кнопками."""
    r = tg("sendPhoto", chat_id=chat_id, photo=WELCOME_IMG, caption=hello(lang), parse_mode="HTML",
           reply_markup=main_kb(lang))
    if not r:   # картинка не загрузилась — шлём текстом
        send_reply_kb(chat_id, hello(lang), lang)


def price_list(lang):
    P = H["PRICES"]
    pc = T("price_color", lang, c=P["print_color"]) if H["color_ok"](H["DEFAULT_TERMINAL"]) else ""
    return T("price_list", lang, pbw=P["print_bw"], pcolor=pc, cbw=P["copy_bw"], sbw=P["scan_bw"])


def steps(lang, stage):
    """Полоска прогресса заказа: оплата → печать → готово."""
    names = [T("st_paid", lang), T("st_print", lang), T("st_done", lang)]
    marks = []
    for i, n in enumerate(names):
        marks.append(("✅ " if i < stage else "🔵 " if i == stage else "⚪ ") + n)
    return " ─ ".join(marks)


def set_menu_button():
    """Кнопка меню бота — список команд (без мини-приложения)."""
    return tg("setChatMenuButton", menu_button={"type": "commands"})


def lang_kb():
    return [[btn("🇷🇺 Русский", "l:0:ru"), btn("🇰🇬 Кыргызча", "l:0:ky"), btn("🇬🇧 English", "l:0:en")]]


# ─────────────────────── Тексты ───────────────────────

def price_line(lang):
    P = H["PRICES"]
    s = T("prices", lang, bw=P["print_bw"])
    if H["color_ok"](H["DEFAULT_TERMINAL"]):
        s += T("prices_c", lang, c=P["print_color"])
    return s


def hello(lang):
    return T("hello", lang, prices=price_line(lang))


PHOTO_FORMATS = {"10x15": "Фото 10×15 см", "passport": "Паспортное 3.5×4.5 см",
                 "d30x40": "Фото на документы 3×4 см", "d25x35": "Фото на документы 2.5×3.5 см",
                 "d50x50": "Фото на документы 5×5 см"}
DOC_KEYS = ("d30x40", "passport", "d25x35", "d50x50")
DOC_LABEL = {"d30x40": ("3×4", 6), "passport": ("3.5×4.5", 6), "d25x35": ("2.5×3.5", 6), "d50x50": ("5×5", 2)}


def is_image(d):
    return (d.get("file_path") or "").lower().endswith((".jpg", ".jpeg", ".png"))


def photo_fmt(d):
    return PHOTO_FORMATS.get(d.get("photo") or "")


def ph_line(d, lang):
    if d["photo"] == "10x15":
        return T("ph_line_10x15", lang)
    size, n = DOC_LABEL[d["photo"]]
    return T("ph_line_doc", lang, s=size, n=n)


def draft_text(d, lang):
    """Короткий чек: что, как, где и сколько. Всё остальное — кнопками."""
    som = T("som", lang)
    if photo_fmt(d):
        return (f"🖼 <b>{esc(d['file_name'])}</b>\n"
                f"{ph_line(d, lang)} · × {d['copies']}\n"
                f"📍 {esc(d['terminal'])}\n\n"
                f"💰 <b>{T('total', lang)}: {draft_total(d)} {som}</b>"
                + (f"\n{T('ph_hint', lang)}" if d['photo'] in DOC_KEYS else ""))
    parts = [f"{d['pages']} {T('pg', lang)}" if not d.get("page_range")
             else f"{T('pg', lang)} {esc(d['page_range'])} ({d['pages']})",
             ("🎨 " + T("COLOR", lang)) if d["color"] else ("⚫ " + T("BW", lang))]
    if d["sides"] != "single":
        parts.append(T("s_" + d["sides"], lang))
    parts.append(f"× {d['copies']}")
    return (f"📄 <b>{esc(d['file_name'])}</b>\n"
            f"{' · '.join(parts)}\n"
            f"📍 {esc(d['terminal'])}\n\n"
            f"💰 <b>{T('total', lang)}: {draft_total(d)} {som}</b>")


def draft_total(d):
    if photo_fmt(d):
        return (H["photo_price"](photo_fmt(d)) or 0) * d["copies"]
    return H["order_total"]("print", "color" if d["color"] else "bw", d["sides"], d["pages"], d["copies"])


def draft_kb(d, lang, multi=False):
    """multi=True — файлов несколько: оплата одна, в корзине внизу.
    Не больше 6 рядов: формат/цвет, стороны или размер фото, копии, страницы+терминал, оплата, бесплатно/отмена."""
    i = d["id"]
    kb = []
    som = T("som", lang)
    sel = lambda on, text: ("✓ " + text) if on else text
    cur = d.get("photo") or ""
    if is_image(d) and H["color_ok"](d["terminal"]):
        kb.append([btn(sel(not cur, "📄 A4"), f"d:{i}:ph_", style="primary" if not cur else None),
                   btn(sel(cur == "10x15", f"🖼 10×15 · {H['photo_price'](PHOTO_FORMATS['10x15'])}"), f"d:{i}:ph_10x15",
                       style="primary" if cur == "10x15" else None),
                   btn(sel(cur in DOC_KEYS, T("docs_btn", lang, p=H['photo_price'](PHOTO_FORMATS['passport']))),
                       f"d:{i}:ph_docs", style="primary" if cur in DOC_KEYS else None)])
        if cur in DOC_KEYS:
            kb.append([btn(sel(cur == k, DOC_LABEL[k][0]), f"d:{i}:ph_{k}", style="primary" if cur == k else None)
                       for k in DOC_KEYS])
    if H["color_ok"](d["terminal"]) and not photo_fmt(d):
        kb.append([btn(sel(not d["color"], "⚫ " + T("BW", lang)), f"d:{i}:bw", style="primary" if not d["color"] else None),
                   btn(sel(d["color"], "🎨 " + T("COLOR", lang)), f"d:{i}:color", style="primary" if d["color"] else None)])
    if H["DUPLEX_ENABLED"] and d["pages"] > 1 and not photo_fmt(d):
        kb.append([btn(sel(d["sides"] == k, T("s_" + k, lang)), f"d:{i}:s_{k}", style="primary" if d["sides"] == k else None)
                   for k in ("single", "duplex", "booklet")])
    kb.append([btn("−", f"d:{i}:minus", off=d["copies"] <= 1), btn(f"× {d['copies']}", off=True),
               btn("+", f"d:{i}:plus", off=d["copies"] >= 20)])
    row = []
    real = d.get("real_pages") or d["pages"]
    if real > 1 and not photo_fmt(d):
        row.append(btn(("📑 " + d["page_range"]) if d.get("page_range") else T("all_pages", lang),
                       f"d:{i}:{'allpages' if d.get('page_range') else 'range'}"))
    if len(terminal_ids()) > 1:
        row.append(btn(f"📍 {d['terminal']}", f"d:{i}:term"))
    if row:
        kb.append(row)
    if multi:
        kb.append([btn(T("in_cart", lang), off=True), btn("✖", f"d:{i}:cancel", style="danger")])
        return kb
    kb.append([btn(f"💳 {T('pay', lang)} · {draft_total(d)} {som}", f"d:{i}:pay", style="success")])
    if is_owner(d.get("chat_id")):
        kb.append([btn(T("free_short", lang), f"d:{i}:free", style="primary"),
                   btn(T("cancel_short", lang), f"d:{i}:cancel", style="danger")])
    else:
        kb.append([btn(T("cancel_short", lang), f"d:{i}:cancel", style="danger")])
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
    conn.execute("UPDATE tg_drafts SET copies=?, color=?, sides=?, terminal=?, msg_id=?, order_id=?, pages=?, "
                 "page_range=?, photo=? WHERE id=?",
                 (d["copies"], d["color"], d["sides"], d["terminal"], d.get("msg_id"), d.get("order_id"), d["pages"],
                  d.get("page_range"), d.get("photo"), d["id"]))
    conn.commit()
    conn.close()


def set_wait(chat_id, draft_id):
    """Бот ждёт от студента диапазон страниц для этого файла (None — ничего не ждёт)."""
    conn = H["db"]()
    conn.execute("INSERT INTO tg_wait (chat_id, draft_id) VALUES (?,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET draft_id=excluded.draft_id", (str(chat_id), draft_id))
    conn.commit()
    conn.close()


def get_wait(chat_id):
    conn = H["db"]()
    row = conn.execute("SELECT draft_id FROM tg_wait WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    return row["draft_id"] if row else None


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def chat_lock(chat_id):
    """Файлы альбома приходят почти одновременно — корзину чата обновляем по очереди."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(chat_id), threading.Lock())


def open_drafts(chat_id):
    conn = H["db"]()
    rows = conn.execute("SELECT * FROM tg_drafts WHERE chat_id=? AND order_id IS NULL ORDER BY id",
                        (str(chat_id),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cart_msg(chat_id, msg_id=None, clear=False):
    """id сообщения-корзины в чате (хранится в БД)."""
    conn = H["db"]()
    if clear or msg_id:
        conn.execute("INSERT INTO tg_carts (chat_id, msg_id) VALUES (?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET msg_id=excluded.msg_id", (str(chat_id), msg_id))
        conn.commit()
        conn.close()
        return msg_id
    row = conn.execute("SELECT msg_id FROM tg_carts WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    return row["msg_id"] if row else None


def cart_text(ds, lang):
    line = "┄" * 18
    rows = []
    for n, d in enumerate(ds, 1):
        if photo_fmt(d):
            rows.append(f"{n}. {ph_line(d, lang)} — <b>{esc(d['file_name'])}</b>\n"
                        f"     × {d['copies']} — <b>{draft_total(d)} {T('som', lang)}</b>")
            continue
        mode = T("color" if d["color"] else "bw", lang)
        if d["sides"] != "single":
            mode += " · " + T(d["sides"], lang)
        if d.get("page_range"):
            mode += " · " + T("pages", lang).lower() + " " + esc(d["page_range"])
        rows.append(f"{n}. 📄 <b>{esc(d['file_name'])}</b>\n"
                    f"     {d['pages']} × {d['copies']} · {mode} — <b>{draft_total(d)} {T('som', lang)}</b>")
    total = sum(draft_total(d) for d in ds)
    return (f"{T('cart', lang)}\n{line}\n" + "\n".join(rows) + f"\n{line}\n"
            f"{T('files_n', lang)}: <b>{len(ds)}</b>\n"
            f"💰 {T('total', lang)}: <b>{total} {T('som', lang)}</b>\n\n<i>{T('cart_hint', lang)}</i>")


def cart_kb(ds, lang):
    total = sum(draft_total(d) for d in ds)
    kb = [[btn(f"💳 {T('pay_all', lang)} — {total} {T('som', lang)}", "c:0:pay", style="success")]]
    if ds and is_owner(ds[0].get("chat_id")):
        kb.append([btn(T("free_btn", lang), "c:0:free", style="primary")])
    kb.append([btn(T("clear", lang), "c:0:clear", style="danger")])
    return kb


def refresh_cart(chat_id, lang, to_bottom=False):
    """Корзина появляется, когда файлов 2 и больше. to_bottom — переслать её вниз (пришёл новый файл)."""
    ds = open_drafts(chat_id)
    old = cart_msg(chat_id)
    if len(ds) < 2:
        if old:
            tg("deleteMessage", chat_id=chat_id, message_id=old)
            cart_msg(chat_id, None, clear=True)
        return ds
    if old and not to_bottom:
        if (edit(chat_id, old, cart_text(ds, lang), cart_kb(ds, lang)) is not None
                or "not modified" in (LAST_ERROR.get("text") or "")):
            return ds
    if old:
        tg("deleteMessage", chat_id=chat_id, message_id=old)
    m = send(chat_id, cart_text(ds, lang), cart_kb(ds, lang))
    cart_msg(chat_id, m["message_id"] if m else None, clear=not m)
    return ds


def redraw_cards(ds, lang):
    """Перерисовать карточки файлов (одна оплата ↔ корзина)."""
    multi = len(ds) >= 2
    for d in ds:
        if d.get("msg_id"):
            edit(d["chat_id"], d["msg_id"], draft_text(d, lang), draft_kb(d, lang, multi))


# ─────────────────────── Приём файла ───────────────────────

def on_file(chat_id, file_id, file_name, size, is_photo=False, lang="ru"):
    ext = os.path.splitext(file_name)[1].lower()
    if ext == ".doc":
        return send(chat_id, T("doc_old", lang))
    if ext not in (".pdf", ".docx", ".txt", ".jpg", ".jpeg", ".png"):
        return send(chat_id, T("bad_ext", lang, ext=esc(ext or "?")))
    if size and size > MAX_TG_MB * 1024 * 1024:
        return send(chat_id, T("too_big", lang, mb=MAX_TG_MB))
    tg("sendChatAction", chat_id=chat_id, action="upload_document")
    wait = send(chat_id, T("counting", lang))
    info = tg("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return send(chat_id, T("dl_fail", lang))
    path = os.path.join(H["FILES_DIR"], f"{uuid.uuid4().hex}{ext}")
    try:
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        log.error("download: %s", e)
        return send(chat_id, T("dl_fail", lang))
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
        return send(chat_id, T("broken", lang))
    pages = max(1, min(H["MAX_PAGES"], pages))
    color = 1 if (is_photo or ext in (".jpg", ".jpeg", ".png")) and H["color_ok"](H["DEFAULT_TERMINAL"]) else 0
    with chat_lock(chat_id):
        _add_draft(chat_id, path, file_name, pages, color, wait, lang)


def _add_draft(chat_id, path, file_name, pages, color, wait, lang):
    before = open_drafts(chat_id)
    conn = H["db"]()
    cur = conn.execute(
        "INSERT INTO tg_drafts (chat_id, file_path, file_name, pages, copies, color, sides, terminal, created_at, "
        "real_pages) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(chat_id), path, file_name[:120], pages, 1, color, "single", H["DEFAULT_TERMINAL"], H["now"](), pages))
    conn.commit()
    d = dict(conn.execute("SELECT * FROM tg_drafts WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.close()
    if wait:
        tg("deleteMessage", chat_id=chat_id, message_id=wait["message_id"])
    if path.lower().endswith(".pdf"):                 # предпросмотр первой страницы
        img = render_pages(path, [0])
        if img:
            kb = [[btn(T("preview_all", lang), f"d:{d['id']}:preview")]] if pages > 1 else None
            send_photos(chat_id, img, T("preview_cap", lang, n=pages), kb)
    multi = len(before) >= 1
    m = None
    if not multi and use_widget(chat_id):            # интерактивная карточка (HTML-виджет), пока для владельца
        LAST_ERROR["text"] = ""
        m = tg("sendWidget", chat_id=chat_id, html=tg_widget.html(lang), caption=f"🧾 {d['file_name']}"[:200],
               chat_data=widget_state(d, lang), allow_queries=True)
        if not m and is_owner(chat_id):              # тест у владельца — сразу показываем, что ответил Telegram
            send(chat_id, "⚠️ Виджет не отправился: <code>" + esc(LAST_ERROR["text"] or "нет ответа") +
                 "</code>\nПоказываю обычный чек.")
        if m:
            conn = H["db"]()
            conn.execute("UPDATE tg_drafts SET widget=1 WHERE id=?", (d["id"],))
            conn.commit()
            conn.close()
            d["widget"] = 1
    if not m:
        m = send(chat_id, draft_text(d, lang), draft_kb(d, lang, multi))
    if m:
        d["msg_id"] = m["message_id"]
        save_draft(d)
    if len(before) == 1:          # был один файл с кнопкой «Оплатить» — теперь оплата в корзине
        redraw_cards(before, lang)
    refresh_cart(chat_id, lang, to_bottom=True)


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
    lang = get_lang(chat_id, (cq.get("from") or {}).get("language_code"))
    if action == "noop":                               # подпись / счётчик (на старых клиентах — обычная кнопка)
        return tg("answerCallbackQuery", callback_query_id=cq["id"])

    if kind == "l" and action in LANGS:                # выбор языка
        set_lang(chat_id, action)
        edit(chat_id, msg_id, T("lang_set", action))
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return send_hello(chat_id, action)

    if kind == "o":                                    # проверка оплаты
        if H.get("sync_payment"):
            try:
                H["sync_payment"](oid, wait=True)          # МБанк: спросить статус напрямую
            except Exception as e:
                log.error("sync_payment: %s", e)
        conn = H["db"]()
        row = conn.execute("SELECT status, terminal_id FROM orders WHERE id=? AND tg_chat_id=?",
                           (oid, str(chat_id))).fetchone()
        conn.close()
        if not row:
            answer = T("not_found", lang)
        elif row["status"] == "pending_payment":
            answer = T("not_paid", lang)
        elif row["status"] == "rejected":
            answer = T("rejected_now", lang)
        else:
            answer = T("paid_now", lang) + status_name(row["status"], lang)
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer, show_alert=True)

    if kind == "c":                                    # корзина: оплатить всё / очистить
        with chat_lock(chat_id):
            if action == "pay":
                return pay_all(cq, chat_id, msg_id, lang)
            if action == "free" and is_owner(chat_id):
                ds = open_drafts(chat_id)
                if not ds:
                    return tg("answerCallbackQuery", callback_query_id=cq["id"], text=T("stale", lang))
                print_free(chat_id, ds, lang)
                cart_msg(chat_id, None, clear=True)
                edit(chat_id, msg_id, cart_text(ds, lang))
                return tg("answerCallbackQuery", callback_query_id=cq["id"])
            if action == "clear":
                for d in open_drafts(chat_id):
                    H["remove"](d["file_path"])
                    conn = H["db"]()
                    conn.execute("DELETE FROM tg_drafts WHERE id=?", (d["id"],))
                    conn.commit()
                    conn.close()
                    if d.get("msg_id"):
                        edit(chat_id, d["msg_id"], f"{T('cancelled', lang)}: {esc(d['file_name'])}")
                cart_msg(chat_id, None, clear=True)
                edit(chat_id, msg_id, T("cleared", lang))
        return tg("answerCallbackQuery", callback_query_id=cq["id"])

    d = get_draft(oid, chat_id)
    if not d or d.get("order_id"):
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text=T("stale", lang))

    if action == "bw":
        d["color"] = 0
    elif action == "color" and H["color_ok"](d["terminal"]):
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
        if d["color"] and not H["color_ok"](d["terminal"]):
            d["color"] = 0
            answer = T("only_bw", lang)
    elif action == "cancel":
        H["remove"](d["file_path"])
        conn = H["db"]()
        conn.execute("DELETE FROM tg_drafts WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        edit(chat_id, msg_id, f"{T('cancelled', lang)}: {esc(d['file_name'])}")
        with chat_lock(chat_id):
            left = open_drafts(chat_id)
            if len(left) == 1:     # остался один файл — снова своя кнопка «Оплатить»
                redraw_cards(left, lang)
            refresh_cart(chat_id, lang)
        return tg("answerCallbackQuery", callback_query_id=cq["id"])
    elif action == "pay":
        return pay(cq, d, chat_id, msg_id, lang)
    elif action == "free" and is_owner(chat_id):
        print_free(chat_id, [d], lang)
        with chat_lock(chat_id):
            refresh_cart(chat_id, lang)
        return tg("answerCallbackQuery", callback_query_id=cq["id"])
    elif action.startswith("ph_") and is_image(d):
        k = action[3:]
        if k == "docs":
            k = d.get("photo") if d.get("photo") in DOC_KEYS else "d30x40"
        if k in PHOTO_FORMATS and H["color_ok"](d["terminal"]):
            d["photo"], d["color"], d["sides"] = k, 1, "single"
        else:
            d["photo"] = None
    elif action == "noop":
        return tg("answerCallbackQuery", callback_query_id=cq["id"])
    elif action == "range":
        set_wait(chat_id, d["id"])
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return send(chat_id, T("range_ask", lang, n=d.get("real_pages") or d["pages"]))
    elif action == "allpages":
        d["page_range"] = None
        d["pages"] = d.get("real_pages") or d["pages"]
    elif action == "preview":
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        tg("sendChatAction", chat_id=chat_id, action="upload_photo")
        n = d.get("real_pages") or d["pages"]
        imgs = render_pages(d["file_path"], list(range(min(n, PREVIEW_MAX))), width=700)
        if imgs:
            send_photos(chat_id, imgs, T("preview_more", lang, k=len(imgs), n=n) if n > len(imgs) else None)
        return

    save_draft(d)
    with chat_lock(chat_id):
        ds = open_drafts(chat_id)
        edit(chat_id, msg_id, draft_text(d, lang), draft_kb(d, lang, len(ds) >= 2))
        if len(ds) >= 2:
            refresh_cart(chat_id, lang)
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


def print_free(chat_id, ds, lang="ru"):
    """Владелец: файлы сразу уходят в очередь печати, сумма 0, без оплаты."""
    group_id, token = uuid.uuid4().hex, secrets.token_urlsafe(16)
    ids = []
    conn = H["db"]()
    for d in ds:
        cur = conn.execute(
            "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
            "paid_at, group_id, terminal_id, order_type, access_token, sides, tg_chat_id, page_range, photo_format) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["file_name"], d["file_path"], "color" if d["color"] and H["color_ok"](d["terminal"]) else "bw",
             d["pages"], d["copies"], 0, "free", "new", H["now"](), H["now"](), group_id, d["terminal"],
             "photo" if photo_fmt(d) else "print", token, d["sides"], str(chat_id), d.get("page_range"), photo_fmt(d)))
        ids.append(cur.lastrowid)
        conn.execute("UPDATE tg_drafts SET order_id=? WHERE id=?", (cur.lastrowid, d["id"]))
    conn.commit()
    conn.close()
    for d, oid in zip(ds, ids):
        if d.get("msg_id"):
            edit(chat_id, d["msg_id"], draft_text(d, lang) + "\n\n" +
                 T("free_ok", lang, id=oid, term=esc(d["terminal"])))
    return ids


def make_payment(d, chat_id):
    """Создаёт заказ и платёж для одного файла. (order_id, total, payment) или None, если платёж не создался."""
    total = draft_total(d)
    token = secrets.token_urlsafe(16)
    conn = H["db"]()
    cur = conn.execute(
        "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
        "group_id, terminal_id, order_type, access_token, sides, tg_chat_id, page_range, photo_format) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["file_name"], d["file_path"], "color" if d["color"] else "bw", d["pages"], d["copies"], total, "gopay",
         "pending_payment", H["now"](), uuid.uuid4().hex, d["terminal"], "photo" if photo_fmt(d) else "print",
         token, d["sides"], str(chat_id), d.get("page_range"), photo_fmt(d)))
    order_id = cur.lastrowid
    conn.execute("UPDATE tg_drafts SET order_id=? WHERE id=?", (order_id, d["id"]))
    conn.commit()
    conn.close()

    return_url = f"{H['SITE_URL']}/app.html?order={order_id}&token={token}"
    payment = None
    try:
        payment = H["gopay"].create_payment(order_id, total, f"АкБарак заказ #{order_id}", return_url=return_url)
        if not payment and H.get("gopay_fallback") and H["gopay_fallback"] is not H["gopay"]:
            payment = H["gopay_fallback"].create_payment(order_id, total, f"АкБарак заказ #{order_id}", return_url=return_url)
    except Exception as e:
        log.error("gopay: %s", e)
    if not payment or not payment.get("checkout_url"):
        conn = H["db"]()
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.execute("UPDATE tg_drafts SET order_id=NULL WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        return None
    conn = H["db"]()
    conn.execute("UPDATE orders SET gopay_payment_id=? WHERE id=?", (payment.get("payment_id"), order_id))
    conn.commit()
    conn.close()
    return order_id, total, payment


def pay(cq, d, chat_id, msg_id, lang="ru"):
    if d["color"] and not H["color_ok"](d["terminal"]):
        d["color"] = 0
        save_draft(d)
        edit(chat_id, msg_id, draft_text(d, lang), draft_kb(d, lang))
        return tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True, text=T("only_bw_pay", lang))
    made = make_payment(d, chat_id)
    if not made:
        return tg("answerCallbackQuery", callback_query_id=cq["id"],
                  text=T("pay_fail", lang), show_alert=True)
    order_id, total, payment = made
    edit(chat_id, msg_id,
         draft_text(d, lang) + "\n\n" + T("pay_go", lang, id=order_id),
         [[btn(f"💳 {T('pay', lang)} {total} {T('som', lang)}", url=payment["checkout_url"], style="success")],
          [btn(T("check", lang), f"o:{order_id}:check")]])
    tg("answerCallbackQuery", callback_query_id=cq["id"])


def pay_all(cq, chat_id, msg_id, lang="ru"):
    """Одна оплата за все файлы корзины: заказы одной группы, один платёж."""
    ds = open_drafts(chat_id)
    if not ds:
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text=T("stale", lang))
    fixed = False
    for d in ds:
        if d["color"] and not H["color_ok"](d["terminal"]):
            d["color"] = 0
            save_draft(d)
            fixed = True
    if fixed:
        redraw_cards(ds, lang)
        refresh_cart(chat_id, lang)
        return tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True, text=T("only_bw_pay", lang))
    group_id, token = uuid.uuid4().hex, secrets.token_urlsafe(16)
    ids, total = [], 0
    conn = H["db"]()
    for d in ds:
        t = draft_total(d)
        total += t
        cur = conn.execute(
            "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
            "group_id, terminal_id, order_type, access_token, sides, tg_chat_id, page_range, photo_format) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["file_name"], d["file_path"], "color" if d["color"] else "bw", d["pages"], d["copies"], t, "gopay",
             "pending_payment", H["now"](), group_id, d["terminal"], "photo" if photo_fmt(d) else "print",
             token, d["sides"], str(chat_id), d.get("page_range"), photo_fmt(d)))
        ids.append(cur.lastrowid)
        conn.execute("UPDATE tg_drafts SET order_id=? WHERE id=?", (cur.lastrowid, d["id"]))
    conn.commit()
    conn.close()
    first = ids[0]
    title = f"АкБарак заказ #{first}" + (f"–#{ids[-1]}" if len(ids) > 1 else "")
    return_url = f"{H['SITE_URL']}/app.html?order={first}&token={token}"
    payment = None
    try:
        payment = H["gopay"].create_payment(first, total, title, return_url=return_url)
        if not payment and H.get("gopay_fallback") and H["gopay_fallback"] is not H["gopay"]:
            payment = H["gopay_fallback"].create_payment(first, total, title, return_url=return_url)
    except Exception as e:
        log.error("gopay: %s", e)
    q = ",".join("?" * len(ids))
    conn = H["db"]()
    if not payment or not payment.get("checkout_url"):
        conn.execute(f"UPDATE orders SET status='rejected' WHERE id IN ({q})", ids)
        conn.execute(f"UPDATE tg_drafts SET order_id=NULL WHERE order_id IN ({q})", ids)
        conn.commit()
        conn.close()
        return tg("answerCallbackQuery", callback_query_id=cq["id"], text=T("pay_fail", lang), show_alert=True)
    conn.execute(f"UPDATE orders SET gopay_payment_id=? WHERE id IN ({q})", [payment.get("payment_id")] + ids)
    conn.commit()
    conn.close()
    for d in ds:                                     # карточки файлов — без кнопок
        if d.get("msg_id"):
            edit(chat_id, d["msg_id"], draft_text(d, lang))
    cart_msg(chat_id, None, clear=True)
    edit(chat_id, msg_id, cart_text(ds, lang) + "\n\n" + T("pay_go", lang, id=first),
         [[btn(f"💳 {T('pay', lang)} {total} {T('som', lang)}", url=payment["checkout_url"], style="success")],
          [btn(T("check", lang), f"o:{first}:check")]])
    tg("answerCallbackQuery", callback_query_id=cq["id"])


def apply_range(chat_id, draft_id, text, lang):
    d = get_draft(draft_id, chat_id)
    if not d or d.get("order_id"):
        return send(chat_id, T("stale", lang))
    real = d.get("real_pages") or d["pages"]
    sel = H["parse_range"](text.replace(" ", "").replace("–", "-").replace("—", "-"), real)
    if not sel:
        set_wait(chat_id, draft_id)                    # ждём ещё раз
        return send(chat_id, T("range_bad", lang, n=real))
    # компактная запись: 1,2,3,5 → 1-3,5
    parts, start, prev = [], sel[0], sel[0]
    for p in sel[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if p is not None:
            start = prev = p
    rng = ",".join(parts)
    d["page_range"] = None if len(sel) == real else rng
    d["pages"] = len(sel)
    save_draft(d)
    with chat_lock(chat_id):
        ds = open_drafts(chat_id)
        if d.get("msg_id"):
            edit(chat_id, d["msg_id"], draft_text(d, lang), draft_kb(d, lang, len(ds) >= 2))
        if len(ds) >= 2:
            refresh_cart(chat_id, lang)
    send(chat_id, T("range_ok", lang, r=esc(d["page_range"] or T("range_all", lang))))


# ─────────────────────── Команды ───────────────────────

def my_orders(chat_id, lang="ru"):
    conn = H["db"]()
    rows = conn.execute("SELECT id, file_name, total, status, terminal_id FROM orders WHERE tg_chat_id=? "
                        "ORDER BY id DESC LIMIT 8", (str(chat_id),)).fetchall()
    conn.close()
    if not rows:
        return send(chat_id, T("no_orders", lang))
    lines = [f"#{r['id']} · {esc(r['file_name'])} · {r['total']} {T('som', lang)} · <b>{status_name(r['status'], lang)}</b>"
             for r in rows]
    send(chat_id, f"<b>{T('my_orders', lang)}</b>\n" + "\n".join(lines))


# ─────────────────────── Интерактивная карточка (HTML-виджет) ───────────────────────
WIDGETS = os.getenv("TG_WIDGETS", "off")   # off — выкл. (Telegram пока не пускает обычных ботов: INPUT_MEDIA_WIDGET_NOT_ALLOWED), owner — только владельцу, all — всем


def use_widget(chat_id):
    return WIDGETS == "all" or (WIDGETS == "owner" and is_owner(chat_id))


def widget_state(d, lang, z="d", order_id=None):
    return tg_widget.state(d, draft_total(d), is_owner(d.get("chat_id")), H["color_ok"](d["terminal"]),
                           H["DUPLEX_ENABLED"], z=z, order_id=order_id, rev=int(time.time()))


def widget_push(chat_id, msg_id, data):
    return tg("editMessageWidgetData", chat_id=chat_id, message_id=msg_id, chat_data=data)


def on_widget_query(q):
    chat_id = (q.get("chat") or {}).get("id") or (q.get("from") or {}).get("id")
    msg_id = q.get("message_id")
    code = (q.get("data") or "")[:64]
    lang = get_lang(chat_id, (q.get("from") or {}).get("language_code"))
    answer = lambda: tg("answerWidgetQuery", widget_query_id=q["id"], data="ok")
    conn = H["db"]()
    row = conn.execute("SELECT * FROM tg_drafts WHERE chat_id=? AND msg_id=?", (str(chat_id), msg_id)).fetchone()
    conn.close()
    d = dict(row) if row else None
    if not d or d.get("order_id"):
        return answer()
    if code == "c+":
        d["copies"] = min(20, d["copies"] + 1)
    elif code == "c-":
        d["copies"] = max(1, d["copies"] - 1)
    elif code in ("k0", "k1"):
        d["color"] = 1 if code == "k1" and H["color_ok"](d["terminal"]) else 0
    elif code.startswith("s:") and H["DUPLEX_ENABLED"] and code[2:] in H["SIDES"]:
        d["sides"] = code[2:]
    elif code.startswith("f:") and is_image(d):
        k = code[2:]
        if k in PHOTO_FORMATS and H["color_ok"](d["terminal"]):
            d["photo"], d["color"], d["sides"] = k, 1, "single"
        else:
            d["photo"] = None
    elif code == "x":
        H["remove"](d["file_path"])
        conn = H["db"]()
        conn.execute("DELETE FROM tg_drafts WHERE id=?", (d["id"],))
        conn.commit()
        conn.close()
        answer()
        return widget_push(chat_id, msg_id, widget_state(d, lang, z="x"))
    elif code == "free" and is_owner(chat_id):
        save_draft(d)
        oid = print_free(chat_id, [d], lang)[0]
        answer()
        return widget_push(chat_id, msg_id, widget_state(d, lang, z="p", order_id=oid))
    elif code == "pay":
        if d["color"] and not H["color_ok"](d["terminal"]):
            d["color"] = 0
        save_draft(d)
        made = make_payment(d, chat_id)
        answer()
        if not made:
            return send(chat_id, T("pay_fail", lang))
        oid, total, payment = made
        widget_push(chat_id, msg_id, widget_state(d, lang, z="w", order_id=oid))
        return send(chat_id, T("pay_go", lang, id=oid),
                    [[btn(f"💳 {T('pay', lang)} {total} {T('som', lang)}", url=payment["checkout_url"], style="success")],
                     [btn(T("check", lang), f"o:{oid}:check")]])
    save_draft(d)
    answer()
    widget_push(chat_id, msg_id, widget_state(d, lang))


def widget_orders(ids, event):
    """Обновляет полоску «Оплачен → Печатается → Готово» в карточках этих заказов."""
    z = {"paid": "p", "printing": "r", "printed": "ok", "failed": "f", "rejected": "x"}.get(event)
    if not z or not ids:
        return
    conn = H["db"]()
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM tg_drafts WHERE widget=1 AND order_id IN ({q})", list(ids)).fetchall()
    conn.close()
    for r in rows:
        d = dict(r)
        widget_push(d["chat_id"], d["msg_id"], widget_state(d, get_lang(d["chat_id"]), z=z, order_id=d["order_id"]))


def on_update(u):
    if "widget_query" in u:
        return on_widget_query(u["widget_query"])
    if "callback_query" in u:
        return on_callback(u["callback_query"])
    m = u.get("message") or {}
    chat_id = (m.get("chat") or {}).get("id")
    if not chat_id or (m.get("chat") or {}).get("type") != "private":
        return
    text = (m.get("text") or "").strip()
    # ссылка с сайта: t.me/PrintKGMA_bot?start=lang_en → /start lang_en
    if text.startswith("/start lang_") and text[12:14] in LANGS:
        set_lang(chat_id, text[12:14])
    lang = get_lang(chat_id, (m.get("from") or {}).get("language_code"))
    if text.startswith("/start mk_") and H.get("market_login"):     # вход на сайт АкБарак Маркет
        return send(chat_id, H["market_login"](chat_id, text[10:].strip(), m.get("from")))
    if "document" in m:
        doc = m["document"]
        return on_file(chat_id, doc["file_id"], doc.get("file_name") or "file", doc.get("file_size"), lang=lang)
    if "photo" in m:
        ph = m["photo"][-1]                             # самое большое
        return on_file(chat_id, ph["file_id"], f"photo_{m.get('message_id')}.jpg", ph.get("file_size"), True, lang=lang)
    if text.startswith("/id"):
        return send(chat_id, f"Ваш chat_id: <code>{chat_id}</code>\n" +
                    ("✅ Бесплатная печать включена (владелец)." if is_owner(chat_id) else
                     "Бесплатная печать выключена. Владелец: отправьте  /owner ПАРОЛЬ_АДМИНКИ"))
    if text.startswith("/owner"):
        key = text[6:].strip()
        admin_key = os.getenv("ADMIN_KEY", "")
        tg("deleteMessage", chat_id=chat_id, message_id=m.get("message_id"))   # пароль не оставляем в чате
        if admin_key and key and secrets.compare_digest(key, admin_key):
            conn = H["db"]()
            conn.execute("INSERT OR IGNORE INTO tg_owners (chat_id) VALUES (?)", (str(chat_id),))
            conn.commit()
            conn.close()
            for d in open_drafts(chat_id):          # уже отправленные файлы — сразу с кнопкой
                if d.get("msg_id"):
                    edit(chat_id, d["msg_id"], draft_text(d, lang), draft_kb(d, lang, len(open_drafts(chat_id)) >= 2))
            refresh_cart(chat_id, lang)
            return send(chat_id, "✅ Готово. Теперь под каждым файлом есть кнопка «🆓 Печать бесплатно (владелец)».")
        return send(chat_id, "❌ Неверный пароль.")
    act = kb_action(text)
    wait_id = get_wait(chat_id)
    if wait_id:
        set_wait(chat_id, None)
        if text and not text.startswith("/") and not act:
            return apply_range(chat_id, wait_id, text, lang)
    if text.startswith("/orders") or act == "kb_orders":
        return my_orders(chat_id, lang)
    if text.startswith("/help") or act == "kb_send":
        return send_reply_kb(chat_id, T("help", lang), lang)
    if text.startswith("/prices") or act == "kb_prices":
        return send_reply_kb(chat_id, price_list(lang), lang)
    if text.startswith("/lang") or act == "kb_lang":
        return send(chat_id, T("lang_pick", lang), lang_kb())
    if text.startswith("/start"):
        return send_hello(chat_id, lang)
    send_reply_kb(chat_id, T("hint", lang), lang)


# ─────────────────────── События заказа → сообщение студенту ───────────────────────

EVENTS = ("paid", "printing", "printed", "failed", "rejected")


def order_event(order_ids, event):
    """Вызывается из server.py, когда заказ оплачен / печатается / напечатан / ошибка."""
    if not TOKEN or event not in EVENTS:
        return
    ids = list(order_ids) if isinstance(order_ids, (list, tuple, set)) else [order_ids]

    def _go():
        try:
            conn = H["db"]()
            q = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT id, tg_chat_id, terminal_id FROM orders WHERE id IN ({q}) "
                                "AND tg_chat_id IS NOT NULL", ids).fetchall()
            conn.close()
            by_chat = {}                                  # заказы одной оплаты — одним сообщением
            for r in rows:
                by_chat.setdefault((r["tg_chat_id"], r["terminal_id"]), []).append(r["id"])
            try:
                widget_orders(ids, event)
            except Exception as e:
                log.error("widget_orders: %s", e)
            for (chat, term), oids in by_chat.items():
                lang = get_lang(chat)
                text = T("ev_" + event, lang, id=", #".join(str(i) for i in oids), term=esc(term))
                stage = {"paid": 1, "printing": 1, "printed": 3}.get(event)
                if stage is not None:
                    text += "\n\n" + steps(lang, stage)
                send(chat, text)
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

ALLOWED_UPDATES = ["message", "callback_query", "widget_query"]


def _ensure_updates():
    """Если вебхук уже стоит, но без widget_query — переставляем тот же адрес с нужными типами."""
    try:
        info = tg("getWebhookInfo") or {}
        if info.get("url") and set(ALLOWED_UPDATES) - set(info.get("allowed_updates") or ["message", "callback_query"]):
            tg("setWebhook", url=info["url"], secret_token=SECRET, allowed_updates=ALLOWED_UPDATES)
            log.info("Вебхук: включил приём нажатий из виджетов")
    except Exception as e:
        log.error("ensure_updates: %s", e)


def register(app, helpers):
    H.update(helpers)
    conn = H["db"]()
    conn.execute("""CREATE TABLE IF NOT EXISTS tg_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id INTEGER, file_path TEXT, file_name TEXT,
        pages INTEGER, copies INTEGER DEFAULT 1, color INTEGER DEFAULT 0, sides TEXT DEFAULT 'single',
        terminal TEXT, created_at TEXT, order_id INTEGER)""")
    conn.execute("CREATE TABLE IF NOT EXISTS tg_users (chat_id TEXT PRIMARY KEY, lang TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS tg_carts (chat_id TEXT PRIMARY KEY, msg_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS tg_wait (chat_id TEXT PRIMARY KEY, draft_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS tg_owners (chat_id TEXT PRIMARY KEY)")
    for col in ("page_range TEXT", "real_pages INTEGER", "photo TEXT", "widget INTEGER"):
        try:
            conn.execute(f"ALTER TABLE tg_drafts ADD COLUMN {col}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN tg_chat_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()
    if TOKEN:   # кнопка мини-приложения в меню бота — ставим при каждом запуске сервера, без ручной настройки
        threading.Thread(target=set_menu_button, daemon=True).start()
        threading.Thread(target=_ensure_updates, daemon=True).start()

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
                allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True)
        CMDS = {"ru": ("Как печатать", "Мои заказы", "Помощь", "Язык"),
                "ky": ("Кантип басуу", "Буйрутмаларым", "Жардам", "Тил"),
                "en": ("How to print", "My orders", "Help", "Language")}
        DESC = {"ru": ("Печать с телефона на терминалах АкБарак. Отправь файл — посчитаю цену, оплатишь, и лист выйдет из терминала.",
                       "Печать с телефона 24/7 · терминалы АкБарак"),
                "ky": ("АкБарак терминалдарында телефондон басып чыгаруу. Файл жибер — баасын эсептейм, төлөйсүң, барак терминалдан чыгат.",
                       "Телефондон басып чыгаруу 24/7 · АкБарак"),
                "en": ("Print from your phone at AkBarak terminals. Send a file — I'll count the price, you pay, and the sheet comes out.",
                       "Print from your phone 24/7 · AkBarak terminals")}
        for lc, (c1, c2, c3, c4) in CMDS.items():
            extra = {} if lc == "ru" else {"language_code": lc}
            tg("setMyCommands", commands=[{"command": "start", "description": c1}, {"command": "orders", "description": c2},
                                          {"command": "help", "description": c3}, {"command": "lang", "description": c4}], **extra)
            tg("setMyDescription", description=DESC[lc][0], **extra)
            tg("setMyShortDescription", short_description=DESC[lc][1], **extra)
        set_menu_button()
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

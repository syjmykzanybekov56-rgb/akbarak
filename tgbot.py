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


def btn(text, data=None, url=None, web_app=None):
    b = {"text": text}
    if web_app:
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


def draft_text(d, lang):
    line = "┄" * 18
    mode = T("color" if d["color"] else "bw", lang) + " · " + T(d["sides"], lang)
    return (f"🧾 <b>{T('receipt', lang)} · АкБарак</b>\n{line}\n"
            f"📄 <b>{esc(d['file_name'])}</b>\n\n"
            f"📑 {T('pages', lang)}: <b>{d['pages']}</b>\n"
            f"{'🎨' if d['color'] else '⚫'} {T('print', lang)}: <b>{mode}</b>\n"
            f"🔁 {T('copies', lang)}: <b>{d['copies']}</b>\n"
            f"📍 {T('terminal', lang)}: <b>{esc(d['terminal'])}</b>\n"
            f"{line}\n"
            f"💰 {T('total', lang)}: <b>{draft_total(d)} {T('som', lang)}</b>\n"
            f"<i>{T('pages_x', lang, p=d['pages'], c=d['copies'])}</i>")


def draft_total(d):
    return H["order_total"]("print", "color" if d["color"] else "bw", d["sides"], d["pages"], d["copies"])


def draft_kb(d, lang):
    i = d["id"]
    kb = []
    if H["color_ok"](d["terminal"]):
        kb.append([btn(("● " if not d["color"] else "○ ") + "⚫ " + T("BW", lang), f"d:{i}:bw"),
                   btn(("● " if d["color"] else "○ ") + "🎨 " + T("COLOR", lang), f"d:{i}:color")])
    kb.append([btn("−", f"d:{i}:minus"), btn(f"🔁 {d['copies']}", f"d:{i}:noop"), btn("+", f"d:{i}:plus")])
    if H["DUPLEX_ENABLED"] and d["pages"] > 1:
        kb.append([btn(("✅ " if d["sides"] == k else "") + T("s_" + k, lang), f"d:{i}:s_{k}")
                   for k in ("single", "duplex", "booklet")])
    terms = terminal_ids()
    if len(terms) > 1:
        kb.append([btn(f"📍 {T('terminal', lang)}: {d['terminal']} ({T('change', lang)})", f"d:{i}:term")])
    kb.append([btn(f"💳 {T('pay', lang)} {draft_total(d)} {T('som', lang)}", f"d:{i}:pay")])
    kb.append([btn(T("cancel", lang), f"d:{i}:cancel")])
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
    m = send(chat_id, draft_text(d, lang), draft_kb(d, lang))
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
    lang = get_lang(chat_id, (cq.get("from") or {}).get("language_code"))

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
        return tg("answerCallbackQuery", callback_query_id=cq["id"])
    elif action == "pay":
        return pay(cq, d, chat_id, msg_id, lang)
    elif action == "noop":
        return tg("answerCallbackQuery", callback_query_id=cq["id"])

    save_draft(d)
    edit(chat_id, msg_id, draft_text(d, lang), draft_kb(d, lang))
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


def pay(cq, d, chat_id, msg_id, lang="ru"):
    if d["color"] and not H["color_ok"](d["terminal"]):
        d["color"] = 0
        save_draft(d)
        edit(chat_id, msg_id, draft_text(d, lang), draft_kb(d, lang))
        return tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True, text=T("only_bw_pay", lang))
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
        return tg("answerCallbackQuery", callback_query_id=cq["id"],
                  text=T("pay_fail", lang), show_alert=True)
    conn = H["db"]()
    conn.execute("UPDATE orders SET gopay_payment_id=? WHERE id=?", (payment.get("payment_id"), order_id))
    conn.commit()
    conn.close()
    edit(chat_id, msg_id,
         draft_text(d, lang) + "\n\n" + T("pay_go", lang, id=order_id),
         [[btn(f"💳 {T('pay', lang)} {total} {T('som', lang)}", url=payment["checkout_url"])],
          [btn(T("check", lang), f"o:{order_id}:check")]])
    tg("answerCallbackQuery", callback_query_id=cq["id"])


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


def on_update(u):
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
    if "document" in m:
        doc = m["document"]
        return on_file(chat_id, doc["file_id"], doc.get("file_name") or "file", doc.get("file_size"), lang=lang)
    if "photo" in m:
        ph = m["photo"][-1]                             # самое большое
        return on_file(chat_id, ph["file_id"], f"photo_{m.get('message_id')}.jpg", ph.get("file_size"), True, lang=lang)
    act = kb_action(text)
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
            for r in rows:
                lang = get_lang(r["tg_chat_id"])
                text = T("ev_" + event, lang, id=r["id"], term=esc(r["terminal_id"]))
                stage = {"paid": 1, "printing": 1, "printed": 3}.get(event)
                if stage is not None:
                    text += "\n\n" + steps(lang, stage)
                send(r["tg_chat_id"], text)
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
    conn.execute("CREATE TABLE IF NOT EXISTS tg_users (chat_id TEXT PRIMARY KEY, lang TEXT)")
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN tg_chat_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()
    if TOKEN:   # кнопка мини-приложения в меню бота — ставим при каждом запуске сервера, без ручной настройки
        threading.Thread(target=set_menu_button, daemon=True).start()

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

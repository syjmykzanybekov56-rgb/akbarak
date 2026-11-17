"""
АкБарак Маркет — площадка для студентов (подключается из server.py).

  • Вход через Telegram-бота: сайт даёт ссылку t.me/<бот>?start=mk_<код>, студент жмёт «Старт»,
    бот подтверждает — сайт получает токен. Пароли и SMS не нужны.
  • Конспекты, лекции, методички: продавец загружает PDF/DOCX, ставит цену; покупатель видит
    первые страницы, платит (МБанк/GoPay — тем же способом, что печать), скачивает или печатает
    на терминале АкБарак.
  • Заказы: «жожошка» (сходить на лекцию частной организации и законспектировать для заказчика),
    консультация, другое. Заказчик платит сразу; исполнитель берёт заказ, сдаёт результат;
    заказчик принимает — деньги (минус комиссия) зачисляются исполнителю на баланс.
  • Выплаты продавцам/исполнителям — вручную владельцем (/mk/admin/...), бот присылает уведомления.

Запрещено (правила на сайте): работы, которые сдают от чужого имени (истории болезни, рефераты, СРС),
данные пациентов, записи платных курсов без разрешения организатора.
"""

import logging
import os
import secrets
import shutil
import uuid
from functools import wraps

from flask import jsonify, request, send_file

log = logging.getLogger("market")
H = {}          # помощники из server.py (db, FILES_DIR, PAY, notify, tg_send, ...)

MK_OFFSET = 900_000_000                         # номер платежа = MK_OFFSET + id платежа маркета
FEE_PCT = int(os.getenv("MK_FEE_PCT", "10"))    # комиссия площадки, %
PREVIEW_PAGES = 3
LOGIN_TTL_MIN = 10
ITEM_KINDS = {"konspekt": "Конспект", "lekciya": "Лекция", "metodichka": "Методичка", "test": "Тесты"}
TASK_KINDS = {"zhozhoshka": "Жожошка", "konsult": "Консультация", "drugoe": "Другое"}
MAX_PRICE = 50_000
ALLOWED_EXT = (".pdf", ".docx")


def now():
    return H["now"]()


def db():
    return H["db"]()


def tg_send(chat_id, text):
    try:
        H["tg_send"](chat_id, text)
    except Exception as e:
        log.warning("tg_send: %s", e)


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def user_link(u):
    name = esc(u["name"] or "студент")
    return f'<a href="tg://user?id={u["tg_chat_id"]}">{name}</a>' + (f" (@{esc(u['username'])})" if u["username"] else "")


# ─────────────────────── Таблицы ───────────────────────

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mk_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tg_chat_id TEXT UNIQUE, name TEXT, username TEXT,
        course TEXT, faculty TEXT, balance INTEGER DEFAULT 0, paid_out INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_sessions (token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_login (code TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, kind TEXT, title TEXT, subject TEXT,
        course TEXT, description TEXT, price INTEGER, file_path TEXT, file_name TEXT, pages INTEGER,
        previews INTEGER DEFAULT 0, status TEXT DEFAULT 'active', sales INTEGER DEFAULT 0,
        rating_sum INTEGER DEFAULT 0, rating_n INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, executor_id INTEGER, kind TEXT,
        title TEXT, description TEXT, when_text TEXT, place TEXT, price INTEGER,
        status TEXT DEFAULT 'pending_payment', result_text TEXT, result_path TEXT, result_name TEXT,
        created_at TEXT, taken_at TEXT, done_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, what TEXT, ref_id INTEGER, amount INTEGER,
        status TEXT DEFAULT 'pending', payment_id TEXT, created_at TEXT, paid_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, buyer_id INTEGER, price INTEGER,
        payment_ref INTEGER, rating INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS mk_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, note TEXT, created_at TEXT);
    """)
    conn.commit()
    conn.close()


def fee(amount):
    return amount * FEE_PCT // 100


# ─────────────────────── Вход ───────────────────────

def current_user():
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip() or request.args.get("s")
    if not tok:
        return None
    conn = db()
    row = conn.execute("SELECT u.* FROM mk_sessions s JOIN mk_users u ON u.id=s.user_id WHERE s.token=?",
                       (tok,)).fetchone()
    conn.close()
    if not row or row["banned"]:
        return None
    return dict(row)


def need_user(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "Войдите через Telegram", "login": True}), 401
        return f(u, *a, **kw)
    return wrapper


def tg_login(chat_id, code, tg_from):
    """Вызывает бот на /start mk_<код>. Возвращает текст ответа студенту."""
    conn = db()
    row = conn.execute("SELECT code, created_at FROM mk_login WHERE code=?", (code,)).fetchone()
    if not row or row["created_at"] < H["ago"](minutes=LOGIN_TTL_MIN):
        conn.close()
        return "⌛ Ссылка для входа устарела. Нажмите «Войти» на сайте ещё раз."
    name = " ".join(x for x in ((tg_from or {}).get("first_name"), (tg_from or {}).get("last_name")) if x) or "Студент"
    username = (tg_from or {}).get("username")
    u = conn.execute("SELECT id FROM mk_users WHERE tg_chat_id=?", (str(chat_id),)).fetchone()
    if u:
        uid = u["id"]
        conn.execute("UPDATE mk_users SET username=? WHERE id=?", (username, uid))
    else:
        uid = conn.execute("INSERT INTO mk_users (tg_chat_id, name, username, created_at) VALUES (?,?,?,?)",
                           (str(chat_id), name[:60], username, now())).lastrowid
    conn.execute("UPDATE mk_login SET user_id=? WHERE code=?", (uid, code))
    conn.commit()
    conn.close()
    return "✅ Вы вошли в <b>АкБарак Маркет</b>. Вернитесь на сайт — страница обновится сама."


def public_user(u):
    return {"id": u["id"], "name": u["name"], "username": u["username"], "course": u["course"],
            "faculty": u["faculty"], "balance": u["balance"], "paid_out": u["paid_out"]}


# ─────────────────────── Файлы и превью ───────────────────────

def page_count(path):
    if path.lower().endswith(".pdf"):
        return H["pdf_page_count"](path)
    if path.lower().endswith(".docx"):
        return H["docx_page_count"](path)
    return None


def make_previews(item_id, path):
    """Картинки первых страниц (только PDF). Возвращает их число."""
    if not path.lower().endswith(".pdf"):
        return 0
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        n = min(PREVIEW_PAGES, len(pdf))
        for i in range(n):
            page = pdf[i]
            img = page.render(scale=700 / page.get_width()).to_pil().convert("RGB")
            img.save(preview_path(item_id, i + 1), "JPEG", quality=72)
        pdf.close()
        return n
    except Exception as e:
        log.warning("preview %s: %s", item_id, e)
        return 0


def preview_path(item_id, n):
    d = os.path.join(H["FILES_DIR"], "mk_preview")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{item_id}_{n}.jpg")


def item_json(r, me=None, bought=False):
    d = {k: r[k] for k in ("id", "kind", "title", "subject", "course", "description", "price", "pages",
                           "previews", "sales", "created_at", "status")}
    d["kind_name"] = ITEM_KINDS.get(r["kind"], r["kind"])
    d["file_ext"] = os.path.splitext(r["file_name"] or "")[1].lower().lstrip(".")
    d["rating"] = round(r["rating_sum"] / r["rating_n"], 1) if r["rating_n"] else None
    d["rating_n"] = r["rating_n"]
    d["seller"] = r["seller_name"] if "seller_name" in r.keys() else None
    d["mine"] = bool(me and me["id"] == r["seller_id"])
    d["bought"] = bought
    return d


# ─────────────────────── Оплата ───────────────────────

def start_payment(user, what, ref_id, amount, title, return_url):
    conn = db()
    pid = conn.execute("INSERT INTO mk_payments (user_id, what, ref_id, amount, created_at) VALUES (?,?,?,?,?)",
                       (user["id"], what, ref_id, amount, now())).lastrowid
    conn.commit()
    conn.close()
    pay_no = MK_OFFSET + pid
    payment = None
    try:
        payment = H["PAY"].create_payment(pay_no, amount, title, return_url=return_url)
        if (not payment or not payment.get("payment_id")) and H.get("PAY_FALLBACK") is not None \
                and H["PAY_FALLBACK"] is not H["PAY"]:
            payment = H["PAY_FALLBACK"].create_payment(pay_no, amount, title, return_url=return_url)
    except Exception as e:
        log.error("mk payment: %s", e)
    if not payment or not payment.get("checkout_url"):
        return None
    conn = db()
    conn.execute("UPDATE mk_payments SET payment_id=? WHERE id=?", (payment.get("payment_id"), pid))
    conn.commit()
    conn.close()
    return {"pay_id": pid, "checkout_url": payment["checkout_url"]}


def is_market_payment(no):
    try:
        return int(no) >= MK_OFFSET
    except (TypeError, ValueError):
        return False


def on_paid(pay_no=None, payment_id=None, amount=None):
    """Вызывает server.py, когда МБанк/GoPay подтвердили оплату. True — платёж маркета найден."""
    conn = db()
    if pay_no is not None:
        p = conn.execute("SELECT * FROM mk_payments WHERE id=?", (int(pay_no) - MK_OFFSET,)).fetchone()
    else:
        p = conn.execute("SELECT * FROM mk_payments WHERE payment_id=?", (payment_id,)).fetchone()
    if not p:
        conn.close()
        return False
    if p["status"] == "paid":
        conn.close()
        return True
    if amount is not None and abs(float(amount) - p["amount"]) > 0.01 and abs(float(amount) - p["amount"] * 100) > 0.5:
        conn.close()
        H["notify"](f"⚠️ Маркет: сумма не совпадает (платёж М{p['id']}: ждали {p['amount']}, пришло {amount}).")
        return True
    conn.execute("UPDATE mk_payments SET status='paid', paid_at=? WHERE id=?", (now(), p["id"]))
    buyer = conn.execute("SELECT * FROM mk_users WHERE id=?", (p["user_id"],)).fetchone()
    if p["what"] == "item":
        it = conn.execute("SELECT * FROM mk_items WHERE id=?", (p["ref_id"],)).fetchone()
        conn.execute("INSERT INTO mk_purchases (item_id, buyer_id, price, payment_ref, created_at) VALUES (?,?,?,?,?)",
                     (it["id"], p["user_id"], p["amount"], p["id"], now()))
        conn.execute("UPDATE mk_items SET sales=sales+1 WHERE id=?", (it["id"],))
        earn = p["amount"] - fee(p["amount"])
        conn.execute("UPDATE mk_users SET balance=balance+? WHERE id=?", (earn, it["seller_id"]))
        seller = conn.execute("SELECT * FROM mk_users WHERE id=?", (it["seller_id"],)).fetchone()
        conn.commit()
        conn.close()
        tg_send(buyer["tg_chat_id"], f"✅ Оплачено: <b>{esc(it['title'])}</b>.\nСкачать или распечатать — "
                                     f"на сайте в разделе «Мои покупки».")
        tg_send(seller["tg_chat_id"], f"💰 Купили ваш материал <b>{esc(it['title'])}</b> за {p['amount']} сом.\n"
                                      f"На баланс: +{earn} сом (комиссия {FEE_PCT}%).")
        H["notify"](f"🛒 Маркет: продажа «{it['title']}» за {p['amount']} сом. Продавцу +{earn}.")
    elif p["what"] == "task":
        conn.execute("UPDATE mk_tasks SET status='open' WHERE id=? AND status='pending_payment'", (p["ref_id"],))
        t = conn.execute("SELECT * FROM mk_tasks WHERE id=?", (p["ref_id"],)).fetchone()
        conn.commit()
        conn.close()
        tg_send(buyer["tg_chat_id"], f"✅ Заказ <b>{esc(t['title'])}</b> оплачен и опубликован. "
                                     f"Когда кто-то возьмёт его — я пришлю контакт исполнителя.")
        H["notify"](f"📝 Маркет: новый заказ «{t['title']}» на {t['price']} сом.")
    else:
        conn.commit()
        conn.close()
    return True


# ─────────────────────── Маршруты ───────────────────────

def register(app, helpers):
    H.update(helpers)
    init_db()

    @app.route("/mk/login/start", methods=["POST"])
    def mk_login_start():
        code = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
        conn = db()
        conn.execute("INSERT INTO mk_login (code, created_at) VALUES (?,?)", (code, now()))
        conn.execute("DELETE FROM mk_login WHERE created_at < ?", (H["ago"](minutes=LOGIN_TTL_MIN * 3),))
        conn.commit()
        conn.close()
        return jsonify({"code": code, "bot_url": f"https://t.me/{H['BOT_USERNAME']}?start=mk_{code}"})

    @app.route("/mk/login/check", methods=["GET"])
    def mk_login_check():
        code = request.args.get("code", "")
        conn = db()
        row = conn.execute("SELECT user_id FROM mk_login WHERE code=?", (code,)).fetchone()
        if not row or not row["user_id"]:
            conn.close()
            return jsonify({"ok": False})
        tok = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO mk_sessions (token, user_id, created_at) VALUES (?,?,?)", (tok, row["user_id"], now()))
        conn.execute("DELETE FROM mk_login WHERE code=?", (code,))
        u = conn.execute("SELECT * FROM mk_users WHERE id=?", (row["user_id"],)).fetchone()
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "token": tok, "user": public_user(u)})

    @app.route("/mk/logout", methods=["POST"])
    def mk_logout():
        tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
        conn = db()
        conn.execute("DELETE FROM mk_sessions WHERE token=?", (tok,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.route("/mk/me", methods=["GET"])
    @need_user
    def mk_me(u):
        conn = db()
        items = conn.execute("SELECT i.*, ? AS seller_name FROM mk_items i WHERE seller_id=? AND status!='deleted' "
                             "ORDER BY id DESC", (u["name"], u["id"])).fetchall()
        bought = conn.execute("SELECT i.*, p.id AS purchase_id, p.rating AS my_rating, us.name AS seller_name "
                              "FROM mk_purchases p JOIN mk_items i ON i.id=p.item_id JOIN mk_users us ON us.id=i.seller_id "
                              "WHERE p.buyer_id=? ORDER BY p.id DESC", (u["id"],)).fetchall()
        tasks = conn.execute("SELECT * FROM mk_tasks WHERE (customer_id=? OR executor_id=?) AND status!='pending_payment' "
                             "ORDER BY id DESC", (u["id"], u["id"])).fetchall()
        conn.close()
        out_b = []
        for r in bought:
            d = item_json(r, u, True)
            d["purchase_id"], d["my_rating"] = r["purchase_id"], r["my_rating"]
            out_b.append(d)
        return jsonify({"user": public_user(u), "fee_pct": FEE_PCT,
                        "items": [item_json(r, u) for r in items], "bought": out_b,
                        "tasks": [task_json(t, u) for t in tasks]})

    @app.route("/mk/profile", methods=["POST"])
    @need_user
    def mk_profile(u):
        d = request.get_json(silent=True) or {}
        conn = db()
        conn.execute("UPDATE mk_users SET name=?, course=?, faculty=? WHERE id=?",
                     ((d.get("name") or u["name"])[:60], (d.get("course") or "")[:20], (d.get("faculty") or "")[:60],
                      u["id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    # ── Материалы ──
    @app.route("/mk/items", methods=["GET"])
    def mk_items():
        me = current_user()
        q = (request.args.get("q") or "").strip().lower()
        kind = request.args.get("kind")
        course = request.args.get("course")
        sql = ("SELECT i.*, u.name AS seller_name FROM mk_items i JOIN mk_users u ON u.id=i.seller_id "
               "WHERE i.status='active'")
        args = []
        if kind in ITEM_KINDS:
            sql += " AND i.kind=?"
            args.append(kind)
        if course:
            sql += " AND i.course=?"
            args.append(course)
        sql += " ORDER BY i.sales DESC, i.id DESC LIMIT 300"
        conn = db()
        rows = conn.execute(sql, args).fetchall()
        mine = set()
        if me:
            mine = {r["item_id"] for r in conn.execute("SELECT item_id FROM mk_purchases WHERE buyer_id=?", (me["id"],))}
        conn.close()
        if q:
            rows = [r for r in rows if q in f"{r['title']} {r['subject']} {r['description']}".lower()]
        return jsonify({"items": [item_json(r, me, r["id"] in mine) for r in rows], "kinds": ITEM_KINDS,
                        "task_kinds": TASK_KINDS, "fee_pct": FEE_PCT})

    @app.route("/mk/items/<int:item_id>/preview/<int:n>", methods=["GET"])
    def mk_preview(item_id, n):
        p = preview_path(item_id, n)
        if n < 1 or n > PREVIEW_PAGES or not os.path.exists(p):
            return jsonify({"error": "Нет превью"}), 404
        resp = send_file(p, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @app.route("/mk/items", methods=["POST"])
    @need_user
    def mk_item_add(u):
        f = request.files.get("file")
        title = (request.form.get("title") or "").strip()[:120]
        kind = request.form.get("kind")
        try:
            price = int(request.form.get("price") or 0)
        except ValueError:
            price = 0
        if not f or not title or kind not in ITEM_KINDS:
            return jsonify({"error": "Заполните название, тип и прикрепите файл"}), 400
        if price < 0 or price > MAX_PRICE:
            return jsonify({"error": f"Цена от 0 до {MAX_PRICE} сом"}), 400
        if request.form.get("agree") != "1":
            return jsonify({"error": "Подтвердите, что это ваш материал и он не нарушает правила"}), 400
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALLOWED_EXT:
            return jsonify({"error": "Можно PDF или DOCX"}), 400
        d = os.path.join(H["FILES_DIR"], "mk_items")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{uuid.uuid4().hex}{ext}")
        f.save(path)
        pages = page_count(path)
        if not pages:
            os.remove(path)
            return jsonify({"error": "Файл не открывается — проверьте, что он не повреждён"}), 400
        conn = db()
        iid = conn.execute(
            "INSERT INTO mk_items (seller_id, kind, title, subject, course, description, price, file_path, file_name, "
            "pages, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (u["id"], kind, title, (request.form.get("subject") or "").strip()[:80],
             (request.form.get("course") or "").strip()[:20], (request.form.get("description") or "").strip()[:1500],
             price, path, (f.filename or "file")[:120], pages, now())).lastrowid
        conn.commit()
        conn.close()
        n = make_previews(iid, path)
        conn = db()
        conn.execute("UPDATE mk_items SET previews=? WHERE id=?", (n, iid))
        conn.commit()
        conn.close()
        H["notify"](f"📚 Маркет: {u['name']} выставил «{title}» ({ITEM_KINDS[kind]}, {price} сом). "
                    f"Проверить: /mk/admin/items")
        return jsonify({"ok": True, "id": iid})

    @app.route("/mk/items/<int:item_id>", methods=["DELETE"])
    @need_user
    def mk_item_del(u, item_id):
        conn = db()
        conn.execute("UPDATE mk_items SET status='deleted' WHERE id=? AND seller_id=?", (item_id, u["id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.route("/mk/items/<int:item_id>/buy", methods=["POST"])
    @need_user
    def mk_buy(u, item_id):
        conn = db()
        it = conn.execute("SELECT * FROM mk_items WHERE id=? AND status='active'", (item_id,)).fetchone()
        had = conn.execute("SELECT 1 FROM mk_purchases WHERE item_id=? AND buyer_id=?", (item_id, u["id"])).fetchone()
        conn.close()
        if not it:
            return jsonify({"error": "Материал не найден"}), 404
        if had or it["seller_id"] == u["id"]:
            return jsonify({"ok": True, "already": True})
        if it["price"] == 0:                       # бесплатный — сразу в покупки
            conn = db()
            conn.execute("INSERT INTO mk_purchases (item_id, buyer_id, price, created_at) VALUES (?,?,0,?)",
                         (item_id, u["id"], now()))
            conn.execute("UPDATE mk_items SET sales=sales+1 WHERE id=?", (item_id,))
            conn.commit()
            conn.close()
            return jsonify({"ok": True, "free": True})
        res = start_payment(u, "item", item_id, it["price"], f"АкБарак Маркет: {it['title'][:60]}",
                            f"{H['SITE_URL']}/market.html#my")
        if not res:
            return jsonify({"error": "Не удалось создать оплату, попробуйте через минуту"}), 502
        return jsonify({"ok": True, **res})

    @app.route("/mk/pay/<int:pay_id>", methods=["GET"])
    @need_user
    def mk_pay_status(u, pay_id):
        conn = db()
        p = conn.execute("SELECT * FROM mk_payments WHERE id=? AND user_id=?", (pay_id, u["id"])).fetchone()
        conn.close()
        if not p:
            return jsonify({"error": "Нет такого платежа"}), 404
        if p["status"] != "paid" and H.get("sync_market"):
            try:
                H["sync_market"](p["payment_id"])
            except Exception as e:
                log.warning("sync_market: %s", e)
            conn = db()
            p = conn.execute("SELECT * FROM mk_payments WHERE id=?", (pay_id,)).fetchone()
            conn.close()
        return jsonify({"status": p["status"]})

    def _owned_purchase(u, purchase_id):
        conn = db()
        r = conn.execute("SELECT i.* FROM mk_purchases p JOIN mk_items i ON i.id=p.item_id "
                         "WHERE p.id=? AND p.buyer_id=?", (purchase_id, u["id"])).fetchone()
        conn.close()
        return r

    @app.route("/mk/download/<int:purchase_id>", methods=["GET"])
    @need_user
    def mk_download(u, purchase_id):
        it = _owned_purchase(u, purchase_id)
        if not it or not os.path.exists(it["file_path"]):
            return jsonify({"error": "Файл не найден"}), 404
        return send_file(it["file_path"], as_attachment=True, download_name=it["file_name"])

    @app.route("/mk/download_my/<int:item_id>", methods=["GET"])
    @need_user
    def mk_download_my(u, item_id):
        conn = db()
        it = conn.execute("SELECT * FROM mk_items WHERE id=? AND seller_id=?", (item_id, u["id"])).fetchone()
        conn.close()
        if not it or not os.path.exists(it["file_path"]):
            return jsonify({"error": "Файл не найден"}), 404
        return send_file(it["file_path"], as_attachment=True, download_name=it["file_name"])

    @app.route("/mk/purchases/<int:purchase_id>/print", methods=["POST"])
    @need_user
    def mk_print(u, purchase_id):
        """Купленный материал → заказ печати на терминале (оплата печати как обычно)."""
        it = _owned_purchase(u, purchase_id)
        if not it or not os.path.exists(it["file_path"]):
            return jsonify({"error": "Файл не найден"}), 404
        d = request.get_json(silent=True) or {}
        color = d.get("color") == "color"
        sides = d.get("sides") if d.get("sides") in ("single", "duplex", "booklet") else "single"
        if sides != "single" and not H["DUPLEX_ENABLED"]:
            sides = "single"
        try:
            copies = max(1, min(20, int(d.get("copies") or 1)))
        except ValueError:
            copies = 1
        terminal = d.get("terminal") or H["DEFAULT_TERMINAL"]
        ext = os.path.splitext(it["file_path"])[1]
        copy_path = os.path.join(H["FILES_DIR"], f"{uuid.uuid4().hex}{ext}")
        shutil.copyfile(it["file_path"], copy_path)
        ptype = "color" if color and H["color_ok"](terminal) else "bw"
        total = H["order_total"]("print", ptype, sides, it["pages"], copies)
        token = secrets.token_urlsafe(16)
        conn = db()
        oid = conn.execute(
            "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, status, created_at, "
            "group_id, terminal_id, order_type, access_token, sides) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["file_name"], copy_path, ptype, it["pages"], copies, total, "gopay", "pending_payment", now(),
             uuid.uuid4().hex, terminal, "print", token, sides)).lastrowid
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "order_id": oid, "token": token, "total": total})

    @app.route("/mk/purchases/<int:purchase_id>/rate", methods=["POST"])
    @need_user
    def mk_rate(u, purchase_id):
        try:
            r = max(1, min(5, int((request.get_json(silent=True) or {}).get("rating"))))
        except (TypeError, ValueError):
            return jsonify({"error": "Оценка от 1 до 5"}), 400
        conn = db()
        p = conn.execute("SELECT * FROM mk_purchases WHERE id=? AND buyer_id=?", (purchase_id, u["id"])).fetchone()
        if not p:
            conn.close()
            return jsonify({"error": "Нет такой покупки"}), 404
        if p["rating"]:
            conn.execute("UPDATE mk_items SET rating_sum=rating_sum-?+? WHERE id=?", (p["rating"], r, p["item_id"]))
        else:
            conn.execute("UPDATE mk_items SET rating_sum=rating_sum+?, rating_n=rating_n+1 WHERE id=?", (r, p["item_id"]))
        conn.execute("UPDATE mk_purchases SET rating=? WHERE id=?", (r, purchase_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    # ── Заказы ──
    @app.route("/mk/tasks", methods=["GET"])
    def mk_tasks():
        me = current_user()
        conn = db()
        rows = conn.execute("SELECT t.*, u.name AS customer_name FROM mk_tasks t JOIN mk_users u ON u.id=t.customer_id "
                            "WHERE t.status='open' ORDER BY t.id DESC LIMIT 200").fetchall()
        conn.close()
        return jsonify({"tasks": [task_json(t, me) for t in rows], "kinds": TASK_KINDS, "fee_pct": FEE_PCT})

    @app.route("/mk/tasks", methods=["POST"])
    @need_user
    def mk_task_add(u):
        d = request.get_json(silent=True) or {}
        title = (d.get("title") or "").strip()[:120]
        kind = d.get("kind")
        try:
            price = int(d.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if not title or kind not in TASK_KINDS:
            return jsonify({"error": "Заполните название и тип заказа"}), 400
        if price < 50 or price > MAX_PRICE:
            return jsonify({"error": f"Цена от 50 до {MAX_PRICE} сом"}), 400
        if d.get("agree") != "1":
            return jsonify({"error": "Подтвердите правила площадки"}), 400
        conn = db()
        tid = conn.execute(
            "INSERT INTO mk_tasks (customer_id, kind, title, description, when_text, place, price, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (u["id"], kind, title, (d.get("description") or "").strip()[:1500], (d.get("when") or "").strip()[:80],
             (d.get("place") or "").strip()[:120], price, now())).lastrowid
        conn.commit()
        conn.close()
        res = start_payment(u, "task", tid, price, f"АкБарак Маркет: заказ #{tid}", f"{H['SITE_URL']}/market.html#my")
        if not res:
            return jsonify({"error": "Не удалось создать оплату, попробуйте через минуту"}), 502
        return jsonify({"ok": True, "id": tid, **res})

    def _task(tid):
        conn = db()
        t = conn.execute("SELECT * FROM mk_tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        return t

    def _user(uid):
        conn = db()
        x = conn.execute("SELECT * FROM mk_users WHERE id=?", (uid,)).fetchone()
        conn.close()
        return x

    @app.route("/mk/tasks/<int:tid>/take", methods=["POST"])
    @need_user
    def mk_task_take(u, tid):
        conn = db()
        cur = conn.execute("UPDATE mk_tasks SET status='taken', executor_id=?, taken_at=? "
                           "WHERE id=? AND status='open' AND customer_id!=?", (u["id"], now(), tid, u["id"]))
        conn.commit()
        conn.close()
        if not cur.rowcount:
            return jsonify({"error": "Заказ уже взяли или он недоступен"}), 409
        t = _task(tid)
        cust = _user(t["customer_id"])
        tg_send(cust["tg_chat_id"], f"🙋 Ваш заказ <b>{esc(t['title'])}</b> взял исполнитель: {user_link(u)}.\n"
                                    f"Можно написать ему в Telegram. Когда он сдаст работу — я сообщу.")
        tg_send(u["tg_chat_id"], f"✅ Вы взяли заказ <b>{esc(t['title'])}</b> ({t['price']} сом).\n"
                                 f"Заказчик: {user_link(cust)}.\nСдать работу — на сайте, раздел «Мои заказы».")
        return jsonify({"ok": True})

    @app.route("/mk/tasks/<int:tid>/submit", methods=["POST"])
    @need_user
    def mk_task_submit(u, tid):
        t = _task(tid)
        if not t or t["executor_id"] != u["id"] or t["status"] not in ("taken", "submitted"):
            return jsonify({"error": "Нельзя сдать этот заказ"}), 403
        text = (request.form.get("text") or "").strip()[:4000]
        f = request.files.get("file")
        path = name = None
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in (".pdf", ".docx", ".jpg", ".jpeg", ".png", ".txt", ".zip"):
                return jsonify({"error": "Можно PDF, DOCX, JPG, PNG, TXT или ZIP"}), 400
            d = os.path.join(H["FILES_DIR"], "mk_results")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{uuid.uuid4().hex}{ext}")
            f.save(path)
            name = f.filename[:120]
        if not text and not path:
            return jsonify({"error": "Добавьте текст или файл"}), 400
        conn = db()
        conn.execute("UPDATE mk_tasks SET status='submitted', result_text=?, result_path=COALESCE(?, result_path), "
                     "result_name=COALESCE(?, result_name) WHERE id=?", (text, path, name, tid))
        conn.commit()
        conn.close()
        cust = _user(t["customer_id"])
        tg_send(cust["tg_chat_id"], f"📬 Исполнитель сдал заказ <b>{esc(t['title'])}</b>.\n"
                                    f"Посмотрите на сайте («Мои заказы») и нажмите «Принять».")
        return jsonify({"ok": True})

    @app.route("/mk/tasks/<int:tid>/result", methods=["GET"])
    @need_user
    def mk_task_result(u, tid):
        t = _task(tid)
        if not t or u["id"] not in (t["customer_id"], t["executor_id"]) or not t["result_path"]:
            return jsonify({"error": "Нет файла"}), 404
        return send_file(t["result_path"], as_attachment=True, download_name=t["result_name"] or "result")

    @app.route("/mk/tasks/<int:tid>/accept", methods=["POST"])
    @need_user
    def mk_task_accept(u, tid):
        conn = db()
        t = conn.execute("SELECT * FROM mk_tasks WHERE id=?", (tid,)).fetchone()
        if not t or t["customer_id"] != u["id"] or t["status"] != "submitted":
            conn.close()
            return jsonify({"error": "Нельзя принять этот заказ"}), 403
        earn = t["price"] - fee(t["price"])
        conn.execute("UPDATE mk_tasks SET status='done', done_at=? WHERE id=?", (now(), tid))
        conn.execute("UPDATE mk_users SET balance=balance+? WHERE id=?", (earn, t["executor_id"]))
        conn.commit()
        conn.close()
        ex = _user(t["executor_id"])
        tg_send(ex["tg_chat_id"], f"🎉 Заказчик принял работу <b>{esc(t['title'])}</b>.\n"
                                  f"На баланс: +{earn} сом (комиссия {FEE_PCT}%).")
        H["notify"](f"✅ Маркет: заказ «{t['title']}» выполнен. Исполнителю {ex['name']} +{earn} сом.")
        return jsonify({"ok": True})

    @app.route("/mk/tasks/<int:tid>/cancel", methods=["POST"])
    @need_user
    def mk_task_cancel(u, tid):
        conn = db()
        t = conn.execute("SELECT * FROM mk_tasks WHERE id=?", (tid,)).fetchone()
        if not t or t["customer_id"] != u["id"] or t["status"] != "open":
            conn.close()
            return jsonify({"error": "Отменить можно, пока заказ никто не взял"}), 403
        conn.execute("UPDATE mk_tasks SET status='cancelled' WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        H["notify"](f"↩️ Маркет: заказ #{tid} «{t['title']}» отменён заказчиком {u['name']} "
                    f"(tg id {u['tg_chat_id']}). Верните {t['price']} сом.")
        tg_send(u["tg_chat_id"], f"↩️ Заказ <b>{esc(t['title'])}</b> отменён. Деньги ({t['price']} сом) вернём "
                                 f"в течение суток — владелец уже получил уведомление.")
        return jsonify({"ok": True})

    @app.route("/mk/tasks/<int:tid>/dispute", methods=["POST"])
    @need_user
    def mk_task_dispute(u, tid):
        t = _task(tid)
        if not t or u["id"] not in (t["customer_id"], t["executor_id"]) or t["status"] not in ("taken", "submitted"):
            return jsonify({"error": "Нельзя"}), 403
        why = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:500]
        H["notify"](f"⚠️ Маркет: спор по заказу #{tid} «{t['title']}» от {u['name']} (tg id {u['tg_chat_id']}): {why}")
        return jsonify({"ok": True})

    # ── Админ ──
    @app.route("/mk/admin/overview", methods=["GET"])
    def mk_admin_overview():
        if not H["admin_ok"]():
            return jsonify({"error": "Нет доступа"}), 403
        conn = db()
        users = conn.execute("SELECT id, name, username, tg_chat_id, balance, paid_out FROM mk_users "
                             "WHERE balance>0 ORDER BY balance DESC").fetchall()
        items = conn.execute("SELECT i.id, i.title, i.kind, i.price, i.sales, i.status, u.name AS seller "
                             "FROM mk_items i JOIN mk_users u ON u.id=i.seller_id ORDER BY i.id DESC LIMIT 200").fetchall()
        tasks = conn.execute("SELECT id, title, kind, price, status FROM mk_tasks ORDER BY id DESC LIMIT 200").fetchall()
        conn.close()
        return jsonify({"to_pay": [dict(r) for r in users], "items": [dict(r) for r in items],
                        "tasks": [dict(r) for r in tasks]})

    @app.route("/mk/admin/payout", methods=["POST"])
    def mk_admin_payout():
        if not H["admin_ok"]():
            return jsonify({"error": "Нет доступа"}), 403
        d = request.get_json(silent=True) or {}
        uid, amount = int(d.get("user_id") or 0), int(d.get("amount") or 0)
        conn = db()
        u = conn.execute("SELECT * FROM mk_users WHERE id=?", (uid,)).fetchone()
        if not u or amount <= 0 or amount > u["balance"]:
            conn.close()
            return jsonify({"error": "Неверная сумма"}), 400
        conn.execute("UPDATE mk_users SET balance=balance-?, paid_out=paid_out+? WHERE id=?", (amount, amount, uid))
        conn.execute("INSERT INTO mk_payouts (user_id, amount, note, created_at) VALUES (?,?,?,?)",
                     (uid, amount, (d.get("note") or "")[:200], now()))
        conn.commit()
        conn.close()
        tg_send(u["tg_chat_id"], f"💸 Вам выплачено {amount} сом из баланса АкБарак Маркет.")
        return jsonify({"ok": True})

    @app.route("/mk/admin/items/<int:item_id>/<action>", methods=["POST"])
    def mk_admin_item(item_id, action):
        if not H["admin_ok"]():
            return jsonify({"error": "Нет доступа"}), 403
        if action not in ("hide", "show"):
            return jsonify({"error": "hide или show"}), 400
        conn = db()
        conn.execute("UPDATE mk_items SET status=? WHERE id=?", ("hidden" if action == "hide" else "active", item_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})


TASK_STATUS = {"pending_payment": "Ждёт оплаты", "open": "Ищем исполнителя", "taken": "В работе",
               "submitted": "Сдан, ждёт проверки", "done": "Выполнен", "cancelled": "Отменён"}


def task_json(t, me=None):
    d = {k: t[k] for k in ("id", "kind", "title", "description", "when_text", "place", "price", "status",
                           "created_at")}
    d["kind_name"] = TASK_KINDS.get(t["kind"], t["kind"])
    d["status_name"] = TASK_STATUS.get(t["status"], t["status"])
    d["earn"] = t["price"] - fee(t["price"])
    d["role"] = ("customer" if me and me["id"] == t["customer_id"]
                 else "executor" if me and me["id"] == t["executor_id"] else None)
    if d["role"]:
        d["result_text"] = t["result_text"]
        d["has_file"] = bool(t["result_path"])
    return d

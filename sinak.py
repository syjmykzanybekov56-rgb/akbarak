"""
«Ак сынак» — честный входной контроль на занятии (прототип).

Преподаватель создаёт тест → на экране QR → студенты проходят тест на своих телефонах.
Сервер выдаёт вопросы по одному (без правильных ответов), сам проверяет ответы и время.
Если студент вышел из теста (переключил приложение, свернул браузер), попытка аннулируется,
а преподаватель видит это и может разрешить продолжить.
"""

import json
import os
import random
import secrets
import time

from flask import jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "sinak_bank.json"), encoding="utf-8") as f:
    BANKS = json.load(f)

GRACE_SEC = 3          # запас на задержку сети при проверке времени ответа
SESSION_HOURS = 6      # сколько живёт тест


def register(app, db):
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sinak_sessions (
            code TEXT PRIMARY KEY, teacher_key TEXT, bank TEXT, n INTEGER, sec INTEGER,
            created REAL, status TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS sinak_students (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, grp TEXT, token TEXT,
            plan TEXT, idx INTEGER DEFAULT 0, q_started REAL, correct INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active', leaves INTEGER DEFAULT 0, last_reason TEXT,
            joined REAL, updated REAL);
    """)
    conn.commit()
    conn.close()

    def session(code):
        conn = db()
        row = conn.execute("SELECT * FROM sinak_sessions WHERE code=? AND created>?",
                           (code, time.time() - SESSION_HOURS * 3600)).fetchone()
        conn.close()
        return row

    def teacher_ok(code):
        s = session(code)
        key = (request.get_json(force=True, silent=True) or {}).get("key") or request.args.get("key", "")
        return s if s and secrets.compare_digest(key or "", s["teacher_key"]) else None

    def student(sid):
        token = (request.get_json(force=True, silent=True) or {}).get("token") or request.args.get("token", "")
        conn = db()
        row = conn.execute("SELECT * FROM sinak_students WHERE id=?", (sid,)).fetchone()
        conn.close()
        if not row or not secrets.compare_digest(token or "", row["token"]):
            return None
        return row

    # ───────── преподаватель ─────────

    @app.route("/sinak/banks", methods=["GET"])
    def sinak_banks():
        return jsonify({k: {"title": b["title"], "subject": b["subject"], "count": len(b["questions"])}
                        for k, b in BANKS.items()})

    @app.route("/sinak/session", methods=["POST"])
    def sinak_new():
        data = request.get_json(force=True, silent=True) or {}
        bank = data.get("bank")
        if bank not in BANKS:
            return jsonify({"error": "Неизвестный банк вопросов"}), 400
        total = len(BANKS[bank]["questions"])
        n = max(3, min(total, int(data.get("n") or 5)))
        sec = max(10, min(120, int(data.get("sec") or 25)))
        code = f"{random.randint(0, 999999):06d}"
        key = secrets.token_urlsafe(16)
        conn = db()
        conn.execute("INSERT OR REPLACE INTO sinak_sessions (code, teacher_key, bank, n, sec, created) VALUES (?,?,?,?,?,?)",
                     (code, key, bank, n, sec, time.time()))
        conn.commit()
        conn.close()
        return jsonify({"code": code, "key": key, "n": n, "sec": sec, "title": BANKS[bank]["title"]})

    @app.route("/sinak/session/<code>", methods=["GET"])
    def sinak_dashboard(code):
        s = teacher_ok(code)
        if not s:
            return jsonify({"error": "Нет доступа"}), 403
        conn = db()
        rows = conn.execute("SELECT * FROM sinak_students WHERE code=? ORDER BY name", (code,)).fetchall()
        conn.close()
        now = time.time()
        students = []
        for r in rows:
            # студент «завис» на вопросе дольше лимита — считаем ответ пропущенным
            students.append({
                "id": r["id"], "name": r["name"], "group": r["grp"], "status": r["status"],
                "done": r["idx"], "total": s["n"], "correct": r["correct"],
                "leaves": r["leaves"], "reason": r["last_reason"],
                "score": round(100 * r["correct"] / s["n"]) if r["status"] == "done" else None,
                "idle": int(now - (r["updated"] or now)),
            })
        return jsonify({"code": code, "status": s["status"], "n": s["n"], "sec": s["sec"],
                        "title": BANKS[s["bank"]]["title"], "students": students})

    @app.route("/sinak/unlock/<code>/<int:sid>", methods=["POST"])
    def sinak_unlock(code, sid):
        """Преподаватель разрешает продолжить (например, был входящий звонок). Текущий вопрос — не засчитан."""
        if not teacher_ok(code):
            return jsonify({"error": "Нет доступа"}), 403
        conn = db()
        conn.execute("UPDATE sinak_students SET status='active', idx=idx+1, q_started=NULL, updated=? "
                     "WHERE id=? AND code=? AND status='annulled'", (time.time(), sid, code))
        conn.execute("UPDATE sinak_students SET status='done' WHERE id=? AND idx>=(SELECT n FROM sinak_sessions WHERE code=?)",
                     (sid, code))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    @app.route("/sinak/close/<code>", methods=["POST"])
    def sinak_close(code):
        if not teacher_ok(code):
            return jsonify({"error": "Нет доступа"}), 403
        conn = db()
        conn.execute("UPDATE sinak_sessions SET status='closed' WHERE code=?", (code,))
        conn.execute("UPDATE sinak_students SET status='done' WHERE code=? AND status='active'", (code,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    # ───────── студент ─────────

    @app.route("/sinak/join/<code>", methods=["POST"])
    def sinak_join(code):
        s = session(code)
        if not s or s["status"] != "open":
            return jsonify({"error": "Тест не найден или уже закрыт. Проверьте код."}), 404
        data = request.get_json(force=True, silent=True) or {}
        name = " ".join(str(data.get("name", "")).split())[:80]
        grp = " ".join(str(data.get("group", "")).split())[:20]
        if len(name) < 3:
            return jsonify({"error": "Введите фамилию и имя"}), 400
        qs = BANKS[s["bank"]]["questions"]
        picked = random.sample(range(len(qs)), s["n"])
        plan = [[qi, random.sample(range(len(qs[qi]["a"])), len(qs[qi]["a"]))] for qi in picked]
        token = secrets.token_urlsafe(16)
        conn = db()
        # повторный вход с тем же ФИО не даёт начать заново
        same = [r for r in conn.execute("SELECT name, grp FROM sinak_students WHERE code=?", (code,)).fetchall()
                if r["name"].casefold() == name.casefold() and r["grp"].casefold() == grp.casefold()]
        if same:
            conn.close()
            return jsonify({"error": "Вы уже начинали этот тест. Обратитесь к преподавателю."}), 409
        cur = conn.execute("INSERT INTO sinak_students (code, name, grp, token, plan, joined, updated) VALUES (?,?,?,?,?,?,?)",
                           (code, name, grp, token, json.dumps(plan), time.time(), time.time()))
        sid = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"sid": sid, "token": token, "n": s["n"], "sec": s["sec"], "title": BANKS[s["bank"]]["title"]})

    @app.route("/sinak/q/<int:sid>", methods=["GET"])
    def sinak_question(sid):
        st = student(sid)
        if not st:
            return jsonify({"error": "Нет доступа"}), 403
        s = session(st["code"])
        if not s:
            return jsonify({"status": "closed"})
        base = {"status": st["status"], "idx": st["idx"], "n": s["n"], "correct": st["correct"]}
        if st["status"] != "active":
            if st["status"] == "done":
                base["score"] = round(100 * st["correct"] / s["n"])
            return jsonify(base)
        plan = json.loads(st["plan"])
        qi, perm = plan[st["idx"]]
        q = BANKS[s["bank"]]["questions"][qi]
        now = time.time()
        started = st["q_started"]
        if not started:
            started = now
            conn = db()
            conn.execute("UPDATE sinak_students SET q_started=?, updated=? WHERE id=?", (now, now, sid))
            conn.commit()
            conn.close()
        left = max(0, int(s["sec"] - (now - started)))
        base.update({"q": q["q"], "a": [q["a"][i] for i in perm], "left": left, "sec": s["sec"]})
        return jsonify(base)

    @app.route("/sinak/answer/<int:sid>", methods=["POST"])
    def sinak_answer(sid):
        st = student(sid)
        if not st or st["status"] != "active":
            return jsonify({"error": "Ответ не принят"}), 409
        s = session(st["code"])
        data = request.get_json(force=True, silent=True) or {}
        if int(data.get("idx", -1)) != st["idx"]:
            return jsonify({"error": "Этот вопрос уже пройден"}), 409
        plan = json.loads(st["plan"])
        qi, perm = plan[st["idx"]]
        choice = data.get("choice")
        in_time = st["q_started"] and time.time() - st["q_started"] <= s["sec"] + GRACE_SEC
        right = (in_time and isinstance(choice, int) and 0 <= choice < len(perm)
                 and perm[choice] == BANKS[s["bank"]]["questions"][qi]["ok"])
        idx = st["idx"] + 1
        status = "done" if idx >= s["n"] else "active"
        conn = db()
        conn.execute("UPDATE sinak_students SET idx=?, correct=correct+?, q_started=NULL, status=?, updated=? WHERE id=?",
                     (idx, 1 if right else 0, status, time.time(), sid))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "status": status})

    @app.route("/sinak/leave/<int:sid>", methods=["POST"])
    def sinak_leave(sid):
        """Страница сообщила, что студент вышел из теста — аннулируем попытку."""
        st = student(sid)
        if not st:
            return jsonify({"error": "Нет доступа"}), 403
        reason = str((request.get_json(force=True, silent=True) or {}).get("reason", ""))[:60]
        if st["status"] == "active":
            conn = db()
            conn.execute("UPDATE sinak_students SET status='annulled', leaves=leaves+1, last_reason=?, updated=? WHERE id=?",
                         (reason, time.time(), sid))
            conn.commit()
            conn.close()
        return jsonify({"success": True})

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sqlite3
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_PATH = "orders.db"
FILES_DIR = "order_files"
os.makedirs(FILES_DIR, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN page_range TEXT DEFAULT NULL")
        conn.commit()
    except:
        pass
    conn.commit()
    conn.close()

init_db()

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "АкБарак сервер работает!"})

@app.route("/upload", methods=["POST"])
def upload():
    try:
        files = request.files.getlist("file")
        if not files or not files[0].filename:
            files = [request.files.get("file")]
        print_type = request.form.get("print_type", "bw")
        copies = int(request.form.get("copies", 1))
        pages = int(request.form.get("pages", 1))
        total = int(request.form.get("total", 0))
        payment = request.form.get("payment", "")
        margins = request.form.get("margins", "standard")

        if not files or not files[0]:
            return jsonify({"error": "Файл не найден"}), 400

        order_ids = []
        conn = sqlite3.connect(DB_PATH)
        page_range = request.form.get("page_range", None)
        for file in files:
            if not file or not file.filename:
                continue
            file_path = os.path.join(FILES_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{file.filename}")
            file.save(file_path)
            cursor = conn.execute(
                "INSERT INTO orders (file_name, file_path, print_type, pages, copies, total, payment, page_range, status, created_at) VALUES (?,?,?,?,?,?,?,?,'new',?)",
                (file.filename, file_path, print_type, pages, copies, total, payment, page_range, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            order_ids.append(cursor.lastrowid)
        conn.commit()
        conn.close()

        return jsonify({"success": True, "order_ids": order_ids})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/order", methods=["POST"])
def order():
    try:
        data = request.get_json()
        conn = sqlite3.connect(DB_PATH)
        order_type = data.get("type","")
        tg_user = data.get("tg_user","")
        conn.execute(
            "INSERT INTO orders (file_name, print_type, pages, copies, total, payment, status, created_at) VALUES (?,?,?,?,?,?,'new',?)",
            (order_type, order_type, data.get("pages",1), data.get("copies",1), data.get("total",0), tg_user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/pending", methods=["GET"])
def pending():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM orders WHERE status='new' ORDER BY created_at").fetchall()
        conn.close()
        return jsonify({"orders": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/file/<int:order_id>", methods=["GET"])
def get_file(order_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT file_path, file_name FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        if row and row[0] and os.path.exists(row[0]):
            return send_file(row[0], as_attachment=True, download_name=row[1])
        return jsonify({"error": "Файл не найден"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/done/<int:order_id>", methods=["POST"])
def done(order_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE orders SET status='printed' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/orders", methods=["GET"])
def all_orders():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()
        conn.close()
        return jsonify({"orders": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/scan_upload/<int:order_id>", methods=["POST"])
def scan_upload(order_id):
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Файл не найден"}), 400
        file_path = os.path.join(FILES_DIR, f"scan_{order_id}.jpg")
        file.save(file_path)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE orders SET status='printed', file_path=? WHERE id=?", (file_path, order_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "file_path": f"/scan_download/{order_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scan_download/<int:order_id>", methods=["GET"])
def scan_download(order_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT file_path FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        if row and row[0] and os.path.exists(row[0]):
            return send_file(row[0], as_attachment=True, download_name=f"scan_{order_id}.jpg")
        return jsonify({"error": "Файл не найден"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scan_status/<int:order_id>", methods=["GET"])
def scan_status(order_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        if row:
            return jsonify({"status": row[0], "ready": row[0] == 'printed'})
        return jsonify({"error": "Заказ не найден"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

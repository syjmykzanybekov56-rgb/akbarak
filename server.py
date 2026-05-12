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
            scan_data BLOB,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN scan_data BLOB")
        conn.commit()
    except:
        pass
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
        cursor = conn.execute(
            "INSERT INTO orders (file_name, print_type, pages, copies, total, payment, status, created_at) VALUES (?,?,?,?,?,?,'new',?)",
            (order_type, order_type, data.get("pages",1), data.get("copies",1), data.get("total",0), tg_user, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"success": True, "order_id": order_id})
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
        scan_data = file.read()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE orders SET status='scan_ready', scan_data=? WHERE id=?", (scan_data, order_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scan_download/<int:order_id>", methods=["GET"])
def scan_download(order_id):
    try:
        import io
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT scan_data FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        if row and row[0]:
            return send_file(
                io.BytesIO(row[0]),
                mimetype="image/jpeg",
                as_attachment=True,
                download_name=f"scan_{order_id}.jpg"
            )
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
            return jsonify({"status": row[0], "ready": row[0] == 'scan_ready'})
        return jsonify({"error": "Заказ не найден"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

@app.route("/admin", methods=["GET"])
def admin():
    password = request.args.get("key", "")
    if password != "akbarak2024":
        return "<h2>Нет доступа. Добавь ?key=akbarak2024</h2>", 403

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    orders = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()

    orders = [dict(o) for o in orders]
    total_rev = sum(o['total'] for o in orders if o['status'] in ('printed','scan_ready'))
    today = datetime.now().strftime('%Y-%m-%d')
    today_orders = [o for o in orders if str(o['created_at']).startswith(today)]
    today_rev = sum(o['total'] for o in today_orders if o['status'] in ('printed','scan_ready'))
    new_count = sum(1 for o in orders if o['status'] == 'new')

    rows = ""
    for o in orders:
        sc = {'new':'#F5A623','printed':'#22c55e','scan_ready':'#22c55e'}.get(o['status'],'#aaa')
        st = {'new':'В очереди','printed':'Готово','scan_ready':'Скан готов','no_file':'Без файла'}.get(o['status'], o['status'])
        btns = ""
        if o['status'] == 'new':
            btns = f"<button onclick=\"cancelOrder({o['id']})\" style=\"background:#ef4444;color:#fff;border:none;padding:6px 12px;border-radius:8px;font-weight:700;cursor:pointer;font-size:12px\">Отменить</button>"
        elif o['status'] in ('printed','scan_ready'):
            btns = f"<button onclick=\"reprintOrder({o['id']})\" style=\"background:#1a3a5c;color:#fff;border:1px solid rgba(255,255,255,0.2);padding:6px 12px;border-radius:8px;font-weight:700;cursor:pointer;font-size:12px\">Перепечатать</button>"
        rows += f"<tr><td>#{o['id']}</td><td>{o['created_at']}</td><td>{o['file_name'] or '-'}</td><td>{o['print_type']}</td><td>{o['pages']} x {o['copies']}</td><td><b>{o['total']} с</b></td><td>{o['payment'] or '-'}</td><td style='color:{sc}'>{st}</td><td>{btns}</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AkBarak Admin</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#0D2240;color:#fff}}
.hdr{{background:#1a3a5c;padding:20px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,0.08)}}
.brand{{font-size:20px;font-weight:900}}.brand span{{color:#F5A623}}
.btn{{background:#F5A623;color:#000;border:none;padding:10px 20px;border-radius:10px;font-weight:800;cursor:pointer}}
.stats{{display:flex;gap:12px;padding:20px;flex-wrap:wrap}}
.sc{{background:#1a3a5c;border-radius:16px;padding:18px 20px;flex:1;min-width:130px;border:1px solid rgba(255,255,255,0.08)}}
.sn{{font-size:28px;font-weight:900;color:#F5A623}}
.sl{{font-size:11px;color:rgba(255,255,255,0.4);margin-top:4px;text-transform:uppercase;letter-spacing:1px}}
.tw{{padding:0 20px 20px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;background:#1a3a5c;border-radius:14px;overflow:hidden}}
th{{padding:12px 14px;text-align:left;font-size:11px;color:rgba(255,255,255,0.4);border-bottom:1px solid rgba(255,255,255,0.08);text-transform:uppercase;letter-spacing:1px}}
td{{padding:12px 14px;font-size:13px;border-bottom:1px solid rgba(255,255,255,0.05);color:rgba(255,255,255,0.8)}}
tr:last-child td{{border-bottom:none}}
</style></head><body>
<div class="hdr"><div class="brand">AK<span>BARAK</span> &mdash; Админ</div><button class="btn" onclick="location.reload()">Обновить</button></div>
<div class="stats">
<div class="sc"><div class="sn">{len(orders)}</div><div class="sl">Всего заказов</div></div>
<div class="sc"><div class="sn">{len(today_orders)}</div><div class="sl">Сегодня</div></div>
<div class="sc"><div class="sn" style="color:#ef4444">{new_count}</div><div class="sl">В очереди</div></div>
<div class="sc"><div class="sn">{today_rev} с</div><div class="sl">Выручка сегодня</div></div>
<div class="sc"><div class="sn">{total_rev} с</div><div class="sl">Всего выручка</div></div>
</div>
<div class="tw"><table>
<thead><tr><th>#</th><th>Время</th><th>Файл</th><th>Тип</th><th>Объём</th><th>Сумма</th><th>Оплата</th><th>Статус</th><th>Действия</th></tr></thead>
<tbody>{rows}</tbody>
</table></div><script>
async function cancelOrder(id){
  if(!confirm('Отменить заказ #'+id+'?')) return;
  await fetch('/done/'+id, {method:'POST'});
  location.reload();
}
async function reprintOrder(id){
  if(!confirm('Перепечатать заказ #'+id+'?')) return;
  await fetch('/reprint/'+id, {method:'POST'});
  alert('Заказ отправлен в очередь!');
  location.reload();
}
// Автообновление каждые 15 секунд
setTimeout(()=>location.reload(), 15000);
</script>
</body></html>"""
    return html

@app.route("/reprint/<int:order_id>", methods=["POST"])
def reprint(order_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE orders SET status='new' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

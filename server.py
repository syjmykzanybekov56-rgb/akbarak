from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import tempfile

app = Flask(__name__)
CORS(app)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8573434102:AAFRzabTCk2KjI_1bGZPQj28B5d91bfdZDk")
ADMIN_ID = os.getenv("ADMIN_ID", "8568663749")

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "АкБарак сервер работает!"})

@app.route("/upload", methods=["POST"])
def upload():
    try:
        file = request.files.get("file")
        print_type = request.form.get("print_type", "bw")
        copies = request.form.get("copies", "1")
        pages = request.form.get("pages", "1")
        total = request.form.get("total", "0")
        user_name = request.form.get("user_name", "Клиент")

        if not file:
            return jsonify({"error": "Файл не найден"}), 400

        price_per = 5 if print_type == "bw" else 10
        type_name = "Чёрно-белая" if print_type == "bw" else "Цветная"

        tmp = tempfile.NamedTemporaryFile(
            suffix=os.path.splitext(file.filename)[1],
            delete=False
        )
        file.save(tmp.name)

        caption = (
            f"🔔 Новый заказ с сайта!\n\n"
            f"👤 {user_name}\n"
            f"📄 {file.filename}\n"
            f"🖨 {type_name}\n"
            f"📋 Страниц: {pages}\n"
            f"🔢 Копий: {copies}\n"
            f"💰 Итого: {total} сом\n\n"
            f"💳 Ожидает оплаты"
        )

        with open(tmp.name, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": ADMIN_ID, "caption": caption},
                files={"document": f}
            )

        os.unlink(tmp.name)

        return jsonify({"success": True, "message": "Файл отправлен!"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

"""
АкБарак — канал оплаты МБанк (MKassa) без печати.
Запускается на любом компьютере в Кыргызстане: забирает у сервера запросы к MKassa,
отправляет их (MKassa пускает только кыргызские IP) и возвращает ответ серверу.
Ключ канала — в файле worker_key.txt рядом (тот же, что MKASSA_RELAY_KEY или WORKER_KEY на Railway).
"""
import logging
import os
import sys
import time

import requests

VERSION = "2026-11-11 relay v1"
SERVER = "https://akbarak-production.up.railway.app"
PREFIX = "https://api.mkassa.kg/api/partners/"
HERE = os.path.dirname(os.path.abspath(__file__))
NAME = os.getenv("AKBARAK_TERMINAL", "relay-" + os.getenv("COMPUTERNAME", "pc"))[:40]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("relay")


def load_key():
    p = os.path.join(HERE, "worker_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            return f.read().strip()
    return ""


def main():
    key = load_key()
    log.info("Канал МБанк %s. Имя: %s. Ключ: %s", VERSION, NAME, "есть" if key else "НЕТ worker_key.txt!")
    try:
        r = requests.get("https://api.mkassa.kg/", timeout=10)
        log.info("Связь с МБанком: есть (%s)", r.status_code)
    except Exception as e:
        log.warning("Связь с МБанком: НЕТ (%s)", e)
    s = requests.Session()
    s.headers["X-Worker-Key"] = key
    ok_said = False
    while True:
        try:
            r = s.get(f"{SERVER}/mkassa/relay/next", params={"terminal": NAME}, timeout=40)
            if r.status_code == 204:
                if not ok_said:
                    log.info("Сервер на связи, ждём оплаты…")
                    ok_said = True
                continue
            if r.status_code != 200:
                log.warning("Сервер ответил %s", r.status_code)
                time.sleep(10)
                continue
            job = r.json()
            out = {"id": job.get("id")}
            url = str(job.get("url", ""))
            if not url.startswith(PREFIX) or job.get("method") not in ("GET", "POST", "PUT"):
                out["error"] = "запрещённый адрес"
            else:
                try:
                    m = requests.request(job["method"], url, headers=job.get("headers") or {},
                                         json=job.get("json"), timeout=18)
                    out.update(status=m.status_code, text=m.text[:20000])
                except Exception as e:
                    out["error"] = str(e)[:300]
            s.post(f"{SERVER}/mkassa/relay/result", json=out, timeout=15)
            log.info("МБанк: %s %s → %s", job.get("method"), url.replace(PREFIX, ""),
                     out.get("status", out.get("error")))
        except Exception as e:
            log.warning("Нет связи: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()

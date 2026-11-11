"""
АкБарак — оплата через MKassa (МБанк QR). Документация: «MKassa API QR» v1.1.7.

Включается, когда на Railway заданы переменные:
    MKASSA_API_KEY      cashier api key (выдаёт МБанк)
    MKASSA_BRANCH       ID торговой точки (число, выдаёт МБанк; можно узнать через /admin/api/mkassa_branches)
    MKASSA_CASHIER      ID кассира (число)
    MKASSA_LONG_LIVING  1 — если МБанк включил «долгий» QR (иначе QR живёт 60 секунд)
    MKASSA_BASE_URL     опционально, по умолчанию https://api.mkassa.kg

Пока MKASSA_API_KEY пустой — сайт, терминал и бот работают через GoPay, как раньше.

Колбэк MKassa не подписан, поэтому мы ему не верим на слово: по id из колбэка
сами спрашиваем статус у MKassa и только тогда считаем заказ оплаченным.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

API_KEY = "".join((os.getenv("MKASSA_API_KEY") or "").split())
BRANCH = (os.getenv("MKASSA_BRANCH") or "").strip()
CASHIER = (os.getenv("MKASSA_CASHIER") or "").strip()
LONG_LIVING = os.getenv("MKASSA_LONG_LIVING", "0").strip().lower() in ("1", "true", "yes", "да")
BASE_URL = (os.getenv("MKASSA_BASE_URL") or "https://api.mkassa.kg").rstrip("/")
QR_LIFETIME = 0 if LONG_LIVING else 55          # секунд; 0 = без ограничения

PAID = ("paid",)
DEAD = ("failed", "overdue", "canceled", "cancelled", "rollback", "for_rollback")

LAST_ERROR = ""


def enabled() -> bool:
    return bool(API_KEY)


def _h(json_body=False):
    h = {"Authorization": f"api-key {API_KEY}", "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def create_payment(order_id: int, amount_som: int, description: str = "", return_url: str = ""):
    """Динамический QR. Возвращает {payment_id, checkout_url, qr_url, expires_in} или None.
    checkout_url — ссылка app.mbank.kg/qr#…: на телефоне открывает МБанк, на терминале рисуем её как QR."""
    global LAST_ERROR
    body = {
        "amount": int(round(amount_som * 100)),            # тыйыны
        "metadata": {"order_id": str(order_id), "shop": "AkBarak",
                     "desc": (description or f"АкБарак заказ #{order_id}")[:150]},
    }
    if LONG_LIVING:
        body["is_long_living"] = True
    if _num(BRANCH) is not None:
        body["branch"] = _num(BRANCH)
    if _num(CASHIER) is not None:
        body["cashier"] = _num(CASHIER)
    try:
        r = requests.post(f"{BASE_URL}/api/partners/transactions/init_payment/",
                          json=body, headers=_h(True), timeout=20)
        logger.info("MKassa init_payment #%s: %s %s", order_id, r.status_code, r.text[:500])
        if r.status_code >= 300:
            LAST_ERROR = f"init_payment {r.status_code}: {r.text[:300]}"
            return None
        d = r.json()
        link = d.get("payment_token")
        if not d.get("id") or not link:
            LAST_ERROR = f"init_payment без id/payment_token: {str(d)[:300]}"
            return None
        LAST_ERROR = ""
        return {"payment_id": d["id"], "checkout_url": link, "qr_url": None, "expires_in": QR_LIFETIME}
    except Exception as e:
        LAST_ERROR = f"init_payment: {e}"
        logger.error("MKassa init_payment #%s: %s", order_id, e)
        return None


def get_transaction(tx_id: str):
    """Статус транзакции прямо из MKassa (источник правды). dict или None."""
    global LAST_ERROR
    if not tx_id:
        return None
    try:
        r = requests.get(f"{BASE_URL}/api/partners/transactions/{tx_id}/", headers=_h(), timeout=20)
        if r.status_code >= 300:
            LAST_ERROR = f"status {r.status_code}: {r.text[:300]}"
            return None
        return r.json()
    except Exception as e:
        LAST_ERROR = f"status: {e}"
        logger.error("MKassa status %s: %s", tx_id, e)
        return None


def cancel(tx_id: str) -> bool:
    """Отмена неоплаченного QR (после оплаты отменить нельзя)."""
    try:
        r = requests.put(f"{BASE_URL}/api/partners/transactions/{tx_id}/cancel/", headers=_h(), timeout=15)
        return r.status_code < 300
    except Exception as e:
        logger.error("MKassa cancel %s: %s", tx_id, e)
        return False


def branches():
    """Список торговых точек и кассиров — чтобы узнать MKASSA_BRANCH / MKASSA_CASHIER."""
    r = requests.get(f"{BASE_URL}/api/partners/branches/", headers=_h(), timeout=20)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:500]


def order_id_of(tx: dict):
    md = (tx or {}).get("metadata") or {}
    return _num(md.get("order_id"))


def amount_som(tx: dict) -> float:
    try:
        return float((tx or {}).get("amount")) / 100.0
    except (TypeError, ValueError):
        return -1.0

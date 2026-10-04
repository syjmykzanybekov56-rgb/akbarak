"""
АкБарак (сайт) — интеграция с GoPay.kg
Создание платежа и проверка вебхук-подписи.

Требует переменные окружения на Railway:
    GOPAY_API_KEY
    GOPAY_SECRET
    GOPAY_WEBHOOK_SECRET   (отдельный секрет из кабинета GoPay для вебхуков)
    GOPAY_BASE_URL (опционально, по умолчанию https://api.gopay.kg/v1)
"""

import os
import hmac
import hashlib
import secrets
import json
import logging
import requests

logger = logging.getLogger(__name__)

GOPAY_API_KEY = os.getenv("GOPAY_API_KEY", "")
GOPAY_SECRET = os.getenv("GOPAY_SECRET", "")
GOPAY_WEBHOOK_SECRET = os.getenv("GOPAY_WEBHOOK_SECRET", "")
GOPAY_BASE_URL = os.getenv("GOPAY_BASE_URL", "https://api.gopay.kg/v1")


def _sign(body: str, nonce: str, secret: str) -> str:
    """HMAC-SHA512(nonce + '\\n' + body + '\\n', secret).hexdigest().upper() — формула из доки GoPay."""
    payload = f"{nonce}\n{body}\n".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest().upper()


def _headers(body: str) -> dict:
    nonce = secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "GoPay-Api-Key": GOPAY_API_KEY,
        "GoPay-Nonce": nonce,
        "GoPay-Signature": _sign(body, nonce, GOPAY_SECRET),
    }


def create_payment(order_id: int, amount_som: int, description: str = ""):
    """Создаёт платёж в GoPay. Возвращает dict с payment_id и checkout_url, либо None при ошибке."""
    payload = {
        "amount": amount_som,
        "currency": "KGS",
        "order_id": f"{order_id}-{secrets.token_hex(4)}",  # уникально на каждую попытку оплаты — GoPay не разрешает повторы
        "description": description or f"АкБарак заказ #{order_id}",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        resp = requests.post(
            f"{GOPAY_BASE_URL}/payments",
            data=body.encode("utf-8"),
            headers=_headers(body),
            timeout=15,
        )
        logger.info(f"GoPay create_payment: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") != "OK":
            logger.error(f"GoPay create_payment отклонён (заказ #{order_id}): {result}")
            return None
        data = result.get("data", {})
        return {
            "payment_id": data.get("payment_id"),
            "checkout_url": data.get("checkout_url"),
            "qr_url": data.get("qr_url"),
        }
    except Exception as e:
        logger.error(f"GoPay create_payment ошибка (заказ #{order_id}): {e}")
        return None


def verify_webhook(nonce: str, raw_body: str, signature: str) -> bool:
    """Проверяет подпись входящего вебхука отдельным webhook_secret."""
    if not GOPAY_WEBHOOK_SECRET or not nonce or not signature:
        return False
    expected = _sign(raw_body, nonce, GOPAY_WEBHOOK_SECRET)
    return hmac.compare_digest(expected, signature.upper())

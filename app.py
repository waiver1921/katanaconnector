# app.py — Код 1: Shopify webhook → Katana SO sync
# Делает только три вещи:
#   1. Принимает и верифицирует webhook от Shopify
#   2. Ждёт пока Katana создаст SO (polling)
#   3. Обновляет delivery_date + additional_info (теги + notes)

import os
import hmac
import hashlib
import base64
import time
import re
import logging
import smtplib
import threading
from email.message import EmailMessage
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SHOPIFY_WEBHOOK_SECRET = os.environ["SHOPIFY_WEBHOOK_SECRET"]
KATANA_API_KEY         = os.environ["KATANA_API_KEY"]
NOTIFY_EMAIL           = os.environ["NOTIFY_EMAIL"]
SMTP_HOST              = os.environ["SMTP_HOST"]        # e.g. smtp.gmail.com
SMTP_PORT              = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER              = os.environ["SMTP_USER"]
SMTP_PASS              = os.environ["SMTP_PASS"]

KATANA_BASE    = "https://api.katanamrp.com/v1"
KATANA_HEADERS = {
    "Authorization": f"Bearer {KATANA_API_KEY}",
    "Content-Type":  "application/json",
}

# Idempotency: помним какие order_id уже обработали
# Сбрасывается при рестарте — для MVP достаточно
processed_orders: set = set()


# ─── Email ────────────────────────────────────────────────────────────────────

def send_alert(subject: str, body: str):
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[Caviar MRP] {subject}"
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log.info(f"Alert sent: {subject}")
    except Exception as e:
        log.error(f"Failed to send alert: {e}")


# ─── Parsing helpers ──────────────────────────────────────────────────────────

def parse_tags(raw_tags: str) -> tuple[str | None, list[str]]:
    """
    Формат даты в теге: DD-MM-YYYY (например "05-12-2026" = 5 декабря 2026).
    Katana ожидает YYYY-MM-DD — конвертируем при парсинге.

    Тег с датой распознаётся по паттерну DD-MM-YYYY, других тегов
    такого вида не бывает.

    Пример входа:  "05-12-2026, vip, fragile"
    Результат:     delivery_date="2026-12-05", other_tags=["vip", "fragile"]
    """
    if not raw_tags:
        return None, []

    tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    delivery_date = None
    other_tags    = []

    for tag in tags:
        match = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", tag)
        if match:
            day, month, year = match.group(1), match.group(2), match.group(3)
            # Валидируем что это реальная дата (например 32-13-2026 упадёт здесь)
            try:
                datetime.strptime(f"{day}-{month}-{year}", "%d-%m-%Y")
                delivery_date = f"{year}-{month}-{day}"  # → YYYY-MM-DD для Katana
            except ValueError:
                log.warning(f"Tag looks like a date but is invalid: '{tag}'")
                other_tags.append(tag)  # не теряем тег, кладём в other_tags
        else:
            other_tags.append(tag)

    return delivery_date, other_tags


def build_additional_info(other_tags: list[str], notes: str) -> str | None:
    """
    Склеивает остальные теги и notes в одну строку для additional_info.

    Пример результата:
        "Tags: vip, fragile | Notes: Handle carefully"

    Если что-то пустое — не добавляем лишних разделителей.
    """
    parts = []
    if other_tags:
        parts.append(f"Tags: {', '.join(other_tags)}")
    if notes and notes.strip():
        parts.append(f"Notes: {notes.strip()}")

    return " | ".join(parts) if parts else None


# ─── Katana API ───────────────────────────────────────────────────────────────

def katana_get(path: str, params: dict = None) -> dict | list | None:
    try:
        r = requests.get(
            f"{KATANA_BASE}{path}",
            headers=KATANA_HEADERS,
            params=params,
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Katana GET {path} failed: {e}")
        return None


def katana_patch(path: str, payload: dict) -> dict | None:
    try:
        r = requests.patch(
            f"{KATANA_BASE}{path}",
            headers=KATANA_HEADERS,
            json=payload,
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Katana PATCH {path} failed: {e}")
        return None


def find_katana_so(shopify_order_number: str, max_wait: int = 120) -> dict | None:
    """
    Polling: каждые 10 сек спрашиваем Katana пока не найдём SO.
    Ищем по номеру заказа Shopify (Katana коннектор пишет его в order_no).
    Максимум 120 сек = 12 попыток.
    """
    log.info(f"Polling Katana for SO #{shopify_order_number} (max {max_wait}s)")

    attempts = max_wait // 10

    for attempt in range(1, attempts + 1):
        time.sleep(10)

        # Katana коннектор может писать номер как "1234" или "#1234"
        for search_term in [shopify_order_number, f"#{shopify_order_number}"]:
            result = katana_get("/sales-orders", params={"search": search_term})
            if not result:
                continue

            orders = result.get("data", result) if isinstance(result, dict) else result

            for so in (orders if isinstance(orders, list) else []):
                so_order_no = str(so.get("order_no",    "")).lstrip("#")
                so_ext_id   = str(so.get("external_id", ""))
                if so_order_no == shopify_order_number or so_ext_id == shopify_order_number:
                    log.info(f"Found SO id={so['id']} on attempt {attempt}")
                    return so

        log.info(f"SO not found yet, attempt {attempt}/{attempts}")

    log.error(f"SO for order #{shopify_order_number} not found after {max_wait}s")
    return None


# ─── Core logic ───────────────────────────────────────────────────────────────

def process_order(order: dict):
    """
    Основная логика для одного Shopify заказа.
    Запускается в отдельном треде чтобы не держать соединение с Shopify.
    """
    order_id     = str(order.get("id"))
    order_number = str(order.get("order_number", "")).strip("#")
    raw_tags     = order.get("tags", "") or ""
    notes        = order.get("note",  "") or ""

    log.info(f"▶ Processing order #{order_number} (id={order_id})")

    # 1. Ждём SO в Katana
    so = find_katana_so(order_number)
    if not so:
        send_alert(
            f"SO not found — order #{order_number}",
            f"Shopify order id: {order_id}\n"
            f"Katana SO was not created within 120 seconds.\n"
            f"Please check the Katana-Shopify connector and update SO manually."
        )
        return

    so_id = so["id"]
    log.info(f"Found SO id={so_id}")

    # 2. Парсим теги: дата → delivery_date, остальные → other_tags
    delivery_date, other_tags = parse_tags(raw_tags)

    if not delivery_date:
        log.warning(f"No delivery date found in tags: '{raw_tags}'")
        send_alert(
            f"No delivery date — order #{order_number}",
            f"SO id={so_id}\n"
            f"Tags received: '{raw_tags}'\n"
            f"delivery_date was NOT set. Please update SO manually in Katana."
        )

    # 3. Собираем additional_info: остальные теги + notes
    additional_info = build_additional_info(other_tags, notes)

    # 4. Патчим SO
    payload = {}
    if delivery_date:
        payload["delivery_date"] = delivery_date
    if additional_info:
        payload["additional_info"] = additional_info

    if not payload:
        log.info(f"Nothing to update for SO {so_id} — no date, no tags, no notes")
        return

    log.info(f"Updating SO {so_id}: {payload}")
    result = katana_patch(f"/sales-orders/{so_id}", payload)

    if result:
        log.info(f"✓ SO {so_id} updated successfully")
    else:
        send_alert(
            f"Failed to update SO — order #{order_number}",
            f"SO id={so_id}\n"
            f"Attempted to set: {payload}\n"
            f"Katana PATCH returned an error. Please update manually."
        )


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

@app.route("/webhook/shopify/order-created", methods=["POST"])
def shopify_order_created():
    raw_body    = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")

    # Верифицируем подпись Shopify
    expected = base64.b64encode(
        hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).digest()
    ).decode("utf-8")

    if not hmac.compare_digest(expected, hmac_header):
        log.warning("Invalid HMAC signature — rejected")
        return jsonify({"error": "Unauthorized"}), 401

    order    = request.get_json(force=True)
    order_id = str(order.get("id"))

    # Идемпотентность: пропускаем дубли
    if order_id in processed_orders:
        log.info(f"Duplicate webhook for order {order_id} — skipped")
        return jsonify({"status": "duplicate"}), 200

    processed_orders.add(order_id)

    # Запускаем обработку в треде — сразу отвечаем Shopify 200
    # (иначе Shopify будет ждать и повторно слать webhook)
    threading.Thread(target=process_order, args=(order,), daemon=True).start()

    return jsonify({"status": "accepted"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "processed_orders_count": len(processed_orders)
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

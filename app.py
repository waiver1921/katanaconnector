# app.py
import os
import hmac
import hashlib
import time
import re
import json
import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Config from env ───────────────────────────────────────────────────────────
SHOPIFY_WEBHOOK_SECRET = os.environ["SHOPIFY_WEBHOOK_SECRET"]
KATANA_API_KEY         = os.environ["KATANA_API_KEY"]
NOTIFY_EMAIL           = os.environ["NOTIFY_EMAIL"]
SMTP_HOST              = os.environ["SMTP_HOST"]          # e.g. smtp.gmail.com
SMTP_PORT              = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER              = os.environ["SMTP_USER"]
SMTP_PASS              = os.environ["SMTP_PASS"]

KATANA_BASE = "https://api.katanamrp.com/v1"
KATANA_HEADERS = {
    "Authorization": f"Bearer {KATANA_API_KEY}",
    "Content-Type": "application/json",
}

# Simple in-memory idempotency store (достаточно для MVP)
# При рестарте сервиса очищается — для MVP это нормально
processed_orders: set = set()

# ─── Helpers ───────────────────────────────────────────────────────────────────

def verify_shopify_hmac(data: bytes, hmac_header: str) -> bool:
    """Проверяем подпись вебхука от Shopify."""
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        data,
        hashlib.sha256
    ).hexdigest()
    # Shopify шлёт base64, не hex — декодируем правильно
    import base64
    expected = base64.b64encode(
        hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
            data,
            hashlib.sha256
        ).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, hmac_header or "")


def send_alert(subject: str, body: str):
    """Отправляем алерт на email."""
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[Caviar MRP] {subject}"
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log.info(f"Alert sent: {subject}")
    except Exception as e:
        log.error(f"Failed to send alert email: {e}")


def parse_delivery_date(tags: str) -> str | None:
    """
    Парсим дату из строки тегов Shopify.
    Shopify возвращает теги как строку: "vip, delivery-2024-03-15, urgent"
    """
    if not tags:
        return None
    match = re.search(r"delivery-(\d{4}-\d{2}-\d{2})", tags)
    if not match:
        return None
    date_str = match.group(1)
    # Валидируем что это реальная дата
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        log.warning(f"Found delivery tag but invalid date: {date_str}")
        return None


def katana_get(path: str, params: dict = None) -> dict | list | None:
    """GET запрос к Katana API с базовой обработкой ошибок."""
    url = f"{KATANA_BASE}{path}"
    try:
        r = requests.get(url, headers=KATANA_HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Katana GET {path} failed: {e}")
        return None


def katana_patch(path: str, payload: dict) -> dict | None:
    """PATCH запрос к Katana API."""
    url = f"{KATANA_BASE}{path}"
    try:
        r = requests.patch(url, headers=KATANA_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Katana PATCH {path} failed: {e}")
        return None


def katana_post(path: str, payload: dict) -> dict | None:
    """POST запрос к Katana API."""
    url = f"{KATANA_BASE}{path}"
    try:
        r = requests.post(url, headers=KATANA_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Katana POST {path} failed: {e}")
        return None


def find_katana_so(shopify_order_number: str, max_wait: int = 120) -> dict | None:
    """
    Polling: ждём пока Katana-коннектор создаст Sales Order.
    Ищем по номеру заказа Shopify в поле order_no или external_id.
    
    Katana обычно создаёт SO в течение 30-60 сек после заказа Shopify.
    Даём 120 секунд с интервалом 10 сек = 12 попыток.
    """
    log.info(f"Polling Katana for SO with Shopify order #{shopify_order_number}")
    
    attempts = max_wait // 10
    for attempt in range(attempts):
        time.sleep(10)  # Всегда ждём перед первой попыткой тоже
        
        # Пробуем поиск по order_no (формат #1234 или 1234 — зависит от настроек коннектора)
        for search_term in [f"#{shopify_order_number}", shopify_order_number]:
            result = katana_get("/sales-orders", params={"search": search_term})
            if result and isinstance(result, dict) and result.get("data"):
                orders = result["data"]
                for so in orders:
                    # Проверяем несколько возможных полей где Katana хранит Shopify номер
                    if (str(so.get("order_no", "")).strip("#") == str(shopify_order_number) or
                        str(so.get("external_id", "")) == str(shopify_order_number)):
                        log.info(f"Found SO {so['id']} on attempt {attempt + 1}")
                        return so
        
        log.info(f"SO not found yet, attempt {attempt + 1}/{attempts}")
    
    log.error(f"SO for Shopify order #{shopify_order_number} not found after {max_wait}s")
    return None


def get_available_batches_fifo(product_id: int) -> list:
    """
    Получаем доступные батчи (банки) для продукта, отсортированные по FIFO.
    Фильтруем: status == 'in_stock' и quantity > 0.
    """
    result = katana_get("/batches", params={
        "product_id": product_id,
        "status": "in_stock",
        "per_page": 200  # Берём с запасом, у тебя не тысячи банок
    })
    
    if not result:
        return []
    
    batches = result.get("data", result) if isinstance(result, dict) else result
    
    # Фильтруем батчи с ненулевым количеством
    available = [
        b for b in batches
        if float(b.get("in_stock", 0)) > 0
    ]
    
    # Сортируем по дате создания — FIFO (старые первыми)
    available.sort(key=lambda b: b.get("created_at", ""))
    
    log.info(f"Found {len(available)} available batches for product {product_id}")
    return available


def assign_batches_to_so_line(so_id: int, so_row_id: int, 
                               product_id: int, quantity_needed: float) -> bool:
    """
    Привязываем батчи (CITES-банки) к строке Sales Order.
    Так как у тебя batch quantity = 1, берём N батчей для quantity N.
    
    Возвращает True если успешно, False если не хватает stock.
    """
    batches = get_available_batches_fifo(product_id)
    
    if not batches:
        log.error(f"No batches available for product {product_id}")
        return False
    
    total_available = sum(float(b.get("in_stock", 0)) for b in batches)
    if total_available < quantity_needed:
        log.error(
            f"Insufficient stock for product {product_id}: "
            f"need {quantity_needed}, have {total_available}"
        )
        return False
    
    # Назначаем батчи по одному (так как каждый = 1 банка с CITES-кодом)
    remaining = quantity_needed
    for batch in batches:
        if remaining <= 0:
            break
        
        batch_qty = min(float(batch.get("in_stock", 0)), remaining)
        
        payload = {
            "sales_order_id": so_id,
            "sales_order_row_id": so_row_id,
            "batch_id": batch["id"],
            "quantity": batch_qty,
        }
        
        result = katana_post("/batch-transactions", payload)
        if not result:
            log.error(f"Failed to create batch transaction for batch {batch['id']}")
            return False
        
        remaining -= batch_qty
        log.info(f"Assigned batch {batch['id']} (CITES: {batch.get('batch_number')}) qty={batch_qty}")
    
    return True


# ─── Main processing logic ─────────────────────────────────────────────────────

def process_shopify_order(order: dict):
    """
    Основная логика: получаем Shopify заказ, обновляем Katana SO.
    Запускается в основном потоке (для MVP синхронно, Railway даёт 30 сек timeout на webhook response — мы отвечаем 200 сразу).
    """
    order_id   = str(order.get("id"))
    order_name = str(order.get("order_number", order.get("name", ""))).strip("#")
    tags       = order.get("tags", "")
    note       = order.get("note", "") or ""
    line_items = order.get("line_items", [])
    
    log.info(f"Processing Shopify order #{order_name} (id={order_id})")
    
    # 1. Ищем SO в Katana (с polling)
    so = find_katana_so(order_name)
    if not so:
        send_alert(
            f"SO not found for Shopify order #{order_name}",
            f"Order ID: {order_id}\nKatana SO was not created within 120 seconds.\n"
            f"Please check the Katana-Shopify connector and create/update SO manually."
        )
        return
    
    so_id = so["id"]
    log.info(f"Found Katana SO id={so_id}")
    
    # 2. Обновляем delivery_date и additional_info
    delivery_date = parse_delivery_date(tags)
    
    update_payload = {}
    if delivery_date:
        update_payload["delivery_date"] = delivery_date
        log.info(f"Setting delivery_date = {delivery_date}")
    else:
        log.warning(f"No valid delivery tag found in: '{tags}'")
        send_alert(
            f"No delivery date for order #{order_name}",
            f"Order tags: '{tags}'\nSO {so_id} will not have delivery_date set.\n"
            f"Please update manually in Katana."
        )
    
    if note:
        update_payload["additional_info"] = note
        log.info(f"Setting additional_info = '{note[:50]}...' " if len(note) > 50 else f"Setting additional_info = '{note}'")
    
    if update_payload:
        result = katana_patch(f"/sales-orders/{so_id}", update_payload)
        if not result:
            send_alert(
                f"Failed to update SO #{so_id} for order #{order_name}",
                f"Could not set: {update_payload}\nPlease update manually."
            )
    
    # 3. Привязываем батчи к каждой line item
    # Получаем строки SO из Katana (нужны so_row_id и product_id)
    so_detail = katana_get(f"/sales-orders/{so_id}")
    if not so_detail:
        send_alert(
            f"Cannot fetch SO details for order #{order_name}",
            f"SO id={so_id} exists but details fetch failed. Batch assignment skipped."
        )
        return
    
    so_rows = so_detail.get("sales_order_rows", [])
    
    errors = []
    for item in line_items:
        sku      = item.get("sku", "")
        quantity = float(item.get("quantity", 1))
        name     = item.get("name", "")
        
        # Ищем соответствующую строку в Katana SO по SKU
        matching_row = None
        for row in so_rows:
            # Katana хранит SKU в разных местах в зависимости от версии API
            row_sku = (row.get("product", {}) or {}).get("sku") or row.get("sku", "")
            if row_sku == sku:
                matching_row = row
                break
        
        if not matching_row:
            msg = f"No matching SO row for SKU '{sku}' ({name})"
            log.warning(msg)
            errors.append(msg)
            continue
        
        product_id = matching_row.get("product_id") or (matching_row.get("product") or {}).get("id")
        so_row_id  = matching_row["id"]
        
        if not product_id:
            msg = f"Cannot determine product_id for SKU '{sku}'"
            log.error(msg)
            errors.append(msg)
            continue
        
        success = assign_batches_to_so_line(so_id, so_row_id, product_id, quantity)
        if not success:
            msg = f"Batch assignment failed for '{name}' (SKU: {sku}, qty: {quantity})"
            errors.append(msg)
    
    # 4. Отправляем сводный алерт если были ошибки
    if errors:
        send_alert(
            f"Partial errors processing order #{order_name}",
            f"SO id={so_id}\n\nErrors:\n" + "\n".join(f"- {e}" for e in errors) +
            "\n\nOther line items were processed successfully. Please check manually."
        )
        log.warning(f"Order #{order_name} processed with {len(errors)} error(s)")
    else:
        log.info(f"✓ Order #{order_name} fully processed")


# ─── Flask webhook endpoint ────────────────────────────────────────────────────

@app.route("/webhook/shopify/order-created", methods=["POST"])
def shopify_order_created():
    """
    Принимаем webhook от Shopify.
    Сразу отвечаем 200, обработка идёт синхронно (для MVP достаточно).
    """
    # 1. Проверяем HMAC подпись
    raw_body   = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    
    if not verify_shopify_hmac(raw_body, hmac_header):
        log.warning("Invalid HMAC — rejected webhook")
        return jsonify({"error": "Unauthorized"}), 401
    
    order = request.get_json(force=True)
    order_id = str(order.get("id"))
    
    # 2. Идемпотентность: пропускаем дубли
    if order_id in processed_orders:
        log.info(f"Duplicate webhook for order {order_id}, skipping")
        return jsonify({"status": "duplicate"}), 200
    
    processed_orders.add(order_id)
    
    # 3. Обрабатываем заказ
    # Для MVP: синхронно в том же запросе
    # Минус: Railway может прервать если > 30 сек (мы тратим до 120 сек на polling)
    # Решение ниже через threading
    import threading
    thread = threading.Thread(target=process_shopify_order, args=(order,))
    thread.daemon = True
    thread.start()
    
    # Сразу отвечаем Shopify чтобы не получить retry
    return jsonify({"status": "accepted"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "processed_orders": len(processed_orders)}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

import os
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

def get_telegram_credentials():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        from models import SystemSetting
        tg_conf = SystemSetting.get_val("telegram_config")
        if isinstance(tg_conf, dict):
            if tg_conf.get("bot_token"):
                bot_token = tg_conf.get("bot_token").strip()
            if tg_conf.get("chat_id"):
                chat_id = str(tg_conf.get("chat_id")).strip()
    except Exception as e:
        logger.warning(f"Failed to fetch telegram_config from DB: {e}")
    return bot_token, chat_id

def send_telegram_order_alert(order, bot_token=None, chat_id=None):
    """
    Dispatches order notification alert to Telegram.
    Non-blocking / failsafe: catches all network errors so checkout never fails.
    """
    if not bot_token or not chat_id:
        b_tok, c_id = get_telegram_credentials()
        bot_token = bot_token or b_tok
        chat_id = chat_id or c_id

    if not bot_token or not chat_id:
        logger.info("Telegram notification skipped: Bot Token or Chat ID not configured.")
        return False

    try:
        # Parse items
        items = []
        if hasattr(order, 'items_json') and order.items_json:
            try:
                items = json.loads(order.items_json) if isinstance(order.items_json, str) else order.items_json
            except Exception:
                items = []

        item_lines = []
        for idx, item in enumerate(items, 1):
            title = escape_tg_markdown(item.get('title') or item.get('name') or 'Item')
            qty = item.get('qty', 1)
            price = float(item.get('price', 0.0))
            var_name = escape_tg_markdown(item.get('variantName'))
            var_text = f" ({var_name})" if var_name else ""
            item_lines.append(f"  {idx}. {title}{var_text} x{qty} — ${price:.2f}")

        items_formatted = "\n".join(item_lines) if item_lines else "  (No items specified)"
        khr_equiv = int(float(order.total_usd or 0.0) * 4100)

        cust_name = escape_tg_markdown(order.customer_name)
        cust_phone = escape_tg_markdown(order.customer_phone)
        cust_addr = escape_tg_markdown(order.customer_address)
        promo_code = escape_tg_markdown(order.promo_code_used or 'None')
        tg_contact = escape_tg_markdown(getattr(order, 'telegram_name', None))
        tg_line = f"✈️ *Telegram:* {tg_contact}\n" if tg_contact else ""

        msg = (
            f"🛍️ *NEW ORDER ALERT #{order.id}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Customer:* {cust_name}\n"
            f"📞 *Phone:* `{cust_phone}`\n"
            f"{tg_line}"
            f"📍 *Address:* {cust_addr}\n"
            f"💵 *Total:* ${order.total_usd:.2f} ({khr_equiv:,} ៛)\n"
            f"🎟️ *Promo:* {promo_code}\n"
            f"📦 *Items:*\n{items_formatted}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=8.0)
        if resp.status_code != 200:
            logger.error(f"Telegram order alert failed ({resp.status_code}): {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Error sending Telegram alert: {e}")
        return False

def send_telegram_test_message(bot_token, chat_id):
    """
    Sends a test verification message to verify token and chat ID.
    Returns (success: bool, message: str)
    """
    if not bot_token:
        return False, "Bot token is missing"
    if not chat_id:
        return False, "Chat ID is missing"

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": (
                "✅ *Somphea Reak Telegram Alert Connected!*\n\n"
                "Your bot is now configured successfully. You will receive real-time notifications whenever a customer places an order."
            ),
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=5.0)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 200 and data.get("ok"):
            return True, "Test alert sent successfully! Check your Telegram chat."
        else:
            err = data.get("description", f"HTTP {resp.status_code}")
            return False, f"Telegram API error: {err}"
    except Exception as e:
        return False, f"Connection failed: {str(e)}"

def escape_tg_markdown(text):
    if not text:
        return ""
    # Strip markdown delimiters that could break Telegram entity parser
    for char in ['*', '_', '`', '[', ']']:
        text = str(text).replace(char, '')
    return text.strip()

def send_telegram_recovery_alert(user, note=None):
    """
    Dispatches account password recovery request alert to admin Telegram bot.
    """
    bot_token, chat_id = get_telegram_credentials()
    if not bot_token or not chat_id:
        logger.info("Telegram recovery alert skipped: Bot Token or Chat ID not configured.")
        return False

    try:
        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        cust_name = escape_tg_markdown(user.full_name or 'N/A')
        cust_phone = escape_tg_markdown(user.phone)
        safe_note = escape_tg_markdown(note)
        note_line = f"📝 *Note:* {safe_note}\n" if safe_note else ""
        msg = (
            f"🆘 *CUSTOMER ACCOUNT RECOVERY REQUEST*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Customer:* {cust_name}\n"
            f"📱 *Phone:* `{cust_phone}`\n"
            f"🪙 *Credit Balance:* {user.credit_balance} pts\n"
            f"{note_line}"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {now_str} UTC\n"
            f"👉 Go to Admin Panel -> Customers to reset and provide their password."
        )

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=4.0)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Error sending recovery Telegram alert: {e}")
        return False

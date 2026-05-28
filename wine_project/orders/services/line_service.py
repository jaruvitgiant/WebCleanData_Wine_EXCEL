"""
LINE Service — จัดการ LINE Messaging API

หน้าที่:
    - Verify LINE Webhook Signature (HMAC-SHA256)
    - Parse webhook events
    - สร้าง Flex Message สำหรับสรุปออเดอร์
    - สร้าง Flex Message สำหรับสรุปรายวัน
    - ส่ง reply/push message กลับ LINE
"""

import hashlib
import hmac
import base64
import json
import logging
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

LINE_API_BASE = 'https://api.line.me/v2/bot'


def _get_channel_secret() -> str:
    """ดึง LINE Channel Secret จาก settings"""
    secret = getattr(settings, 'LINE_CHANNEL_SECRET', '')
    if not secret:
        raise ValueError("LINE_CHANNEL_SECRET ไม่ได้ตั้งค่าใน settings")
    return secret


def _get_channel_token() -> str:
    """ดึง LINE Channel Access Token จาก settings"""
    token = getattr(settings, 'LINE_CHANNEL_ACCESS_TOKEN', '')
    if not token:
        raise ValueError("LINE_CHANNEL_ACCESS_TOKEN ไม่ได้ตั้งค่าใน settings")
    return token


def verify_signature(body: bytes, signature: str) -> bool:
    """
    ตรวจสอบ LINE Webhook Signature ด้วย HMAC-SHA256

    Args:
        body: request body (bytes)
        signature: ค่า X-Line-Signature จาก header

    Returns:
        True ถ้า signature ถูกต้อง
    """
    channel_secret = _get_channel_secret()
    hash_value = hmac.new(
        channel_secret.encode('utf-8'),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(hash_value).decode('utf-8')

    is_valid = hmac.compare_digest(signature, expected_signature)
    if not is_valid:
        logger.warning("LINE Signature ไม่ถูกต้อง!")
    return is_valid


def parse_webhook_events(body: dict) -> list[dict]:
    """
    แยก events จาก LINE Webhook body

    Returns:
        list ของ event dict ที่มี keys: type, reply_token, user_id, message, etc.
    """
    events = body.get('events', [])
    parsed = []

    for event in events:
        event_type = event.get('type', '')

        parsed_event = {
            'type': event_type,
            'reply_token': event.get('replyToken', ''),
            'timestamp': event.get('timestamp', 0),
        }

        # ดึง user ID จาก source
        source = event.get('source', {})
        parsed_event['user_id'] = source.get('userId', '')
        parsed_event['source_type'] = source.get('type', '')

        # ดึง message ถ้าเป็น message event
        if event_type == 'message':
            message = event.get('message', {})
            parsed_event['message_type'] = message.get('type', '')
            parsed_event['message_text'] = message.get('text', '')
            parsed_event['message_id'] = message.get('id', '')

        # ดึง postback data ถ้าเป็น postback event (สำหรับปุ่มยืนยัน/ยกเลิก)
        elif event_type == 'postback':
            parsed_event['postback_data'] = event.get('postback', {}).get('data', '')

        parsed.append(parsed_event)

    return parsed


def get_user_profile(user_id: str) -> dict:
    """
    ดึงข้อมูล profile ของ LINE user

    Returns:
        dict ที่มี keys: displayName, userId, pictureUrl, statusMessage
    """
    token = _get_channel_token()
    try:
        with httpx.Client() as client:
            response = client.get(
                f"{LINE_API_BASE}/profile/{user_id}",
                headers={'Authorization': f'Bearer {token}'},
                timeout=10.0,
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error("ดึง LINE profile ล้มเหลว (user: %s): %s", user_id, e)
        return {'displayName': 'Unknown', 'userId': user_id}


def send_reply(reply_token: str, messages: list[dict]) -> bool:
    """
    ส่ง reply message กลับ LINE

    Args:
        reply_token: reply token จาก webhook event
        messages: list ของ message objects (ส่งได้สูงสุด 5 messages)

    Returns:
        True ถ้าส่งสำเร็จ
    """
    token = _get_channel_token()
    try:
        with httpx.Client() as client:
            response = client.post(
                f"{LINE_API_BASE}/message/reply",
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                json={
                    'replyToken': reply_token,
                    'messages': messages[:5],  # LINE จำกัด 5 messages
                },
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info("ส่ง LINE reply สำเร็จ (token: %s...)", reply_token[:10])
            return True
    except Exception as e:
        logger.error("ส่ง LINE reply ล้มเหลว: %s", e)
        return False


def send_push_message(user_id: str, messages: list[dict]) -> bool:
    """
    ส่ง push message ไปหา user โดยตรง (ไม่ต้องใช้ reply token)

    Args:
        user_id: LINE user ID
        messages: list ของ message objects
    """
    token = _get_channel_token()
    try:
        with httpx.Client() as client:
            response = client.post(
                f"{LINE_API_BASE}/message/push",
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                json={
                    'to': user_id,
                    'messages': messages[:5],
                },
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info("ส่ง LINE push สำเร็จ (user: %s...)", user_id[:10])
            return True
    except Exception as e:
        logger.error("ส่ง LINE push ล้มเหลว: %s", e)
        return False


def send_text_reply(reply_token: str, text: str) -> bool:
    """ส่งข้อความ text ธรรมดากลับ LINE"""
    return send_reply(reply_token, [{'type': 'text', 'text': text}])


def build_order_flex_message(order) -> dict:
    """
    สร้าง Flex Message สำหรับสรุปออเดอร์

    แสดง:
        - รายการสินค้า + จำนวน + ราคา
        - ที่อยู่จัดส่ง
        - ราคารวม
        - ปุ่มยืนยัน / ยกเลิก

    Args:
        order: Order instance (with related items)

    Returns:
        dict — LINE Flex Message object
    """
    # สร้างรายการสินค้า
    product_items = []
    for item in order.items.all():
        # สถานะ match
        match_status = "✅" if item.product else "⚠️"

        # ชื่อสินค้า
        product_items.append({
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": f"{match_status} {item.product_name}",
                    "size": "sm",
                    "color": "#333333",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"x{item.quantity}",
                    "size": "sm",
                    "color": "#666666",
                    "flex": 1,
                    "align": "center",
                },
                {
                    "type": "text",
                    "text": f"฿{item.total_price:,.0f}",
                    "size": "sm",
                    "color": "#333333",
                    "flex": 2,
                    "align": "end",
                },
            ],
            "margin": "md",
        })

    # สร้าง unmatched warning ถ้ามีสินค้าที่ match ไม่เจอ
    unmatched_items = [item for item in order.items.all() if not item.product]
    warning_section = []
    if unmatched_items:
        warning_names = ', '.join([item.product_name for item in unmatched_items])
        warning_section = [
            {"type": "separator", "margin": "lg"},
            {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "text",
                    "text": f"⚠️ ไม่พบสินค้า: {warning_names}\nกรุณาตรวจสอบชื่ออีกครั้ง",
                    "size": "xs",
                    "color": "#E74C3C",
                    "wrap": True,
                }],
                "margin": "md",
            },
        ]

    # สร้าง address section
    address_section = []
    if order.shipping_address:
        address_section = [
            {"type": "separator", "margin": "lg"},
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": "📍 จัดส่ง",
                        "size": "sm",
                        "color": "#666666",
                        "flex": 2,
                    },
                    {
                        "type": "text",
                        "text": order.shipping_address,
                        "size": "sm",
                        "color": "#333333",
                        "flex": 5,
                        "wrap": True,
                    },
                ],
                "margin": "md",
            },
        ]

    flex_message = {
        "type": "flex",
        "altText": f"สรุปออเดอร์ #{order.pk} — ฿{order.total_price:,.0f}",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": "🍷 สรุปออเดอร์",
                        "weight": "bold",
                        "size": "lg",
                        "color": "#FFFFFF",
                    },
                    {
                        "type": "text",
                        "text": f"Order #{order.pk}",
                        "size": "xs",
                        "color": "#FFFFFFAA",
                    },
                ],
                "backgroundColor": "#5C0614",
                "paddingAll": "lg",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    # หัวข้อรายการ
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "สินค้า", "size": "xs",
                             "color": "#999999", "flex": 4},
                            {"type": "text", "text": "จำนวน", "size": "xs",
                             "color": "#999999", "flex": 1, "align": "center"},
                            {"type": "text", "text": "ราคา", "size": "xs",
                             "color": "#999999", "flex": 2, "align": "end"},
                        ],
                    },
                    {"type": "separator", "margin": "sm"},
                    # รายการสินค้า
                    *product_items,
                    # warning ถ้ามี
                    *warning_section,
                    # ที่อยู่จัดส่ง
                    *address_section,
                    # เส้นแบ่ง + ราคารวม
                    {"type": "separator", "margin": "lg"},
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {
                                "type": "text",
                                "text": "💰 รวมทั้งหมด",
                                "size": "md",
                                "weight": "bold",
                                "color": "#5C0614",
                                "flex": 3,
                            },
                            {
                                "type": "text",
                                "text": f"฿{order.total_price:,.0f}",
                                "size": "lg",
                                "weight": "bold",
                                "color": "#5C0614",
                                "flex": 2,
                                "align": "end",
                            },
                        ],
                        "margin": "lg",
                    },
                ],
                "paddingAll": "lg",
            },
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "✅ ยืนยันออเดอร์",
                            "data": f"action=confirm&order_id={order.pk}",
                        },
                        "style": "primary",
                        "color": "#27AE60",
                        "height": "sm",
                        "flex": 1,
                        "margin": "sm",
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "❌ ยกเลิก",
                            "data": f"action=cancel&order_id={order.pk}",
                        },
                        "style": "primary",
                        "color": "#E74C3C",
                        "height": "sm",
                        "flex": 1,
                        "margin": "sm",
                    },
                ],
                "paddingAll": "md",
            },
        },
    }

    return flex_message


def build_daily_summary_flex(
    total_orders: int,
    total_revenue: Decimal,
    top_products: list[dict],
    date_str: str,
) -> dict:
    """
    สร้าง Flex Message สำหรับสรุปออเดอร์รายวัน

    Args:
        total_orders: จำนวนออเดอร์ทั้งหมดของวัน
        total_revenue: ยอดขายรวม
        top_products: list ของ dict ที่มี name + total_quantity (เรียงจากมากไปน้อย)
        date_str: วันที่ (string)
    """
    # สร้างรายการ top products
    top_items = []
    for i, product in enumerate(top_products[:5], 1):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i - 1]
        top_items.append({
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": f"{medal} {product['name']}",
                    "size": "sm",
                    "color": "#333333",
                    "flex": 4,
                },
                {
                    "type": "text",
                    "text": f"{product['total_quantity']} ขวด",
                    "size": "sm",
                    "color": "#666666",
                    "flex": 2,
                    "align": "end",
                },
            ],
            "margin": "md",
        })

    flex_message = {
        "type": "flex",
        "altText": f"📊 สรุปยอดวันที่ {date_str}",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": "📊 สรุปออเดอร์รายวัน",
                        "weight": "bold",
                        "size": "lg",
                        "color": "#FFFFFF",
                    },
                    {
                        "type": "text",
                        "text": f"วันที่ {date_str}",
                        "size": "sm",
                        "color": "#FFFFFFAA",
                    },
                ],
                "backgroundColor": "#2C3E50",
                "paddingAll": "lg",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    # สถิติรวม
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {"type": "text", "text": "จำนวนออเดอร์",
                                     "size": "xs", "color": "#999999", "align": "center"},
                                    {"type": "text", "text": str(total_orders),
                                     "size": "xxl", "weight": "bold", "color": "#2C3E50",
                                     "align": "center"},
                                    {"type": "text", "text": "ออเดอร์",
                                     "size": "xs", "color": "#999999", "align": "center"},
                                ],
                                "flex": 1,
                            },
                            {"type": "separator"},
                            {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {"type": "text", "text": "ยอดขาย",
                                     "size": "xs", "color": "#999999", "align": "center"},
                                    {"type": "text", "text": f"฿{total_revenue:,.0f}",
                                     "size": "xl", "weight": "bold", "color": "#27AE60",
                                     "align": "center"},
                                    {"type": "text", "text": "บาท",
                                     "size": "xs", "color": "#999999", "align": "center"},
                                ],
                                "flex": 1,
                            },
                        ],
                    },
                    # สินค้าขายดี
                    {"type": "separator", "margin": "xl"},
                    {
                        "type": "text",
                        "text": "🏆 สินค้าขายดี",
                        "weight": "bold",
                        "size": "md",
                        "color": "#333333",
                        "margin": "lg",
                    },
                    *(
                        top_items
                        if top_items
                        else [
                            {
                                "type": "text",
                                "text": "ยังไม่มีออเดอร์วันนี้",
                                "size": "sm",
                                "color": "#999999",
                                "margin": "md",
                            }
                        ]
                    ),
                ],
                "paddingAll": "lg",
            },
        },
    }
    return flex_message


def build_status_update_message(order, new_status: str) -> dict:
    """สร้างข้อความแจ้งอัพเดทสถานะออเดอร์"""
    status_emojis = {
        'confirmed': '✅',
        'cancelled': '❌',
        'shipped': '🚚',
        'completed': '🎉',
    }
    emoji = status_emojis.get(new_status, '📋')
    status_display = dict(order.Status.choices).get(new_status, new_status)

    return {
        "type": "text",
        "text": (
            f"{emoji} ออเดอร์ #{order.pk}\n"
            f"สถานะ: {status_display}\n"
            f"ยอดรวม: ฿{order.total_price:,.0f}"
        ),
    }
# ตัวอย่างฟังก์ชันใน line_service.py (ถ้าคุณใช้ line-bot-sdk เวอร์ชันปกติ)
# เพิ่มลงในไฟล์ orders/services/line_service.py
from linebot.models import ImageSendMessage  # 👈 ตรวจสอบว่ามีบรรทัดอิมพอร์ตนี้อยู่ด้านบนด้วยนะครับ

def send_image_reply(reply_token: str, original_content_url: str, preview_image_url: str):
    """
    ส่งข้อความตอบกลับเป็นรูปภาพไปยัง LINE โดยใช้ LINE Messaging API
    """
    try:
        # สร้างโครงสร้าง Message รูปภาพตามสเปกของ LINE
        image_message = ImageSendMessage(
            original_content_url=original_content_url,
            preview_image_url=preview_image_url
        )
        
        # 💡 ส่งข้อความกลับไปหาลูกค้า (เปลี่ยน line_bot_api ให้ตรงกับชื่อตัวแปรที่ไฟล์คุณใช้)
        line_bot_api.reply_message(reply_token, image_message)
        logger.info("ส่งรูปภาพกลับไปที่ LINE สำเร็จ URL: %s", original_content_url)
        
    except Exception as e:
        logger.error("ส่งรูปภาพกลับไปที่ LINE ล้มเหลว: %s", e, exc_info=True)
        # ตกผ้าตายสำรอง: ถ้าส่งรูปพัง อย่างน้อยส่งข้อความบอกลิงก์บอกลูกค้าแทน
        send_text_reply(reply_token, f"🍷 ขออภัยค่ะ ไม่สามารถแสดงรูปภาพได้ คุณสามารถดูรูปได้ที่ลิงก์นี้แทนนะคะ: {original_content_url}")
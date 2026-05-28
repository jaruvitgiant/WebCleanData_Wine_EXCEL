"""
Gemini AI Service — วิเคราะห์ข้อความสั่งซื้อจากลูกค้าด้วย Gemini API

หน้าที่:
    - รับข้อความภาษาไทย/อังกฤษจากลูกค้า
    - ส่งไปให้ Gemini วิเคราะห์ด้วย structured prompt
    - คืนค่า JSON ที่มีข้อมูล: products, quantity, address
    - มี retry logic และ validation
"""

import json
import logging
import re
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)
SKU_PATTERN = re.compile(r'\b([A-Za-z]{2,4}\d{3})\b')
CUSTOMER_LINE_PATTERN = re.compile(
    r'^\s*คุณ(?P<name>[^\(\n]+?)\s*(?:\((?P<address>[^)]+)\))?\s*$',
    re.MULTILINE,
)
EACH_QUANTITY_PATTERN = re.compile(r'อย่างละ\s*(\d+)?\s*ขวด')
VIEW_IMAGE_HINT_PATTERN = re.compile(r'(ขอ|ดู|เห็น).*(รูป|ภาพ)|รูป.*(สินค้า|ไวน์|รหัส)|image|photo', re.IGNORECASE)
# แก้ไข Prompt ใน orders/services/gemini_service.py
# SYSTEM_PROMPT ตัวใหม่ — อัปเกรดสแกนรหัสสินค้าและความซับซ้อนของออเดอร์
# เพิ่มเคสขอดูรูปเข้าไปใน SYSTEM_PROMPT ของ gemini_service.py

SYSTEM_PROMPT = """คุณเป็นระบบ AI อัจฉริยะสำหรับร้านขายไวน์ชื่อ "DEEPLUS JUD HAI Co., Ltd."
หน้าที่ของคุณคือวิเคราะห์ข้อความจากผู้ใช้ และระบุ "action" ให้ถูกต้องจาก 3 กรณีนี้:

----------------------------------------------------------------
[กรณีที่ 1: หากผู้ใช้พิมพ์ข้อความ "สั่งซื้อสินค้า" หรือ "ส่งออเดอร์"]
ตอบกลับ: {"action": "create_order", "customer_name": "...", "products": [...], "address": "..."}

----------------------------------------------------------------
[กรณีที่ 2: หากผู้ใช้ต้องการ "ขอดูรายงานออเดอร์ประจำวัน"]
ตอบกลับ: {"action": "check_summary", "target_date": "YYYY-MM-DD"}

----------------------------------------------------------------
[กรณีที่ 3: หากผู้ใช้ต้องการ "ขอดูรูปภาพสินค้า" ตามรหัสสินค้า]
เช่น "ขอดูรูป AGL002 หน่อย", "ขอรูปไวน์รหัส KR008", "อยากเห็นรูปขวด le004"
ให้ตอบกลับในโครงสร้างนี้เท่านั้น:
{
    "action": "view_product_image",
    "target_sku": "รหัสสินค้าตัวพิมพ์ใหญ่ทั้งหมดที่ลูกค้าตามหา (เช่น 'AGL002', 'KR008')"
}

----------------------------------------------------------------
[กฎเหล็กในการหารหัสสินค้า (Product SKU)]:
รหัสสินค้าของร้านเราจะมีแพทเทิร์นคือ "ตัวอักษรภาษาอังกฤษ 2-4 ตัว ติดกับตัวเลข 3 ตัว" (เช่น AGL002, KR008)
"""


def _extract_json_from_text(text: str) -> dict:
    """
    ดึง JSON จากข้อความตอบกลับของ Gemini
    รองรับกรณีที่ Gemini ใส่ ```json ... ``` ครอบ
    """
    text = text.strip()

    # ลอง parse ตรงๆ ก่อน
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ลองหา JSON ในกรอบ markdown code block
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # ลองหา JSON ด้วย regex จับ { ... }
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"ไม่สามารถแปลง response เป็น JSON ได้: {text[:200]}")


def _extract_sku(text: str) -> str:
    """ดึง SKU จากข้อความและแปลงเป็นตัวพิมพ์ใหญ่"""
    if not text:
        return ''
    match = SKU_PATTERN.search(text)
    return match.group(1).upper() if match else ''


def _parse_structured_sku_order(message: str) -> dict[str, Any] | None:
    """
    Parse ออเดอร์แบบหลายบรรทัดที่ระบุ SKU โดยตรง
    ตัวอย่าง:
        คุณต้น (ส่งบ้านคุณอาร์ทลาดพร้าวพรุ่งนี้)
        อย่างละขวด
        RLV004
        DMC001
    """
    raw_message = message or ''
    skus = [sku.upper() for sku in SKU_PATTERN.findall(raw_message)]
    if not skus:
        return None

    # ถ้าเป็น intent ขอรูป ให้ส่งต่อไป flow ขอดูรูปสินค้า ไม่ตีความเป็นออเดอร์
    if VIEW_IMAGE_HINT_PATTERN.search(raw_message):
        return None

    quantity = 1
    each_quantity_match = EACH_QUANTITY_PATTERN.search(raw_message)
    if each_quantity_match and each_quantity_match.group(1):
        quantity = max(int(each_quantity_match.group(1)), 1)

    customer_name = None
    address = None
    customer_line_match = CUSTOMER_LINE_PATTERN.search(message or '')
    if customer_line_match:
        customer_name = customer_line_match.group('name').strip()
        address = (customer_line_match.group('address') or '').strip() or None

    products = [{'name': sku, 'quantity': quantity} for sku in skus]
    return {
        'action': 'create_order',
        'is_order': True,
        'customer_name': customer_name,
        'products': products,
        'address': address,
    }


def _validate_parsed_order(data: dict, original_message: str = '') -> dict:
    """
    ตรวจสอบความถูกต้องของ JSON ที่ Gemini วิเคราะห์ได้

    Returns:
        dict ที่ validated แล้ว หรือ raise ValueError ถ้าไม่ถูกต้อง
    """
    action = (data.get('action') or '').strip()

    # คำสั่งพิเศษ: ขอดูรูปสินค้าตามรหัส
    if action == 'view_product_image':
        target_sku = _extract_sku(str(data.get('target_sku') or '')) or _extract_sku(original_message)
        if not target_sku:
            raise ValueError("ไม่พบรหัสสินค้าในคำสั่งขอดูรูป")
        return {
            'action': 'view_product_image',
            'target_sku': target_sku,
            'is_order': False,
        }

    # คำสั่งพิเศษ: ดูรายงาน/สรุป
    if action == 'check_summary':
        return {
            'action': 'check_summary',
            'target_date': data.get('target_date'),
            'is_order': False,
        }

    # แชททั่วไป/ไม่ใช่ออเดอร์
    if data.get('is_order') is False or action == 'general_chat':
        return {
            'action': action or 'general_chat',
            'is_order': False,
            'message': data.get('message', ''),
        }

    # ตรวจสอบ products
    products = data.get('products', [])
    if not isinstance(products, list):
        raise ValueError("products ต้องเป็น array")

    validated_products = []
    for product in products:
        if not isinstance(product, dict):
            continue

        name = product.get('name', '').strip()
        if not name:
            continue  # ข้ามสินค้าที่ไม่มีชื่อ

        # ตรวจสอบ quantity
        try:
            quantity = int(product.get('quantity', 1))
            if quantity < 1:
                quantity = 1
        except (ValueError, TypeError):
            quantity = 1

        validated_products.append({
            'name': name,
            'quantity': quantity,
        })

    if not validated_products:
        raise ValueError("ไม่พบรายการสินค้าในข้อความ")

    return {
        'action': action or 'create_order',
        'customer_name': data.get('customer_name'),
        'products': validated_products,
        'address': data.get('address'),
    }


def analyze_message(message: str, max_retries: int = 3) -> dict[str, Any]:
    """
    วิเคราะห์ข้อความสั่งซื้อด้วย Ollama (Local LLM)

    Args:
        message: ข้อความจากลูกค้า
        max_retries: จำนวนครั้งที่ลองใหม่ถ้า fail

    Returns:
        dict ที่มี keys: customer_name, products, address
        หรือ dict ที่มี is_order=False ถ้าไม่ใช่ข้อความสั่งซื้อ

    Raises:
        ValueError: ถ้าวิเคราะห์ไม่สำเร็จหลังลองครบ max_retries
    """
    deterministic_result = _parse_structured_sku_order(message)
    if deterministic_result:
        logger.info("ตรวจพบข้อความออเดอร์แบบ SKU โดยตรง: %s", deterministic_result)
        return deterministic_result

    api_url = getattr(settings, 'OLLAMA_API_URL', 'http://localhost:11434').rstrip('/')
    model_name = getattr(settings, 'OLLAMA_MODEL', 'qwen2.5')

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Ollama attempt %d/%d — message: %s",
                attempt, max_retries, message[:100]
            )

            # เรียก Ollama API
            url = f"{api_url}/api/chat"
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": f"ข้อความลูกค้า: {message}"
                    }
                ],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.1
                }
            }

            # กำหนด timeout เผื่อกรณีที่ model ใช้เวลาโหลดเข้าหน่วยความจำหรือประมวลผลนานขึ้น
            response = httpx.post(url, json=payload, timeout=60.0)
            
            if response.status_code != 200:
                raise ValueError(f"Ollama API ส่งกลับสถานะ {response.status_code}: {response.text}")

            response_json = response.json()
            response_text = response_json.get('message', {}).get('content', '').strip()
            
            logger.debug("Ollama response: %s", response_text[:500])

            # แปลง response เป็น JSON
            parsed_data = _extract_json_from_text(response_text)

            # Validate ข้อมูล
            validated_data = _validate_parsed_order(parsed_data, original_message=message)

            logger.info("Ollama วิเคราะห์สำเร็จ: %s", validated_data)
            return validated_data

        except Exception as e:
            last_error = e
            logger.warning(
                "Ollama attempt %d/%d ล้มเหลว: %s",
                attempt, max_retries, str(e)
            )

    # ลองครบแล้วยัง fail
    error_msg = f"Ollama วิเคราะห์ไม่สำเร็จหลังลอง {max_retries} ครั้ง: {last_error}"
    logger.error(error_msg)
    raise ValueError(error_msg)


def is_summary_command(message: str) -> bool:
    """
    ตรวจสอบว่าข้อความเป็นคำสั่งขอสรุปหรือไม่

    รองรับ:
        - "สรุปออเดอร์วันนี้"
        - "สรุปยอดวันนี้"
        - "สรุป"
        - "ยอดวันนี้"
    """
    summary_keywords = ['สรุปออเดอร์', 'สรุปยอด', 'ยอดวันนี้', 'สรุปวันนี้']
    message_lower = message.strip().lower()
    return any(keyword in message_lower for keyword in summary_keywords)


def is_list_orders_command(message: str) -> bool:
    """
    ตรวจสอบว่าเป็นคำสั่งขอดูรายการออเดอร์ในแต่ละวันหรือไม่

    รองรับ:
        - "ดูออเดอร์วันนี้"
        - "รายการออเดอร์วันนี้"
        - "ขอรายการออเดอร์"
        - "ดูออเดอร์"
        - "รายการออเดอร์"
        - "ออเดอร์ทั้งหมดวันนี้"
    """
    keywords = ['ดูออเดอร์', 'รายการออเดอร์', 'ออเดอร์ทั้งหมด', 'ออเดอร์วันนี้']
    message_lower = message.strip().lower()
    return any(keyword in message_lower for keyword in keywords)


def is_confirm_command(message: str) -> bool:
    """ตรวจสอบว่าเป็นคำสั่งยืนยันออเดอร์"""
    confirm_keywords = ['ยืนยัน', 'ตกลง', 'ok', 'confirm', 'ใช่']
    return message.strip().lower() in confirm_keywords


def is_cancel_command(message: str) -> bool:
    """ตรวจสอบว่าเป็นคำสั่งยกเลิกออเดอร์"""
    cancel_keywords = ['ยกเลิก', 'cancel', 'ไม่เอา', 'ไม่']
    return message.strip().lower() in cancel_keywords

"""
Order Service — ประมวลผลออเดอร์จาก LINE Bot

Workflow หลัก:
    1. รับ raw message + LINE user ID
    2. Get or Create Customer
    3. ตรวจสอบว่าเป็น command พิเศษหรือไม่ (สรุป/ยืนยัน/ยกเลิก)
    4. ส่งไป Gemini วิเคราะห์
    5. Match สินค้าด้วย fuzzy matching
    6. สร้าง Order + OrderItems
    7. คำนวณราคารวม
    8. อัพเดท stock
    9. Return Order object
"""

import logging
import re
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum, F
from django.utils import timezone

from orders.models import Customer, Product, Order, OrderItem
from orders.services import gemini_service, product_matcher, line_service

logger = logging.getLogger(__name__)
SKU_PATTERN = re.compile(r'\b([A-Za-z]{2,4}\d{3})\b')
OFF_TOPIC_LIMIT = 3
OFF_TOPIC_TTL_SECONDS = 60 * 60 * 24
GROUP_INTRO_TTL_SECONDS = 60 * 60 * 24 * 30


def _off_topic_cache_key(line_user_id: str) -> str:
    return f"orders:offtopic:{line_user_id}"


def _intro_cache_key(line_user_id: str) -> str:
    return f"orders:group:intro:{line_user_id}"


def _increment_off_topic_counter(line_user_id: str) -> int:
    key = _off_topic_cache_key(line_user_id)
    current_count = int(cache.get(key, 0)) + 1
    cache.set(key, current_count, OFF_TOPIC_TTL_SECONDS)
    return current_count


def _reset_off_topic_counter(line_user_id: str) -> None:
    cache.delete(_off_topic_cache_key(line_user_id))


def get_or_create_customer(line_user_id: str) -> Customer:
    """
    ดึงหรือสร้าง Customer จาก LINE User ID
    ถ้าเป็นลูกค้าใหม่จะดึง display name จาก LINE profile
    """
    customer, created = Customer.objects.get_or_create(
        line_user_id=line_user_id,
        defaults={'display_name': ''},
    )

    if created:
        # ดึง display name จาก LINE
        try:
            profile = line_service.get_user_profile(line_user_id)
            customer.display_name = profile.get('displayName', '')
            customer.save(update_fields=['display_name'])
            logger.info("สร้างลูกค้าใหม่: %s (%s)", customer.display_name, line_user_id)
        except Exception as e:
            logger.warning("ดึง LINE profile ไม่ได้: %s", e)

    return customer

@transaction.atomic
def process_order_message(
    line_user_id: str,
    message: str,
    reply_token: str = '',
) -> dict[str, Any]:
    """
    ประมวลผลข้อความสั่งซื้อจาก LINE แบบครบ workflow
    """
    try:
        # 1. Get or Create Customer (ดึงโปรไฟล์ไลน์ขึ้นมาเป็นค่าเริ่มต้นก่อน)
        customer = get_or_create_customer(line_user_id)

        # 2. ส่งไปวิเคราะห์ข้อมูล
        logger.info("กำลังส่งข้อความไปประมวลผล: %s", message[:100])
        parsed_data = gemini_service.analyze_message(message)

        # ตรวจสอบว่าเป็นคำสั่ง "ขอดูรูปสินค้า" หรือไม่
        # === [เพิ่มและแก้ไขระบบดักจับคำสั่งพิเศษตรงนี้] ===
        action = parsed_data.get('action')
        
        # 📸 กรณีที่ 1: ลูกค้าขอ "ดูรูปภาพสินค้า"
        if action == "view_product_image":
            target_sku = (parsed_data.get('target_sku') or '').upper()
            if not target_sku:
                matched = SKU_PATTERN.search(message or '')
                target_sku = matched.group(1).upper() if matched else ''
            if reply_token and target_sku:
                try:
                    # ค้นหาสินค้าจากรหัส SKU (ไม่สนใจพิมพ์เล็กพิมพ์ใหญ่)
                    product = Product.objects.get(sku__iexact=target_sku)
                    
                    if product.image_url:
                        # ส่งเป็นลิงก์ข้อความเท่านั้น (ไม่เปิดบิล/ไม่ส่ง image message)
                        line_service.send_text_reply(
                            reply_token,
                            f"📷 รูปสินค้า {product.sku} ({product.name})\n{product.image_url}"
                        )
                        _reset_off_topic_counter(line_user_id)
                        return {'status': 'success', 'message': f'ส่งลิงก์รูปภาพ {target_sku} เรียบร้อย'}
                    else:
                        line_service.send_text_reply(reply_token, f"🍷 พบไวน์รหัส {target_sku} ({product.name}) แต่ยังไม่มีรูปภาพในระบบค่ะ")
                        _reset_off_topic_counter(line_user_id)
                        return {'status': 'success'}
                        
                except Product.DoesNotExist:
                    line_service.send_text_reply(reply_token, f"🔍 ไม่พบสินค้ารหัส '{target_sku}' ในระบบ กรุณาเช็กอีกครั้งนะคะ")
                    _reset_off_topic_counter(line_user_id)
                    return {'status': 'not_found'}

            if reply_token and not target_sku:
                line_service.send_text_reply(reply_token, "📷 กรุณาระบุรหัสสินค้า เช่น AGL002 แล้วลองใหม่อีกครั้งค่ะ")
                _reset_off_topic_counter(line_user_id)
                return {'status': 'invalid_sku'}

            return {'status': 'success'}

        # 📅 กรณีที่ 2: แอดมิน/ลูกค้าขอ "ดูรายงานออเดอร์ประจำวัน"
        elif action == "check_summary":
            if reply_token:
                # เรียกฟังก์ชันดึงรายงานประจำวัน (ส่ง message เข้าไปด้วยเพื่อเช็กคำว่า "เมื่อวาน")
                handle_list_daily_orders(
                    line_user_id=line_user_id, 
                    reply_token=reply_token, 
                    message=message
                )
            _reset_off_topic_counter(line_user_id)
            return {'status': 'success', 'message': 'ส่งรายงานสรุปยอดเรียบร้อย'}

        # -----------------------------------------------------------
        # ตรวจสอบความปลอดภัย: หากไม่ใช่การสั่งซื้อ (เช่น คุยเล่นทั่วไป หรือ AI งง)
        if parsed_data.get('is_order') is False or action == "general_chat":
            off_topic_count = _increment_off_topic_counter(line_user_id)
            if off_topic_count > OFF_TOPIC_LIMIT:
                logger.info(
                    "งดตอบคำถามนอกงานชั่วคราว user=%s count=%d",
                    line_user_id[:10], off_topic_count
                )
                return {
                    'status': 'ignored_off_topic',
                    'off_topic_count': off_topic_count,
                }
            if reply_token:
                line_service.send_text_reply(
                    reply_token,
                    parsed_data.get('message') or "🤔 พิมพ์รหัสสินค้า หรือแจ้งสั่งซื้อได้เลยค่ะ"
                )
            return {
                'status': 'not_order',
                'message': parsed_data.get('message', ''),
                'off_topic_count': off_topic_count,
            }
            
        # === [จบส่วนดักจับคำสั่งพิเศษ โค้ดด้านล่างจะไหลไปทำ Step 3 (Match สินค้า) ต่อตามปกติ] ===
        # ตรวจสอบว่าเป็นข้อความสั่งซื้อหรือไม่
        if parsed_data.get('is_order') is False:
            if reply_token:
                line_service.send_text_reply(
                    reply_token,
                    "🤔 ข้อความนี้ไม่ใช่การสั่งซื้อนะคะ\n"
                    "พิมพ์ชื่อไวน์ + จำนวน เพื่อสั่งซื้อได้เลยค่ะ\n"
                    "เช่น: \"ส่ง Margaux 2 ขวด ไปสุขุมวิท 24\""
                )
            return {'status': 'not_order', 'message': parsed_data.get('message', '')}

        # ✨ [แก้ไขจุดนี้] ดึงชื่อลูกค้าจากบิลที่ AI แกะได้ มาทับชื่อโปรไฟล์ไลน์
        ai_customer_name = parsed_data.get('customer_name')
        if ai_customer_name:
            customer.display_name = ai_customer_name
            customer.save(update_fields=['display_name'])
            logger.info("อัปเดตชื่อลูกค้าตามบิลเรียบร้อย: %s", ai_customer_name)

        # 3. Match สินค้า
        products_data = parsed_data.get('products', [])
        matched_products = product_matcher.match_products(products_data)

        # 4. สร้าง Order (ปลอดภัย ไร้กังวลเรื่องคอลัมน์เกิน)
        ai_customer_name = parsed_data.get('customer_name')

        order = Order.objects.create(
            customer=customer,
            customer_name=ai_customer_name, # 👈 ดึงชื่อจากบิลบันทึกลงฟิลด์ใหม่ตรงๆ
            raw_message=message,
            ai_parsed_data=parsed_data,
            shipping_address=parsed_data.get('address') or '',
            status=Order.Status.PENDING,
        )

        # 5. สร้าง OrderItems
        for item_data in matched_products:
            product = item_data.get('product')

            order_item = OrderItem.objects.create(
                order=order,
                product=product,
                product_name=item_data['name'],
                quantity=item_data['quantity'],
                unit_price=product.price if product else 0,
            )

        # 6. คำนวณราคารวม
        order.calculate_total()

        # 7. ส่ง Flex Message กลับ LINE
        if reply_token:
            flex_msg = line_service.build_order_flex_message(order)
            line_service.send_reply(reply_token, [flex_msg])
        _reset_off_topic_counter(line_user_id)

        logger.info(
            "สร้างออเดอร์ #%d สำเร็จ — %d รายการ, ฿%s",
            order.pk, order.items.count(), order.total_price
        )

        return {
            'status': 'success',
            'order_id': order.pk,
            'total_price': str(order.total_price),
            'items_count': order.items.count(),
        }

    except Exception as e:
        logger.error("ประมวลผลออเดอร์ล้มเหลว: %s", e, exc_info=True)

        if reply_token:
            line_service.send_text_reply(
                reply_token,
                "❌ ขออภัยค่ะ เกิดข้อผิดพลาดในการประมวลผลออเดอร์\n"
                "กรุณาลองใหม่อีกครั้งค่ะ"
            )

        return {'status': 'error', 'error': str(e)}

def handle_confirm_order(order_id: int, line_user_id: str) -> dict[str, Any]:
    """
    ยืนยันออเดอร์ + อัพเดท stock

    Args:
        order_id: ID ของออเดอร์
        line_user_id: LINE User ID เพื่อตรวจสอบสิทธิ์
    """
    try:
        order = Order.objects.get(pk=order_id, customer__line_user_id=line_user_id)

        if order.status != Order.Status.PENDING:
            return {
                'status': 'error',
                'message': f'ออเดอร์นี้มีสถานะ "{order.get_status_display()}" แล้ว',
            }

        with transaction.atomic():
            # อัพเดท stock
            for item in order.items.filter(product__isnull=False):
                product = item.product
                if product.stock < item.quantity:
                    return {
                        'status': 'error',
                        'message': f'สินค้า {product.name} มีเหลือเพียง {product.stock} ขวด',
                    }
                product.stock = F('stock') - item.quantity
                product.save(update_fields=['stock', 'updated_at'])

            # อัพเดทสถานะ
            order.status = Order.Status.CONFIRMED
            order.save(update_fields=['status', 'updated_at'])

        # ส่งข้อความแจ้ง
        status_msg = line_service.build_status_update_message(order, 'confirmed')
        line_service.send_push_message(line_user_id, [status_msg])

        logger.info("ยืนยันออเดอร์ #%d สำเร็จ", order_id)
        return {'status': 'success', 'order_id': order_id}

    except Order.DoesNotExist:
        logger.warning("ไม่พบออเดอร์ #%d สำหรับ user %s", order_id, line_user_id)
        return {'status': 'error', 'message': 'ไม่พบออเดอร์'}
    except Exception as e:
        logger.error("ยืนยันออเดอร์ #%d ล้มเหลว: %s", order_id, e)
        return {'status': 'error', 'message': str(e)}


def handle_cancel_order(order_id: int, line_user_id: str) -> dict[str, Any]:
    """ยกเลิกออเดอร์"""
    try:
        order = Order.objects.get(pk=order_id, customer__line_user_id=line_user_id)

        if order.status not in (Order.Status.PENDING, Order.Status.CONFIRMED):
            return {
                'status': 'error',
                'message': f'ออเดอร์สถานะ "{order.get_status_display()}" ไม่สามารถยกเลิกได้',
            }

        with transaction.atomic():
            # คืน stock ถ้าสถานะเป็น confirmed (ได้หัก stock ไปแล้ว)
            if order.status == Order.Status.CONFIRMED:
                for item in order.items.filter(product__isnull=False):
                    item.product.stock = F('stock') + item.quantity
                    item.product.save(update_fields=['stock', 'updated_at'])

            order.status = Order.Status.CANCELLED
            order.save(update_fields=['status', 'updated_at'])

        status_msg = line_service.build_status_update_message(order, 'cancelled')
        line_service.send_push_message(line_user_id, [status_msg])

        logger.info("ยกเลิกออเดอร์ #%d สำเร็จ", order_id)
        return {'status': 'success', 'order_id': order_id}

    except Order.DoesNotExist:
        return {'status': 'error', 'message': 'ไม่พบออเดอร์'}
    except Exception as e:
        logger.error("ยกเลิกออเดอร์ #%d ล้มเหลว: %s", order_id, e)
        return {'status': 'error', 'message': str(e)}


def get_daily_summary(
    target_date: date | None = None,
) -> dict[str, Any]:
    """
    สรุปออเดอร์รายวัน

    Returns:
        dict ที่มี: total_orders, total_revenue, top_products, date_str
    """
    if target_date is None:
        target_date = timezone.localdate()

    # Query ออเดอร์ของวัน (ไม่นับที่ cancelled)
    orders = Order.objects.filter(
        created_at__date=target_date,
    ).exclude(status=Order.Status.CANCELLED)

    total_orders = orders.count()
    total_revenue = orders.aggregate(total=Sum('total_price'))['total'] or Decimal('0')

    # สินค้าขายดี
    top_products = (
        OrderItem.objects
        .filter(order__in=orders, product__isnull=False)
        .values(name=F('product__name'))
        .annotate(total_quantity=Sum('quantity'))
        .order_by('-total_quantity')[:5]
    )

    return {
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'top_products': list(top_products),
        'date_str': target_date.strftime('%d/%m/%Y'),
    }


def handle_daily_summary(line_user_id: str, reply_token: str = '') -> dict[str, Any]:
    """สรุปออเดอร์รายวัน แล้วส่ง Flex Message กลับ LINE"""
    try:
        summary = get_daily_summary()

        if reply_token:
            flex_msg = line_service.build_daily_summary_flex(
                total_orders=summary['total_orders'],
                total_revenue=summary['total_revenue'],
                top_products=summary['top_products'],
                date_str=summary['date_str'],
            )
            line_service.send_reply(reply_token, [flex_msg])

        return {'status': 'success', **summary}

    except Exception as e:
        logger.error("สรุปรายวันล้มเหลว: %s", e)
        if reply_token:
            line_service.send_text_reply(reply_token, "❌ ไม่สามารถสรุปข้อมูลได้ กรุณาลองใหม่")
        return {'status': 'error', 'error': str(e)}

def handle_list_daily_orders(line_user_id: str, reply_token: str = '', message: str = '') -> dict[str, Any]:
    """รายการออเดอร์ของวันนี้หรือเมื่อวาน แล้วส่งกลับ LINE"""
    try:
        from datetime import timedelta
        target_date = timezone.localdate()
        date_label = "วันนี้"

        # รองรับเมื่อวาน
        if "เมื่อวาน" in message:
            target_date = target_date - timedelta(days=1)
            date_label = "เมื่อวาน"

        # Query ออเดอร์ของวัน
        orders = Order.objects.filter(
            created_at__date=target_date,
        ).order_by('created_at')

        date_str = target_date.strftime('%d/%m/%Y')

        if not orders.exists():
            text = f"📅 รายการออเดอร์ของ{date_label} ({date_str}):\n\nยังไม่มีออเดอร์ในระบบค่ะ"
        else:
            lines = [f"📅 รายการออเดอร์ของ{date_label} ({date_str}):"]
            lines.append("-----------------------------")
            for i, order in enumerate(orders, 1):
                status_display = order.get_status_display()
                
                # ✨ [แก้ไขจุดนี้] เปลี่ยนจาก order.customer.display_name มาเป็นชื่อที่แกะจากบิล
                # ดึงชื่อลูกค้าจาก AI Parsed Data ก่อน
                # ดึงชื่อลูกค้าตามบิล
                customer = (
                    order.ai_parsed_data.get("customer_name")
                    if order.ai_parsed_data
                    else None
                )
                if not customer and order.customer:
                    customer = order.customer.display_name

                customer = customer or "ไม่ระบุชื่อ"
                
                # 📌 [เพิ่ม] ดึงข้อมูลที่อยู่จาก ai_parsed_data
                address = (
                    order.ai_parsed_data.get("address")
                    if order.ai_parsed_data
                    else None
                )
                customer_name = order.customer_name or (order.customer.display_name if order.customer else "ไม่ระบุชื่อ")
                lines.append(f"{i}. ออเดอร์ #{order.pk} ({status_display})")
                lines.append(f"   👤 คุณ {customer_name}")
                
                # 📌 [เพิ่ม] ถ้ามีที่อยู่ ให้พิมพ์บรรทัดที่อยู่เพิ่มลงไป
                if address:
                    lines.append(f"   📍 ที่อยู่: {address}")
                
                for item in order.items.all():
                    lines.append(f"     • {item.product_name} x{item.quantity} ขวด")
                
                lines.append(f"   💰 รวม: ฿{order.total_price:,.0f}")
                lines.append("") # blank line between orders
            
            # Remove last blank line if present
            if lines[-1] == "":
                lines.pop()
                
            lines.append("-----------------------------")
            # คำนวณยอดขายสุทธิของวันนั้น (ไม่รวม cancelled)
            total_revenue = orders.exclude(status=Order.Status.CANCELLED).aggregate(total=Sum('total_price'))['total'] or Decimal('0')
            lines.append(f"📦 ทั้งหมด {orders.count()} ออเดอร์")
            lines.append(f"💰 ยอดขายสุทธิ: ฿{total_revenue:,.0f}")
            text = "\n".join(lines)

        if reply_token:
            line_service.send_text_reply(reply_token, text)

        return {'status': 'success', 'text': text}

    except Exception as e:
        logger.error("แสดงรายการออเดอร์ล้มเหลว: %s", e, exc_info=True)
        if reply_token:
            line_service.send_text_reply(reply_token, "❌ ไม่สามารถดึงข้อมูลรายการออเดอร์ได้ กรุณาลองใหม่")
        return {'status': 'error', 'error': str(e)}

def handle_postback(postback_data: str, line_user_id: str) -> dict[str, Any]:
    """
    จัดการ postback event จากปุ่ม Flex Message

    postback_data format: "action=confirm&order_id=123"
    """
    try:
        params = parse_qs(postback_data)
        action = params.get('action', [''])[0]
        order_id = int(params.get('order_id', [0])[0])

        if action == 'confirm':
            return handle_confirm_order(order_id, line_user_id)
        elif action == 'cancel':
            return handle_cancel_order(order_id, line_user_id)
        else:
            logger.warning("Unknown postback action: %s", action)
            return {'status': 'error', 'message': f'Unknown action: {action}'}

    except (ValueError, KeyError) as e:
        logger.error("Parse postback data ล้มเหลว: %s (data: %s)", e, postback_data)
        return {'status': 'error', 'message': 'Invalid postback data'}


def handle_line_message(
    line_user_id: str,
    message: str,
    reply_token: str = '',
    source_type: str = 'user',
) -> dict[str, Any]:
    """
    Entry point หลัก — จัดการข้อความจาก LINE

    ตรวจสอบ command พิเศษก่อน แล้วค่อยส่งไป Gemini
    """
    message = message.strip()
    source_type = (source_type or 'user').strip().lower()

    # แนะนำตัวครั้งแรกเมื่อเริ่มคุยในกลุ่ม/ห้อง
    if source_type in {'group', 'room'} and reply_token:
        intro_key = _intro_cache_key(line_user_id)
        if not cache.get(intro_key):
            cache.set(intro_key, True, GROUP_INTRO_TTL_SECONDS)
            line_service.send_text_reply(
                reply_token,
                "🍷 สวัสดีค่ะ DEEPLUS Wine Bot\n"
                "ช่วยรับออเดอร์, สรุปรายวัน, และส่งลิงก์รูปสินค้าตามรหัสได้ค่ะ\n"
                "พิมพ์ตัวอย่าง: คุณต้น (ส่งลาดพร้าวพรุ่งนี้)\\nอย่างละขวด\\nRLV004"
            )
            return {'status': 'group_intro_sent'}

    # ตรวจสอบ command พิเศษ
    if gemini_service.is_summary_command(message):
        _reset_off_topic_counter(line_user_id)
        return handle_daily_summary(line_user_id, reply_token)

    if gemini_service.is_list_orders_command(message):
        _reset_off_topic_counter(line_user_id)
        return handle_list_daily_orders(line_user_id, reply_token, message)

    if gemini_service.is_confirm_command(message):
        _reset_off_topic_counter(line_user_id)
        # หา pending order ล่าสุดของ customer
        latest_order = Order.objects.filter(
            customer__line_user_id=line_user_id,
            status=Order.Status.PENDING,
        ).order_by('-created_at').first()

        if latest_order:
            return handle_confirm_order(latest_order.pk, line_user_id)
        else:
            if reply_token:
                line_service.send_text_reply(reply_token, "ไม่มีออเดอร์ที่รอยืนยันค่ะ")
            return {'status': 'no_pending_order'}

    if gemini_service.is_cancel_command(message):
        _reset_off_topic_counter(line_user_id)
        latest_order = Order.objects.filter(
            customer__line_user_id=line_user_id,
            status=Order.Status.PENDING,
        ).order_by('-created_at').first()

        if latest_order:
            return handle_cancel_order(latest_order.pk, line_user_id)
        else:
            if reply_token:
                line_service.send_text_reply(reply_token, "ไม่มีออเดอร์ที่จะยกเลิกค่ะ")
            return {'status': 'no_pending_order'}

    # ข้อความปกติ → ส่งไป Gemini วิเคราะห์
    return process_order_message(line_user_id, message, reply_token)

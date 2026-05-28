"""
Celery Tasks — Background tasks สำหรับระบบออเดอร์

Tasks:
    - process_line_message_task: ประมวลผลข้อความ LINE (Gemini + Order)
    - send_line_reply_task: ส่ง LINE reply message
    - update_stock_task: อัพเดท stock
    - send_daily_summary_task: สรุปออเดอร์รายวัน (periodic)
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name='orders.process_line_message',
)
def process_line_message_task(self, line_user_id: str, message: str, reply_token: str = ''):
    """
    Background task — ประมวลผลข้อความจาก LINE

    ใช้เมื่อต้องการ process แบบ async เพื่อไม่ให้ webhook timeout
    """
    try:
        from orders.services import order_service
        result = order_service.handle_line_message(
            line_user_id=line_user_id,
            message=message,
            reply_token=reply_token,
        )
        logger.info("Task process_line_message สำเร็จ: %s", result)
        return result
    except Exception as exc:
        logger.error("Task process_line_message ล้มเหลว: %s", exc)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=3,
    name='orders.send_line_reply',
)
def send_line_reply_task(self, reply_token: str, messages: list):
    """Background task — ส่ง LINE reply message"""
    try:
        from orders.services import line_service
        result = line_service.send_reply(reply_token, messages)
        return result
    except Exception as exc:
        logger.error("Task send_line_reply ล้มเหลว: %s", exc)
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=2,
    name='orders.send_line_push',
)
def send_line_push_task(self, user_id: str, messages: list):
    """Background task — ส่ง LINE push message"""
    try:
        from orders.services import line_service
        result = line_service.send_push_message(user_id, messages)
        return result
    except Exception as exc:
        logger.error("Task send_line_push ล้มเหลว: %s", exc)
        raise self.retry(exc=exc)


@shared_task(name='orders.send_daily_summary')
def send_daily_summary_task():
    """
    Periodic task — ส่งสรุปออเดอร์รายวันให้ admin

    ตั้ง schedule ใน settings:
        CELERY_BEAT_SCHEDULE = {
            'daily-summary': {
                'task': 'orders.send_daily_summary',
                'schedule': crontab(hour=23, minute=0),
            },
        }
    """
    try:
        from orders.services import order_service, line_service
        from orders.models import Customer

        summary = order_service.get_daily_summary()

        # สร้าง Flex Message
        flex_msg = line_service.build_daily_summary_flex(
            total_orders=summary['total_orders'],
            total_revenue=summary['total_revenue'],
            top_products=summary['top_products'],
            date_str=summary['date_str'],
        )

        # ส่งให้ admin ทุกคน (ลูกค้าคนแรก หรือสามารถกำหนด admin user IDs ใน settings)
        from django.conf import settings as django_settings
        admin_user_ids = getattr(django_settings, 'LINE_ADMIN_USER_IDS', [])

        for user_id in admin_user_ids:
            line_service.send_push_message(user_id, [flex_msg])

        logger.info(
            "ส่งสรุปรายวัน: %d ออเดอร์, ฿%s",
            summary['total_orders'], summary['total_revenue']
        )
        return summary

    except Exception as e:
        logger.error("Task send_daily_summary ล้มเหลว: %s", e)
        raise

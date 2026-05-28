from django.apps import AppConfig


class OrdersConfig(AppConfig):
    """แอป orders — ระบบรับออเดอร์ผ่าน LINE Bot + Gemini AI"""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders'
    verbose_name = 'ระบบออเดอร์ LINE Bot'

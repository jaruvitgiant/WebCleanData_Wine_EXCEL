"""
Celery Configuration — ตั้งค่า Celery สำหรับ background tasks

ใช้ Redis เป็น broker
ผูกกับ Django settings อัตโนมัติ
"""

import os
from celery import Celery

# ตั้งค่า Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wine_project.settings')

# สร้าง Celery app
app = Celery('wine_project')

# อ่าน config จาก Django settings (ใช้ prefix CELERY_)
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks จากทุก app ที่ลงทะเบียนใน INSTALLED_APPS
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Task สำหรับทดสอบ Celery"""
    print(f'Request: {self.request!r}')

"""
Orders Models — ระบบจัดการออเดอร์สำหรับ LINE Bot

Models:
    - Customer: เก็บข้อมูลลูกค้าที่ผูกกับ LINE User ID
    - Product: เก็บข้อมูลสินค้าไวน์ พร้อม stock และ aliases สำหรับ fuzzy matching
    - Order: เก็บข้อมูลออเดอร์ พร้อม status tracking
    - OrderItem: รายการสินค้าในแต่ละออเดอร์
"""

from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone


class Customer(models.Model):
    """ลูกค้า — ผูกกับ LINE User ID"""

    line_user_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        verbose_name='LINE User ID',
        help_text='User ID จาก LINE Platform',
    )
    display_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='ชื่อที่แสดง',
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        default='',
        verbose_name='เบอร์โทร',
    )
    address = models.TextField(
        blank=True,
        default='',
        verbose_name='ที่อยู่เริ่มต้น',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='วันที่สร้าง',
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='วันที่อัพเดท',
    )

    class Meta:
        verbose_name = 'ลูกค้า'
        verbose_name_plural = 'ลูกค้า'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.display_name or 'ไม่ระบุชื่อ'} ({self.line_user_id[:10]}...)"


class Product(models.Model):
    """สินค้าไวน์ — รองรับ fuzzy matching ผ่าน aliases"""
    image_url = models.URLField(
        verbose_name="ลิงก์รูปภาพสินค้า", 
        max_length=500, null=True, 
        blank=True
        )
    name = models.CharField(
        max_length=255,
        verbose_name='ชื่อสินค้า (อังกฤษ)',
        help_text='ชื่อสินค้าหลัก เช่น Chateau Margaux',
    )
    name_th = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='ชื่อสินค้า (ไทย)',
        help_text='ชื่อภาษาไทย เช่น ชาโตว์ มาร์โกซ์',
    )
    sku = models.CharField(
        max_length=50,
        unique=True,
        verbose_name='SKU',
        help_text='รหัสสินค้า',
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        verbose_name='ราคา (บาท)',
    )
    stock = models.PositiveIntegerField(
        default=0,
        verbose_name='จำนวนคงเหลือ',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='เปิดขาย',
    )
    aliases = models.JSONField(
        default=list,
        blank=True,
        verbose_name='ชื่อเรียกอื่นๆ',
        help_text='รายชื่ออื่นที่ใช้เรียกสินค้านี้ เช่น ["มาร์โกซ์", "Margaux"]',
    )
    description = models.TextField(
        blank=True,
        default='',
        verbose_name='รายละเอียด',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='วันที่สร้าง',
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='วันที่อัพเดท',
    )

    class Meta:
        verbose_name = 'สินค้า'
        verbose_name_plural = 'สินค้า'
        ordering = ['name']

    def __str__(self) -> str:
        return f"{self.name} (฿{self.price:,.0f}) [stock: {self.stock}]"

    @property
    def all_names(self) -> list[str]:
        """รวมชื่อทุกแบบสำหรับ fuzzy matching"""
        names = [self.name]
        if self.name_th:
            names.append(self.name_th)
        if self.aliases:
            names.extend(self.aliases)
        return names


class Order(models.Model):
    """ออเดอร์ — สร้างจากข้อความ LINE ที่ผ่าน Gemini AI วิเคราะห์แล้ว"""

    class Status(models.TextChoices):
        PENDING = 'pending', 'รอยืนยัน'
        CONFIRMED = 'confirmed', 'ยืนยันแล้ว'
        SHIPPED = 'shipped', 'จัดส่งแล้ว'
        COMPLETED = 'completed', 'เสร็จสิ้น'
        CANCELLED = 'cancelled', 'ยกเลิก'

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='orders',
        verbose_name='ลูกค้า',
    )
    customer_name = models.CharField(
        verbose_name='ชื่อลูกค้าตามบิล', 
        max_length=255, 
        null=True, 
        blank=True
    )
    raw_message = models.TextField(
        verbose_name='ข้อความดิบจาก LINE',
        help_text='ข้อความต้นฉบับที่ลูกค้าส่งมา',
    )
    ai_parsed_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name='ข้อมูลที่ AI วิเคราะห์',
        help_text='JSON ที่ Gemini AI วิเคราะห์ได้',
    )
    shipping_address = models.TextField(
        blank=True,
        default='',
        verbose_name='ที่อยู่จัดส่ง',
    )
    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name='ราคารวม (บาท)',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name='สถานะ',
    )
    note = models.TextField(
        blank=True,
        default='',
        verbose_name='หมายเหตุ',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='วันที่สั่ง',
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='วันที่อัพเดท',
    )

    class Meta:
        verbose_name = 'ออเดอร์'
        verbose_name_plural = 'ออเดอร์'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Order #{self.pk} — {self.customer.display_name} ({self.get_status_display()})"

    def calculate_total(self) -> None:
        """คำนวณราคารวมจาก OrderItems ทั้งหมด"""
        total = sum(
            item.total_price for item in self.items.all()
        )
        self.total_price = total
        self.save(update_fields=['total_price', 'updated_at'])


class OrderItem(models.Model):
    """รายการสินค้าในออเดอร์"""

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name='ออเดอร์',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items',
        verbose_name='สินค้า',
        help_text='ถ้า fuzzy match ไม่เจอจะเป็น null',
    )
    product_name = models.CharField(
        max_length=255,
        verbose_name='ชื่อสินค้าที่ลูกค้าระบุ',
        help_text='ชื่อที่ AI วิเคราะห์ได้จากข้อความ',
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name='จำนวน',
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name='ราคาต่อหน่วย (บาท)',
    )
    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name='ราคารวม (บาท)',
    )

    class Meta:
        verbose_name = 'รายการสินค้า'
        verbose_name_plural = 'รายการสินค้า'

    def __str__(self) -> str:
        return f"{self.product_name} x{self.quantity} (฿{self.total_price:,.0f})"

    def save(self, *args, **kwargs) -> None:
        """คำนวณราคารวมอัตโนมัติก่อน save"""
        # ถ้ามี product ที่ match ได้ ใช้ราคาจาก product
        if self.product and self.unit_price == 0:
            self.unit_price = self.product.price
        self.total_price = self.unit_price * self.quantity
        super().save(*args, **kwargs)

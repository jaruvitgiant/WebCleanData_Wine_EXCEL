"""
Django Admin — ปรับแต่ง Admin สำหรับระบบออเดอร์

Features:
    - CustomerAdmin: ค้นหาตาม display_name, line_user_id
    - ProductAdmin: ค้นหาตาม name, sku; จัดการ stock/price
    - OrderAdmin: search, filter, inline OrderItems, actions (confirm/cancel)
    - OrderItemInline: แสดง OrderItems ใน OrderAdmin
"""

from django.contrib import admin
from django.db.models import Sum, Count, F
from django.utils import timezone
from django.utils.html import format_html

from orders.models import Customer, Product, Order, OrderItem


# =============================================================================
# Inline — OrderItem ภายใน Order
# =============================================================================

class OrderItemInline(admin.TabularInline):
    """แสดง OrderItems แบบ inline ใน OrderAdmin"""
    model = OrderItem
    extra = 0
    readonly_fields = ['total_price']
    fields = ['product', 'product_name', 'quantity', 'unit_price', 'total_price']
    autocomplete_fields = ['product']


# =============================================================================
# CustomerAdmin
# =============================================================================

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    """Admin สำหรับจัดการลูกค้า"""

    list_display = [
        'display_name', 'line_user_id_short', 'phone',
        'order_count', 'created_at',
    ]
    list_filter = ['created_at']
    search_fields = ['display_name', 'line_user_id', 'phone']
    readonly_fields = ['line_user_id', 'created_at', 'updated_at']
    ordering = ['-created_at']

    def line_user_id_short(self, obj) -> str:
        """แสดง LINE User ID แบบย่อ"""
        return f"{obj.line_user_id[:15]}..."
    line_user_id_short.short_description = 'LINE User ID'

    def order_count(self, obj) -> int:
        """จำนวนออเดอร์ทั้งหมดของลูกค้า"""
        return obj.orders.count()
    order_count.short_description = 'จำนวนออเดอร์'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('orders')


# =============================================================================
# ProductAdmin
# =============================================================================

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Admin สำหรับจัดการสินค้า"""

    list_display = [
        'name', 'sku', 'price_display', 'stock_display',
        'image_url',  # 👈 1. เพิ่มให้โชว์ลิงก์รูปภาพในหน้าตารางรวมสินค้า
        'is_active', 'updated_at',
    ]
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'name_th', 'sku']
    list_editable = ['is_active']
    ordering = ['name']
    readonly_fields = ['created_at', 'updated_at']

    fieldsets = [
        ('ข้อมูลหลัก', {
            # 👈 2. เพิ่ม 'image_url' เข้าไปในกลุ่มนี้ เพื่อให้มีช่องกรอกตอนกดสร้าง/แก้ไขสินค้า
            'fields': ['name', 'name_th', 'sku', 'image_url', 'description'],
        }),
        ('ราคาและ Stock', {
            'fields': ['price', 'stock', 'is_active'],
        }),
        ('Fuzzy Matching', {
            'fields': ['aliases'],
            'description': 'ใส่ชื่ออื่นๆ ที่ใช้เรียกสินค้านี้ เช่น ["มาร์โกซ์", "Margaux"]',
        }),
        ('ข้อมูลระบบ', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    def price_display(self, obj) -> str:
        """แสดงราคาพร้อม format"""
        return f"฿{obj.price:,.0f}"
    price_display.short_description = 'ราคา'
    price_display.admin_order_field = 'price'

    def stock_display(self, obj) -> str:
        """แสดง stock พร้อมสีแดงถ้าน้อยกว่า 5"""
        if obj.stock < 5:
            return format_html(
                '<span style="color: #E74C3C; font-weight: bold;">{}</span>',
                obj.stock,
            )
        return str(obj.stock)
    stock_display.short_description = 'Stock'
    stock_display.admin_order_field = 'stock'


# =============================================================================
# OrderAdmin
# =============================================================================

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Admin สำหรับจัดการออเดอร์ — มี inline items + actions"""

    list_display = [
        'id', 'customer', 'status_badge', 'items_summary',
        'total_price_display', 'shipping_address_short', 'created_at',
    ]
    list_filter = ['status', 'created_at']
    search_fields = [
        'customer__display_name', 'customer__line_user_id',
        'raw_message', 'shipping_address',
    ]
    readonly_fields = [
        'raw_message', 'ai_parsed_data', 'total_price',
        'created_at', 'updated_at',
    ]
    list_per_page = 25
    ordering = ['-created_at']
    inlines = [OrderItemInline]
    actions = ['action_confirm', 'action_cancel', 'action_shipped']

    fieldsets = [
        ('ข้อมูลออเดอร์', {
            'fields': ['customer', 'status', 'shipping_address', 'note'],
        }),
        ('ราคา', {
            'fields': ['total_price'],
        }),
        ('ข้อมูลจาก LINE + AI', {
            'fields': ['raw_message', 'ai_parsed_data'],
            'classes': ['collapse'],
        }),
        ('ข้อมูลระบบ', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related('customer')
            .prefetch_related('items', 'items__product')
        )

    def status_badge(self, obj) -> str:
        """แสดงสถานะเป็น badge สี"""
        colors = {
            'pending': '#F39C12',
            'confirmed': '#27AE60',
            'shipped': '#3498DB',
            'completed': '#2ECC71',
            'cancelled': '#E74C3C',
        }
        color = colors.get(obj.status, '#95A5A6')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 10px; '
            'border-radius:12px; font-size:11px; font-weight:bold;">{}</span>',
            color, obj.get_status_display(),
        )
    status_badge.short_description = 'สถานะ'
    status_badge.admin_order_field = 'status'

    def items_summary(self, obj) -> str:
        """สรุปรายการสินค้าแบบย่อ"""
        items = obj.items.all()[:3]
        summary = ', '.join([f"{i.product_name} x{i.quantity}" for i in items])
        total = obj.items.count()
        if total > 3:
            summary += f" (+{total - 3} อื่นๆ)"
        return summary or '-'
    items_summary.short_description = 'รายการสินค้า'

    def total_price_display(self, obj) -> str:
        """แสดงราคารวม"""
        return f"฿{obj.total_price:,.0f}"
    total_price_display.short_description = 'ราคารวม'
    total_price_display.admin_order_field = 'total_price'

    def shipping_address_short(self, obj) -> str:
        """แสดงที่อยู่แบบย่อ"""
        if obj.shipping_address:
            return obj.shipping_address[:30] + ('...' if len(obj.shipping_address) > 30 else '')
        return '-'
    shipping_address_short.short_description = 'ที่อยู่จัดส่ง'

    # Admin Actions
    @admin.action(description='✅ ยืนยันออเดอร์ที่เลือก')
    def action_confirm(self, request, queryset):
        updated = queryset.filter(status=Order.Status.PENDING).update(
            status=Order.Status.CONFIRMED,
        )
        self.message_user(request, f"ยืนยัน {updated} ออเดอร์สำเร็จ")

    @admin.action(description='❌ ยกเลิกออเดอร์ที่เลือก')
    def action_cancel(self, request, queryset):
        updated = queryset.filter(
            status__in=[Order.Status.PENDING, Order.Status.CONFIRMED],
        ).update(status=Order.Status.CANCELLED)
        self.message_user(request, f"ยกเลิก {updated} ออเดอร์สำเร็จ")

    @admin.action(description='🚚 เปลี่ยนสถานะเป็น "จัดส่งแล้ว"')
    def action_shipped(self, request, queryset):
        updated = queryset.filter(status=Order.Status.CONFIRMED).update(
            status=Order.Status.SHIPPED,
        )
        self.message_user(request, f"อัพเดท {updated} ออเดอร์เป็น 'จัดส่งแล้ว'")


# =============================================================================
# Admin Site Configuration
# =============================================================================

admin.site.site_header = '🍷 DEEPLUS Wine — ระบบจัดการ'
admin.site.site_title = 'DEEPLUS Wine Admin'
admin.site.index_title = 'แดชบอร์ด'

"""
Serializers — Django REST Framework สำหรับ API

Serializers:
    - CustomerSerializer: ข้อมูลลูกค้า
    - ProductSerializer: ข้อมูลสินค้า
    - OrderItemSerializer: รายการสินค้าในออเดอร์
    - OrderSerializer: ข้อมูลออเดอร์แบบ nested (มี items + customer)
    - OrderCreateSerializer: สำหรับสร้างออเดอร์ผ่าน API
    - OrderSummarySerializer: สรุปออเดอร์รายวัน
"""

from rest_framework import serializers
from orders.models import Customer, Product, Order, OrderItem


class CustomerSerializer(serializers.ModelSerializer):
    """Serializer สำหรับข้อมูลลูกค้า"""

    class Meta:
        model = Customer
        fields = [
            'id', 'line_user_id', 'display_name',
            'phone', 'address', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class ProductSerializer(serializers.ModelSerializer):
    """Serializer สำหรับข้อมูลสินค้า"""

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'name_th', 'sku', 'price',
            'stock', 'is_active', 'aliases', 'description',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class OrderItemSerializer(serializers.ModelSerializer):
    """Serializer สำหรับรายการสินค้าในออเดอร์"""

    product_detail = ProductSerializer(source='product', read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            'id', 'product', 'product_detail', 'product_name',
            'quantity', 'unit_price', 'total_price',
        ]
        read_only_fields = ['id', 'total_price']


class OrderSerializer(serializers.ModelSerializer):
    """
    Serializer สำหรับข้อมูลออเดอร์แบบ nested
    รวม items และ customer info
    """

    items = OrderItemSerializer(many=True, read_only=True)
    customer_detail = CustomerSerializer(source='customer', read_only=True)
    status_display = serializers.CharField(
        source='get_status_display',
        read_only=True,
    )

    class Meta:
        model = Order
        fields = [
            'id', 'customer', 'customer_detail', 'raw_message',
            'ai_parsed_data', 'shipping_address', 'total_price',
            'status', 'status_display', 'note',
            'items', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'total_price', 'ai_parsed_data',
            'created_at', 'updated_at',
        ]


class OrderItemCreateSerializer(serializers.Serializer):
    """Serializer สำหรับรายการสินค้าตอนสร้างออเดอร์ผ่าน API"""

    product_id = serializers.IntegerField(required=False, allow_null=True)
    product_name = serializers.CharField(max_length=255)
    quantity = serializers.IntegerField(min_value=1, default=1)
    unit_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0,
    )


class OrderCreateSerializer(serializers.Serializer):
    """
    Serializer สำหรับสร้างออเดอร์ผ่าน API (POST /api/orders/)
    ใช้สำหรับ manual order creation (ไม่ผ่าน LINE)
    """

    customer_id = serializers.IntegerField(required=False, allow_null=True)
    line_user_id = serializers.CharField(max_length=255, required=False)
    raw_message = serializers.CharField(required=False, default='API Order')
    shipping_address = serializers.CharField(required=False, default='')
    items = OrderItemCreateSerializer(many=True)

    def validate_items(self, value):
        """ตรวจสอบว่ามีรายการสินค้าอย่างน้อย 1 รายการ"""
        if not value:
            raise serializers.ValidationError("ต้องมีรายการสินค้าอย่างน้อย 1 รายการ")
        return value

    def validate(self, data):
        """ตรวจสอบว่ามี customer_id หรือ line_user_id อย่างน้อยหนึ่งอย่าง"""
        if not data.get('customer_id') and not data.get('line_user_id'):
            raise serializers.ValidationError(
                "ต้องระบุ customer_id หรือ line_user_id"
            )
        return data


class OrderSummarySerializer(serializers.Serializer):
    """Serializer สำหรับ response สรุปออเดอร์รายวัน"""

    date = serializers.CharField()
    total_orders = serializers.IntegerField()
    total_revenue = serializers.DecimalField(max_digits=12, decimal_places=2)
    top_products = serializers.ListField(child=serializers.DictField())

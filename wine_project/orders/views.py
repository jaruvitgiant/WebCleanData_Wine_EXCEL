"""
Views — LINE Webhook + REST API endpoints

Endpoints:
    POST /webhook/line/         — รับ webhook จาก LINE Platform
    GET  /api/orders/           — รายการออเดอร์ทั้งหมด
    POST /api/orders/           — สร้างออเดอร์แบบ manual
    GET  /api/orders/<id>/      — รายละเอียดออเดอร์
    GET  /api/orders/summary/   — สรุปออเดอร์รายวัน
    GET  /api/products/         — รายการสินค้า
"""

import json
import logging
from datetime import date

from django.db import transaction
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.models import Customer, Product, Order, OrderItem
from orders.serializers import (
    ProductSerializer,
    OrderSerializer,
    OrderCreateSerializer,
    OrderSummarySerializer,
)
from orders.services import line_service, order_service

logger = logging.getLogger(__name__)


# =============================================================================
# LINE Webhook
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class LineWebhookView(View):
    """
    LINE Webhook Endpoint — POST /webhook/line/

    Flow:
        1. Verify LINE Signature (HMAC-SHA256)
        2. Parse webhook events
        3. แยกประเภท event (message / postback)
        4. ประมวลผลผ่าน order_service
    """

    def post(self, request):
        """รับ webhook จาก LINE"""
        try:
            # 1. ตรวจสอบ Signature
            signature = request.headers.get('X-Line-Signature', '')
            body = request.body

            if not signature:
                logger.warning("Webhook request ไม่มี X-Line-Signature")
                return HttpResponse('Missing signature', status=400)

            if not line_service.verify_signature(body, signature):
                logger.warning("LINE Signature ไม่ถูกต้อง")
                return HttpResponse('Invalid signature', status=403)

            # 2. Parse events
            body_json = json.loads(body.decode('utf-8'))
            events = line_service.parse_webhook_events(body_json)

            # 3. ประมวลผลแต่ละ event
            for event in events:
                self._handle_event(event)

            # LINE ต้องการ 200 OK เสมอ
            return HttpResponse('OK', status=200)

        except json.JSONDecodeError:
            logger.error("Webhook body ไม่ใช่ JSON ที่ถูกต้อง")
            return HttpResponse('Invalid JSON', status=400)
        except Exception as e:
            logger.error("Webhook error: %s", e, exc_info=True)
            # ส่ง 200 กลับเสมอเพื่อไม่ให้ LINE retry
            return HttpResponse('OK', status=200)

    def _handle_event(self, event: dict) -> None:
        """จัดการแต่ละ event จาก LINE"""
        event_type = event.get('type', '')
        user_id = event.get('user_id', '')
        reply_token = event.get('reply_token', '')

        if not user_id:
            logger.warning("Event ไม่มี user_id: %s", event)
            return

        if event_type == 'message' and event.get('message_type') == 'text':
            # ข้อความ text → ส่งไปประมวลผล
            message_text = event.get('message_text', '')
            if message_text:
                logger.info(
                    "รับข้อความจาก LINE user %s: %s",
                    user_id[:10], message_text[:50]
                )
                order_service.handle_line_message(
                    line_user_id=user_id,
                    message=message_text,
                    reply_token=reply_token,
                    source_type=event.get('source_type', 'user'),
                )

        elif event_type == 'postback':
            # Postback จากปุ่ม Flex Message
            postback_data = event.get('postback_data', '')
            if postback_data:
                logger.info(
                    "รับ postback จาก LINE user %s: %s",
                    user_id[:10], postback_data
                )
                order_service.handle_postback(postback_data, user_id)

        elif event_type == 'follow':
            # ผู้ใช้เพิ่มเพื่อน
            logger.info("ผู้ใช้ใหม่เพิ่มเพื่อน: %s", user_id)
            order_service.get_or_create_customer(user_id)
            if reply_token:
                line_service.send_text_reply(
                    reply_token,
                    "🍷 ยินดีต้อนรับสู่ DEEPLUS Wine!\n\n"
                    "สั่งซื้อไวน์ง่ายๆ แค่พิมพ์ชื่อไวน์ + จำนวน\n"
                    "เช่น: \"ส่ง Margaux 2 ขวด ไปสุขุมวิท 24\"\n\n"
                    "📊 พิมพ์ \"สรุปออเดอร์วันนี้\" เพื่อดูสรุปยอด"
                )
        else:
            logger.debug("ข้าม event type: %s", event_type)


# =============================================================================
# REST API — Pagination
# =============================================================================

class StandardPagination(PageNumberPagination):
    """Pagination มาตรฐานสำหรับ API"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


# =============================================================================
# REST API — Products
# =============================================================================

class ProductListView(generics.ListAPIView):
    """
    GET /api/products/ — รายการสินค้าทั้งหมด

    Query params:
        - search: ค้นหาตามชื่อ/SKU
        - active: true/false — กรองสินค้าที่เปิด/ปิดขาย
    """
    serializer_class = ProductSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        queryset = Product.objects.all()

        # Filter by active status
        active = self.request.query_params.get('active')
        if active is not None:
            queryset = queryset.filter(is_active=active.lower() == 'true')

        # Search
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                models.Q(name__icontains=search) |
                models.Q(name_th__icontains=search) |
                models.Q(sku__icontains=search)
            )

        return queryset


# =============================================================================
# REST API — Orders
# =============================================================================

class OrderListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/orders/ — รายการออเดอร์ทั้งหมด
    POST /api/orders/ — สร้างออเดอร์แบบ manual

    Query params (GET):
        - status: กรองตามสถานะ (pending/confirmed/shipped/completed/cancelled)
        - date: กรองตามวันที่ (YYYY-MM-DD)
        - customer: กรองตาม customer ID
    """
    pagination_class = StandardPagination

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return OrderCreateSerializer
        return OrderSerializer

    def get_queryset(self):
        queryset = Order.objects.select_related('customer').prefetch_related(
            'items', 'items__product'
        )

        # Filter by status
        order_status = self.request.query_params.get('status')
        if order_status:
            queryset = queryset.filter(status=order_status)

        # Filter by date
        date_str = self.request.query_params.get('date')
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
                queryset = queryset.filter(created_at__date=target_date)
            except ValueError:
                pass

        # Filter by customer
        customer_id = self.request.query_params.get('customer')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """สร้างออเดอร์แบบ manual ผ่าน API"""
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            # หา customer
            if data.get('customer_id'):
                customer = Customer.objects.get(pk=data['customer_id'])
            else:
                customer, _ = Customer.objects.get_or_create(
                    line_user_id=data['line_user_id'],
                )

            # สร้าง order
            order = Order.objects.create(
                customer=customer,
                raw_message=data.get('raw_message', 'API Order'),
                shipping_address=data.get('shipping_address', ''),
                status=Order.Status.PENDING,
            )

            # สร้าง order items
            for item_data in data['items']:
                product = None
                unit_price = item_data.get('unit_price', 0)

                if item_data.get('product_id'):
                    try:
                        product = Product.objects.get(pk=item_data['product_id'])
                        if unit_price == 0:
                            unit_price = product.price
                    except Product.DoesNotExist:
                        pass

                OrderItem.objects.create(
                    order=order,
                    product=product,
                    product_name=item_data['product_name'],
                    quantity=item_data['quantity'],
                    unit_price=unit_price,
                )

            # คำนวณราคารวม
            order.calculate_total()

            # Return response
            response_serializer = OrderSerializer(order)
            return Response(
                response_serializer.data,
                status=status.HTTP_201_CREATED,
            )

        except Customer.DoesNotExist:
            return Response(
                {'error': 'ไม่พบลูกค้า'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            logger.error("สร้างออเดอร์ผ่าน API ล้มเหลว: %s", e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class OrderDetailView(generics.RetrieveAPIView):
    """GET /api/orders/<id>/ — รายละเอียดออเดอร์"""

    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.select_related('customer').prefetch_related(
            'items', 'items__product'
        )


class OrderSummaryView(APIView):
    """
    GET /api/orders/summary/ — สรุปออเดอร์รายวัน

    Query params:
        - date: วันที่ (YYYY-MM-DD) ถ้าไม่ระบุใช้วันนี้
    """

    def get(self, request):
        """สรุปออเดอร์รายวัน"""
        try:
            date_str = request.query_params.get('date')
            target_date = None

            if date_str:
                try:
                    target_date = date.fromisoformat(date_str)
                except ValueError:
                    return Response(
                        {'error': 'รูปแบบวันที่ไม่ถูกต้อง ใช้ YYYY-MM-DD'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            summary = order_service.get_daily_summary(target_date)

            return Response({
                'date': summary['date_str'],
                'total_orders': summary['total_orders'],
                'total_revenue': str(summary['total_revenue']),
                'top_products': summary['top_products'],
            })

        except Exception as e:
            logger.error("API สรุปรายวันล้มเหลว: %s", e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

"""
URL Configuration — orders app

Endpoints:
    POST /webhook/line/         — LINE Webhook
    GET  /api/products/         — รายการสินค้า
    GET  /api/orders/           — รายการออเดอร์
    POST /api/orders/           — สร้างออเดอร์ manual
    GET  /api/orders/summary/   — สรุปรายวัน
    GET  /api/orders/<id>/      — รายละเอียดออเดอร์
"""

from django.urls import path
from orders import views

app_name = 'orders'

urlpatterns = [
    # LINE Webhook
    path('webhook/line/', views.LineWebhookView.as_view(), name='line-webhook'),

    # REST API — Products
    path('api/products/', views.ProductListView.as_view(), name='product-list'),

    # REST API — Orders
    path('api/orders/', views.OrderListCreateView.as_view(), name='order-list'),
    path('api/orders/summary/', views.OrderSummaryView.as_view(), name='order-summary'),
    path('api/orders/<int:pk>/', views.OrderDetailView.as_view(), name='order-detail'),
]

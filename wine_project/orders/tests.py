"""
orders app — Unit Tests

ทดสอบ:
    - Models (Customer, Product, Order, OrderItem)
    - Product Matcher (fuzzy matching)
    - Gemini Service (JSON parsing + validation)
    - Order Service (workflow)
    - API Endpoints
"""

from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory
from django.utils import timezone

from orders.models import Customer, Product, Order, OrderItem


class CustomerModelTest(TestCase):
    """ทดสอบ Customer model"""

    def test_create_customer(self):
        customer = Customer.objects.create(
            line_user_id='U1234567890abcdef',
            display_name='ทดสอบ',
        )
        self.assertEqual(customer.display_name, 'ทดสอบ')
        self.assertIn('U123456789', str(customer))

    def test_unique_line_user_id(self):
        Customer.objects.create(line_user_id='U111')
        with self.assertRaises(Exception):
            Customer.objects.create(line_user_id='U111')


class ProductModelTest(TestCase):
    """ทดสอบ Product model"""

    def test_create_product(self):
        product = Product.objects.create(
            name='Chateau Margaux',
            name_th='ชาโตว์ มาร์โกซ์',
            sku='WN001',
            price=Decimal('12000.00'),
            stock=50,
            aliases=['Margaux', 'มาร์โกซ์'],
        )
        self.assertEqual(product.name, 'Chateau Margaux')
        self.assertEqual(len(product.all_names), 4)  # name + name_th + 2 aliases

    def test_all_names_property(self):
        product = Product.objects.create(
            name='Latour', sku='WN002', price=Decimal('8500'),
            name_th='ลาตูร์', aliases=['La Tour'],
        )
        names = product.all_names
        self.assertIn('Latour', names)
        self.assertIn('ลาตูร์', names)
        self.assertIn('La Tour', names)


class OrderModelTest(TestCase):
    """ทดสอบ Order + OrderItem model"""

    def setUp(self):
        self.customer = Customer.objects.create(
            line_user_id='U_test_order',
            display_name='ลูกค้าทดสอบ',
        )
        self.product = Product.objects.create(
            name='Test Wine', sku='TW001', price=Decimal('5000'), stock=10,
        )

    def test_create_order_with_items(self):
        order = Order.objects.create(
            customer=self.customer,
            raw_message='ส่ง Test Wine 2 ขวด',
            status=Order.Status.PENDING,
        )
        item = OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name='Test Wine',
            quantity=2,
        )
        # OrderItem.save() จะคำนวณ unit_price + total_price อัตโนมัติ
        self.assertEqual(item.unit_price, Decimal('5000'))
        self.assertEqual(item.total_price, Decimal('10000'))

    def test_order_calculate_total(self):
        order = Order.objects.create(
            customer=self.customer,
            raw_message='test',
        )
        OrderItem.objects.create(
            order=order, product=self.product,
            product_name='Wine A', quantity=2,
        )
        OrderItem.objects.create(
            order=order, product=self.product,
            product_name='Wine B', quantity=1,
        )
        order.calculate_total()
        self.assertEqual(order.total_price, Decimal('15000'))


class GeminiServiceTest(TestCase):
    """ทดสอบ Gemini Service — JSON parsing + validation"""

    def test_extract_json_from_text(self):
        from orders.services.gemini_service import _extract_json_from_text

        # ทดสอบ JSON ตรงๆ
        result = _extract_json_from_text('{"products": [{"name": "Wine", "quantity": 1}]}')
        self.assertEqual(result['products'][0]['name'], 'Wine')

    def test_extract_json_from_markdown(self):
        from orders.services.gemini_service import _extract_json_from_text

        text = '```json\n{"products": [{"name": "Latour", "quantity": 2}]}\n```'
        result = _extract_json_from_text(text)
        self.assertEqual(result['products'][0]['quantity'], 2)

    def test_validate_parsed_order(self):
        from orders.services.gemini_service import _validate_parsed_order

        data = {
            'customer_name': None,
            'products': [{'name': 'Margaux', 'quantity': 2}],
            'address': 'สุขุมวิท 24',
        }
        result = _validate_parsed_order(data)
        self.assertEqual(len(result['products']), 1)
        self.assertEqual(result['address'], 'สุขุมวิท 24')

    def test_validate_empty_products(self):
        from orders.services.gemini_service import _validate_parsed_order

        data = {'products': []}
        with self.assertRaises(ValueError):
            _validate_parsed_order(data)

    def test_is_summary_command(self):
        from orders.services.gemini_service import is_summary_command

        self.assertTrue(is_summary_command('สรุปออเดอร์วันนี้'))
        self.assertTrue(is_summary_command('สรุปยอดวันนี้'))
        self.assertFalse(is_summary_command('ส่ง Margaux 2 ขวด'))

    @patch('httpx.post')
    def test_analyze_message_success(self, mock_post):
        from orders.services.gemini_service import analyze_message

        # Mock Ollama API response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'message': {
                'content': '{"customer_name": "เจษฎา", "products": [{"name": "Margaux", "quantity": 2}], "address": "สุขุมวิท 24"}'
            }
        }
        mock_post.return_value = mock_response

        # Execute
        result = analyze_message("ส่ง Margaux 2 ขวด ไปสุขุมวิท 24")

        # Assert
        self.assertEqual(result['customer_name'], 'เจษฎา')
        self.assertEqual(result['products'][0]['name'], 'Margaux')
        self.assertEqual(result['products'][0]['quantity'], 2)
        self.assertEqual(result['address'], 'สุขุมวิท 24')


class ProductMatcherTest(TestCase):
    """ทดสอบ Product Matcher — fuzzy matching"""

    def setUp(self):
        Product.objects.create(
            name='Chateau Margaux', name_th='ชาโตว์ มาร์โกซ์',
            sku='WN001', price=Decimal('12000'), stock=50,
            aliases=['Margaux', 'มาร์โกซ์'],
        )
        Product.objects.create(
            name='Chateau Latour', name_th='ชาโตว์ ลาตูร์',
            sku='WN002', price=Decimal('8500'), stock=30,
            aliases=['Latour', 'ลาตูร์', 'ลา ตูร์'],
        )

    def test_exact_match(self):
        from orders.services.product_matcher import match_product
        result = match_product('Chateau Margaux')
        self.assertTrue(result.is_matched)
        self.assertEqual(result.product.sku, 'WN001')

    def test_partial_match(self):
        from orders.services.product_matcher import match_product
        result = match_product('Margaux')
        self.assertTrue(result.is_matched)
        self.assertEqual(result.product.sku, 'WN001')

    def test_thai_match(self):
        from orders.services.product_matcher import match_product
        result = match_product('มาร์โกซ์')
        self.assertTrue(result.is_matched)
        self.assertEqual(result.product.sku, 'WN001')

    def test_fuzzy_match_space(self):
        from orders.services.product_matcher import match_product
        result = match_product('ลา ตูร์')
        self.assertTrue(result.is_matched)
        self.assertEqual(result.product.sku, 'WN002')

    def test_no_match(self):
        from orders.services.product_matcher import match_product
        result = match_product('xxxxxxxxxxxxxxxxxxxxx')
        self.assertFalse(result.is_matched)

    def test_match_products_batch(self):
        from orders.services.product_matcher import match_products
        results = match_products([
            {'name': 'Margaux', 'quantity': 2},
            {'name': 'Latour', 'quantity': 1},
        ])
        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0]['product'])
        self.assertIsNotNone(results[1]['product'])


class APIEndpointTest(TestCase):
    """ทดสอบ REST API endpoints"""

    def setUp(self):
        self.customer = Customer.objects.create(
            line_user_id='U_api_test', display_name='API Test',
        )
        self.product = Product.objects.create(
            name='API Wine', sku='API001', price=Decimal('5000'), stock=10,
        )

    def test_product_list_api(self):
        response = self.client.get('/api/products/')
        self.assertEqual(response.status_code, 200)

    def test_order_list_api(self):
        response = self.client.get('/api/orders/')
        self.assertEqual(response.status_code, 200)

    def test_order_summary_api(self):
        response = self.client.get('/api/orders/summary/')
        self.assertEqual(response.status_code, 200)


class OrderServiceTest(TestCase):
    """ทดสอบ Order Service"""

    def setUp(self):
        self.customer = Customer.objects.create(
            line_user_id='U_service_test',
            display_name='เจษฎา',
        )
        self.product = Product.objects.create(
            name='Chateau Margaux', sku='WN001', price=Decimal('12000'), stock=10
        )

    @patch('orders.services.line_service.send_text_reply')
    def test_handle_list_daily_orders_empty(self, mock_reply):
        from orders.services.order_service import handle_list_daily_orders
        
        result = handle_list_daily_orders('U_service_test', reply_token='reply_token_123', message='ดูออเดอร์วันนี้')
        
        self.assertEqual(result['status'], 'success')
        self.assertIn('ยังไม่มีออเดอร์ในระบบ', result['text'])
        mock_reply.assert_called_once()

    @patch('orders.services.line_service.send_text_reply')
    def test_handle_list_daily_orders_with_data(self, mock_reply):
        from orders.services.order_service import handle_list_daily_orders
        
        # Create an order
        order = Order.objects.create(
            customer=self.customer,
            raw_message='ส่ง Margaux 2 ขวด',
            status=Order.Status.CONFIRMED,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name='Chateau Margaux',
            quantity=2,
            unit_price=self.product.price,
        )
        order.calculate_total()

        result = handle_list_daily_orders('U_service_test', reply_token='reply_token_123', message='ดูออเดอร์วันนี้')
        
        self.assertEqual(result['status'], 'success')
        self.assertIn('เจษฎา', result['text'])
        self.assertIn('Chateau Margaux x2 ขวด', result['text'])
        self.assertIn('ยอดขายสุทธิ: ฿24,000', result['text'])
        mock_reply.assert_called_once()

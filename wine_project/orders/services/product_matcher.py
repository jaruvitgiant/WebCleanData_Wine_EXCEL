"""
Product Matcher Service — จับคู่ชื่อสินค้าด้วย Fuzzy Matching

ใช้ rapidfuzz library สำหรับ:
    - รองรับสะกดผิด
    - รองรับเว้นวรรคผิด
    - รองรับภาษาไทย/อังกฤษ
    - Match กับทั้ง name, name_th, aliases

ตัวอย่าง:
    "ลา ตู" → Latour
    "Chateau Margo" → Chateau Margaux
    "มาร์โกซ์" → Chateau Margaux
"""

import logging
from dataclasses import dataclass

from django.conf import settings

from orders.models import Product

logger = logging.getLogger(__name__)

# ค่า threshold สำหรับ fuzzy matching — ยิ่งสูงยิ่งต้อง match แม่นยำ
DEFAULT_THRESHOLD = 65


@dataclass
class MatchResult:
    """ผลลัพธ์จากการ match สินค้า"""
    product: Product | None
    score: float
    matched_name: str

    @property
    def is_matched(self) -> bool:
        return self.product is not None


def _get_threshold() -> int:
    """ดึงค่า threshold จาก settings หรือใช้ค่า default"""
    return getattr(settings, 'FUZZY_MATCH_THRESHOLD', DEFAULT_THRESHOLD)


def _calculate_score(query: str, candidate: str) -> float:
    """
    คำนวณ similarity score ระหว่าง query กับ candidate

    ใช้ 3 วิธีรวมกัน:
    1. token_sort_ratio — จัดเรียง token แล้วเทียบ (ไม่สนลำดับคำ)
    2. partial_ratio — เทียบส่วนที่ match ดีที่สุด (รองรับ substring)
    3. token_set_ratio — เทียบ set ของ token (ไม่สนคำซ้ำ)

    Returns:
        float: ค่าเฉลี่ยถ่วงน้ำหนัก (0-100)
    """
    from rapidfuzz import fuzz

    # ทำให้ lowercase และตัด whitespace ซ้ำ
    q = ' '.join(query.lower().split())
    c = ' '.join(candidate.lower().split())

    if not q or not c:
        return 0.0

    # คำนวณแต่ละ metric
    token_sort = fuzz.token_sort_ratio(q, c)
    partial = fuzz.partial_ratio(q, c)
    token_set = fuzz.token_set_ratio(q, c)

    # ถ่วงน้ำหนัก: partial_ratio สำคัญสุดเพราะรองรับ substring ดี
    weighted_score = (token_sort * 0.3) + (partial * 0.4) + (token_set * 0.3)

    return weighted_score


def match_product(query: str) -> MatchResult:
    """
    จับคู่ชื่อสินค้าจาก query กับสินค้าใน database

    Args:
        query: ชื่อสินค้าที่ลูกค้าพิมพ์ (อาจสะกดผิด/เว้นวรรคผิด)

    Returns:
        MatchResult ที่มี product, score, matched_name

    ตัวอย่าง:
        >>> result = match_product("ลา ตู")
        >>> result.product.name  # "Latour"
        >>> result.score  # 78.5
    """
    threshold = _get_threshold()
    normalized_query = (query or '').strip()

    # ดึงสินค้าที่ active ทั้งหมด
    products = Product.objects.filter(is_active=True)

    # ถ้าผู้ใช้พิมพ์ SKU มาโดยตรง ให้จับคู่แบบ exact ก่อน
    exact_sku_product = products.filter(sku__iexact=normalized_query).first()
    if exact_sku_product:
        return MatchResult(
            product=exact_sku_product,
            score=100.0,
            matched_name=exact_sku_product.sku,
        )

    best_match: MatchResult = MatchResult(
        product=None,
        score=0.0,
        matched_name='',
    )

    for product in products:
        # เทียบกับทุกชื่อ (name, name_th, aliases)
        for candidate_name in product.all_names:
            score = _calculate_score(normalized_query, candidate_name)

            if score > best_match.score:
                best_match = MatchResult(
                    product=product,
                    score=score,
                    matched_name=candidate_name,
                )

    # ตรวจสอบว่า score ผ่าน threshold หรือไม่
    if best_match.score < threshold:
        logger.warning(
            "ไม่พบสินค้าที่ match กับ '%s' (best score: %.1f < threshold: %d)",
            normalized_query, best_match.score, threshold
        )
        return MatchResult(product=None, score=best_match.score, matched_name='')

    logger.info(
        "Match '%s' → '%s' (score: %.1f, matched via: '%s')",
        normalized_query, best_match.product.name, best_match.score, best_match.matched_name
    )
    return best_match


def match_products(product_list: list[dict]) -> list[dict]:
    """
    จับคู่สินค้าหลายรายการพร้อมกัน

    Args:
        product_list: list ของ dict ที่มี key 'name' และ 'quantity'
                      (ได้จาก Gemini service)

    Returns:
        list ของ dict ที่เพิ่ม key 'product' (Product instance หรือ None)
        และ 'match_score'

    ตัวอย่าง Input:
        [{"name": "ลา ตู", "quantity": 2}, {"name": "Margaux", "quantity": 1}]

    ตัวอย่าง Output:
        [
            {"name": "ลา ตู", "quantity": 2, "product": <Product: Latour>, "match_score": 78.5},
            {"name": "Margaux", "quantity": 1, "product": <Product: Chateau Margaux>, "match_score": 92.0},
        ]
    """
    results = []

    for item in product_list:
        name = item.get('name', '')
        quantity = item.get('quantity', 1)

        match_result = match_product(name)

        results.append({
            'name': name,
            'quantity': quantity,
            'product': match_result.product,
            'match_score': match_result.score,
            'matched_name': match_result.matched_name,
        })

    return results

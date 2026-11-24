"""Deterministic intent rules for requests with clear side effects."""

from __future__ import annotations

import re


_REFUND_INFO_MARKERS = (
    "流程", "政策", "规定", "条件", "怎么", "如何", "能否", "能不能",
    "可以吗", "是否", "多久到账", "怎么办", "是什么", "吗",
)
_REFUND_ACTION = re.compile(
    r"(?:"
    r"(?:我要|我想(?:要)?|我需要|帮我.{0,4}|请帮我.{0,4}|给我).{0,8}(?:退款|退钱|退费)"
    r"|(?:退款|退钱|退费).{0,4}(?:申请|办理)"
    r")"
)
_ORDER_ID = re.compile(r"\bORD-[A-Za-z0-9][A-Za-z0-9-]*\b", re.IGNORECASE)


def is_refund_application_request(text: str) -> bool:
    """Return True for a direct refund request, but not a policy/process question."""
    normalized = re.sub(r"\s+", "", (text or "").lower())
    if not normalized or not any(term in normalized for term in ("退款", "退钱", "退费")):
        return False
    if any(marker in normalized for marker in _REFUND_INFO_MARKERS):
        return False
    return bool(_REFUND_ACTION.search(normalized))


def extract_order_id(text: str) -> str | None:
    """Extract a project order ID from customer text."""
    matches = _ORDER_ID.findall(text or "")
    return matches[-1].upper() if matches else None


def is_refund_order_followup(messages: list[object]) -> bool:
    """Recognize an order number sent after the assistant requested refund details."""
    if len(messages) < 2:
        return False
    previous, latest = messages[-2], messages[-1]
    previous_type = getattr(previous, "type", "")
    previous_content = getattr(previous, "content", "")
    latest_content = getattr(latest, "content", "")
    if previous_type != "ai" or "请提供退款订单号" not in str(previous_content):
        return False
    return extract_order_id(str(latest_content)) is not None

"""
أدوات تنسيق العرض: تحويل بيانات قاعدة البيانات إلى نصوص عربية جاهزة للشاشة.

قاعدة: لا منطق أعمال هنا — تنسيق فقط. وأي رقم مالي يُنسَّق من النقاط الصحيحة
عبر `core.money` ولا يُحسب هنا إطلاقاً.
"""

from __future__ import annotations

from html import escape

from core.enums import (
    NotificationType,
    OrderStatus,
    PaymentStatus,
    TicketStatus,
    TransactionType,
)
from core.money import format_units
from utils.datetime_utils import to_local_text

#: أيقونة ووصف عربي لكل حالة طلب
ORDER_STATUS_LABELS: dict[str, str] = {
    OrderStatus.PENDING: "⏳ بانتظار الإرسال",
    OrderStatus.SUBMITTING: "📤 جاري الإرسال",
    OrderStatus.SUBMIT_UNCERTAIN: "❓ قيد المراجعة اليدوية",
    OrderStatus.SUBMITTED: "📨 أُرسل للمزود",
    OrderStatus.IN_PROGRESS: "🔄 قيد التنفيذ",
    OrderStatus.PARTIAL: "⚠️ نُفِّذ جزئياً",
    OrderStatus.COMPLETED: "✅ مكتمل",
    OrderStatus.CANCELED: "🛑 ملغي",
    OrderStatus.FAILED: "❌ فشل",
    OrderStatus.REFUNDED: "↩️ مسترجع",
}

#: وصف عربي لحالات طلب الشحن
PAYMENT_STATUS_LABELS: dict[str, str] = {
    PaymentStatus.PENDING: "⏳ قيد المراجعة",
    PaymentStatus.APPROVED: "✅ معتمد",
    PaymentStatus.REJECTED: "❌ مرفوض",
    PaymentStatus.CANCELLED: "🛑 ملغي",
    PaymentStatus.EXPIRED: "⌛ منتهي",
}

#: وصف عربي لحالات التذكرة
TICKET_STATUS_LABELS: dict[str, str] = {
    TicketStatus.OPEN: "🟢 مفتوحة",
    TicketStatus.PENDING_ADMIN: "⏳ بانتظار الدعم",
    TicketStatus.PENDING_USER: "💬 بانتظار ردك",
    TicketStatus.CLOSED: "🔒 مغلقة",
}

#: وصف عربي لأنواع الحركات المالية
TRANSACTION_LABELS: dict[str, str] = {
    TransactionType.DEPOSIT: "📥 شحن رصيد",
    TransactionType.ORDER: "🛒 دفع طلب",
    TransactionType.REFUND: "↩️ استرجاع",
    TransactionType.PARTIAL_REFUND: "↩️ استرجاع جزئي",
    TransactionType.MANUAL_CREDIT: "➕ إضافة إدارية",
    TransactionType.MANUAL_DEBIT: "➖ خصم إداري",
    TransactionType.WITHDRAW: "📤 سحب",
    TransactionType.ADJUSTMENT: "🔧 تسوية",
}

#: أيقونة لكل نوع إشعار (للعرض في صندوق إشعارات المستخدم)
NOTIFICATION_ICONS: dict[str, str] = {
    NotificationType.ORDER_CREATED: "🛒",
    NotificationType.ORDER_SUBMITTED: "📨",
    NotificationType.ORDER_IN_PROGRESS: "🔄",
    NotificationType.ORDER_COMPLETED: "✅",
    NotificationType.ORDER_PARTIAL: "⚠️",
    NotificationType.ORDER_FAILED: "❌",
    NotificationType.ORDER_CANCELED: "🛑",
    NotificationType.ORDER_REFUNDED: "↩️",
    NotificationType.PAYMENT_RECEIVED: "📥",
    NotificationType.PAYMENT_APPROVED: "✅",
    NotificationType.PAYMENT_REJECTED: "❌",
    NotificationType.BALANCE_CREDITED: "➕",
    NotificationType.BALANCE_DEBITED: "➖",
    NotificationType.TICKET_REPLIED: "💬",
    NotificationType.TICKET_CLOSED: "🔒",
    NotificationType.BROADCAST: "📣",
    NotificationType.SYSTEM: "ℹ️",
}


def order_status_text(status: str) -> str:
    """وصف عربي لحالة الطلب (مع إيموجي)."""
    return ORDER_STATUS_LABELS.get(status, f"❔ {status}")


def payment_status_text(status: str) -> str:
    """وصف عربي لحالة طلب الشحن."""
    return PAYMENT_STATUS_LABELS.get(status, f"❔ {status}")


def ticket_status_text(status: str) -> str:
    """وصف عربي لحالة التذكرة."""
    return TICKET_STATUS_LABELS.get(status, f"❔ {status}")


def transaction_type_text(tx_type: str) -> str:
    """وصف عربي لنوع الحركة المالية."""
    return TRANSACTION_LABELS.get(tx_type, f"❔ {tx_type}")


def notification_icon(notification_type: str) -> str:
    """أيقونة نوع الإشعار."""
    return NOTIFICATION_ICONS.get(notification_type, "🔔")


def money_text(units: int, symbol: str = "", decimals: int = 2) -> str:
    """عرض مبلغ من النقاط الصحيحة بصيغة قابلة للقراءة مع رمز العملة."""
    return format_units(units, symbol, decimals)


def safe(text: str | None, *, limit: int = 0) -> str:
    """
    تهريب نص المستخدم قبل عرضه بتنسيق HTML (حماية من كسر التنسيق أو الحقن).

    المدخلات:
        text  : النص الخام (قد يكون None).
        limit : قص النص عند هذا الطول مع إضافة «…» (صفر = بلا قص).
    """
    value = (text or "").strip()
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return escape(value, quote=False)


def datetime_text(iso_value: str | None, timezone: str = "Asia/Aden") -> str:
    """عرض تاريخ ISO بتوقيت المستخدم المحلي."""
    return to_local_text(iso_value, timezone)


def short_link(link: str, *, limit: int = 45) -> str:
    """اختصار رابط طويل للعرض داخل البطاقات."""
    return safe(link, limit=limit)


def transaction_line(transaction, *, symbol: str = "", timezone: str = "Asia/Aden") -> str:
    """سطر واحد يمثل حركة مالية في سجل العمليات."""
    sign = "+" if transaction.direction == "credit" else "−"
    amount = money_text(transaction.amount_units, symbol)
    label = transaction_type_text(transaction.type)
    when = datetime_text(transaction.created_at, timezone)
    return f"{label}\n   {sign}{amount} • الرصيد بعدها {money_text(transaction.balance_after_units, symbol)}\n   🕒 {when}"


def payment_line(payment, *, symbol: str = "", timezone: str = "Asia/Aden") -> str:
    """سطر واحد يمثل طلب شحن في قائمة طلبات الشحن."""
    status = payment_status_text(payment.status)
    amount = money_text(payment.amount_units, symbol)
    when = datetime_text(payment.created_at, timezone)
    return f"#{payment.id} • {amount} • {status}\n   🕒 {when}"


def notification_line(notification, *, timezone: str = "Asia/Aden") -> str:
    """سطر واحد يمثل إشعاراً في صندوق الإشعارات."""
    icon = notification_icon(notification.type)
    unread = "" if notification.is_read else " 🆕"
    when = datetime_text(notification.created_at, timezone)
    title = safe(notification.title or "", limit=60)
    body = safe(notification.message or "", limit=120)
    return f"{icon} <b>{title}</b>{unread}\n   {body}\n   🕒 {when}"


def order_list_line(order, *, symbol: str = "") -> str:
    """نص زر الطلب في قائمة الطلبات (سطر واحد قصير)."""
    code = order.public_code or f"#{order.id}"
    status = order_status_text(order.status).split(" ", 1)[0]
    return f"{status} {code} • {money_text(order.cost_units, symbol)}"


def ticket_list_line(ticket) -> str:
    """نص زر التذكرة في قائمة التذاكر."""
    status = ticket_status_text(ticket.status).split(" ", 1)[0]
    subject = safe(ticket.subject, limit=30)
    return f"{status} #{ticket.id} • {subject}"

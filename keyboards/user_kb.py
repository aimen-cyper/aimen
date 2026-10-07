"""
لوحات أزرار واجهة العميل — كل شاشة لها دالة واحدة تبني أزرارها.

مبدأ ملزم: **لا زر بلا وظيفة**. كل `callback_data` هنا يقابله معالج مسجَّل في
`handlers/registry.py`، ويتحقق من ذلك اختبار `tests/integration/test_no_dead_buttons.py`.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database.models import (
    Order,
    Payment,
    PaymentMethod,
    Service,
    ServiceCategory,
    SupportTicket,
)
from core.enums import OrderStatus, PaymentStatus
from keyboards.callback_data import (
    NS_ACCOUNT,
    NS_ADMIN,
    NS_CATALOG,
    NS_DEPOSIT,
    NS_FLOW,
    NS_NEW,
    NS_ORDERS,
    NS_SUPPORT,
    NS_WALLET,
    pack,
)
from keyboards.common_kb import back_home_row, home_button, pagination_row
from texts import admin_ar, ar
from utils.formatters import money_text, order_list_line, ticket_list_line

#: تصفية قوائم الطلبات المعروضة للمستخدم (الرمز ⇐ الحالات المشمولة)
ORDER_FILTERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "all": (ar.FILTER_ALL, ()),
    "active": (
        ar.FILTER_ACTIVE,
        (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTING,
            OrderStatus.SUBMIT_UNCERTAIN,
            OrderStatus.SUBMITTED,
            OrderStatus.IN_PROGRESS,
        ),
    ),
    "done": (ar.FILTER_COMPLETED, (OrderStatus.COMPLETED, OrderStatus.PARTIAL)),
    "rfnd": (
        ar.FILTER_REFUNDED,
        (OrderStatus.REFUNDED, OrderStatus.CANCELED, OrderStatus.FAILED),
    ),
}


# ============================ القائمة الرئيسية ========================== #


def main_menu_kb(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    القائمة الرئيسية — ست شاشات في ثلاثة صفوف.

    المدخلات:
        is_admin : يضيف صفاً سابعاً «🛠 لوحة التحكم» لمن يملك `admin.access` فقط.
                   القيمة الافتراضية False حتى لا يظهر الزر بالخطأ لمستخدم عادي
                   في أي مسار لم يتحقق من الصلاحية.
    """
    rows = [
        [
            InlineKeyboardButton(ar.BTN_SERVICES, callback_data=pack(NS_CATALOG, "list", 1)),
            InlineKeyboardButton(ar.BTN_WALLET, callback_data=pack(NS_WALLET, "home")),
        ],
        [
            InlineKeyboardButton(ar.BTN_ORDERS, callback_data=pack(NS_ORDERS, "list", "all", 1)),
            InlineKeyboardButton(ar.BTN_SUPPORT, callback_data=pack(NS_SUPPORT, "home")),
        ],
        [
            InlineKeyboardButton(ar.BTN_ACCOUNT, callback_data=pack(NS_ACCOUNT, "home")),
            InlineKeyboardButton(ar.BTN_INFO, callback_data=pack(NS_ACCOUNT, "info")),
        ],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(admin_ar.BTN_ADMIN_PANEL, callback_data=pack(NS_ADMIN, "home"))]
        )
    return InlineKeyboardMarkup(rows)


# ============================== الخدمات ================================ #


def categories_kb(categories: list[ServiceCategory]) -> InlineKeyboardMarkup:
    """أزرار التصنيفات المتاحة (زرّان في كل صف)."""
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(categories), 2):
        chunk = categories[index : index + 2]
        rows.append(
            [
                InlineKeyboardButton(category.title, callback_data=pack(NS_CATALOG, "open", category.id, 1))
                for category in chunk
            ]
        )
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def services_kb(
    services: list[Service], *, category_id: int, page: int, total_pages: int, symbol: str
) -> InlineKeyboardMarkup:
    """أزرار خدمات تصنيف مع ترقيم صفحات وزر رجوع للتصنيفات."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{service.name} — {money_text(service.price_per_1000_units, symbol)}/1000",
                callback_data=pack(NS_CATALOG, "svc", service.id, category_id, page),
            )
        ]
        for service in services
    ]
    nav = pagination_row(
        NS_CATALOG, "open", page=page, total_pages=total_pages, prefix_args=(category_id,)
    )
    if nav:
        rows.append(nav)
    rows.append(back_home_row(NS_CATALOG, "list", 1))
    return InlineKeyboardMarkup(rows)


def service_card_kb(service: Service, *, category_id: int, page: int) -> InlineKeyboardMarkup:
    """بطاقة الخدمة: زر الطلب + الرجوع لقائمة الخدمات."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_ORDER_NOW, callback_data=pack(NS_NEW, "start", service.id))],
            back_home_row(NS_CATALOG, "open", category_id, page),
        ]
    )


def insufficient_balance_kb(category_id: int, page: int) -> InlineKeyboardMarkup:
    """لوحة تُعرض عند عدم كفاية الرصيد: شحن مباشر أو رجوع."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_DEPOSIT_NOW, callback_data=pack(NS_DEPOSIT, "start"))],
            back_home_row(NS_CATALOG, "open", category_id, page),
        ]
    )


# ============================== طلباتي ================================= #


def orders_filters_row(current: str) -> list[InlineKeyboardButton]:
    """صف أزرار تصفية الطلبات مع تمييز التصفية الحالية."""
    row: list[InlineKeyboardButton] = []
    for code, (label, _statuses) in ORDER_FILTERS.items():
        mark = "• " if code == current else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=pack(NS_ORDERS, "list", code, 1)))
    return row


def orders_kb(
    orders: list[Order], *, filter_code: str, page: int, total_pages: int, symbol: str
) -> InlineKeyboardMarkup:
    """قائمة طلبات المستخدم مع التصفية والترقيم."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                order_list_line(order, symbol=symbol),
                callback_data=pack(NS_ORDERS, "view", order.id, filter_code, page),
            )
        ]
        for order in orders
    ]
    rows.append(orders_filters_row(filter_code))
    nav = pagination_row(
        NS_ORDERS, "list", page=page, total_pages=total_pages, prefix_args=(filter_code,)
    )
    if nav:
        rows.append(nav)
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def order_card_kb(
    order: Order, *, filter_code: str, page: int, can_cancel: bool
) -> InlineKeyboardMarkup:
    """
    بطاقة الطلب.

    زر الإلغاء يظهر فقط عندما يسمح به منطق الخدمة فعلاً — لا زر معطّل ولا زر كاذب.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if can_cancel:
        rows.append(
            [InlineKeyboardButton(ar.BTN_ORDER_CANCEL, callback_data=pack(NS_ORDERS, "cancel", order.id))]
        )
    rows.append(
        [InlineKeyboardButton(ar.BTN_ORDER_TICKET, callback_data=pack(NS_SUPPORT, "neworder", order.id))]
    )
    rows.append(back_home_row(NS_ORDERS, "list", filter_code, page))
    return InlineKeyboardMarkup(rows)


def order_cancel_confirm_kb(order_id: int) -> InlineKeyboardMarkup:
    """تأكيد إلغاء الطلب (خطوة إضافية قبل أي حركة مالية)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(ar.BTN_CONFIRM, callback_data=pack(NS_ORDERS, "cancelyes", order_id)),
                InlineKeyboardButton(ar.BTN_BACK, callback_data=pack(NS_ORDERS, "view", order_id, "all", 1)),
            ]
        ]
    )


# ============================== المحفظة ================================ #


def wallet_kb() -> InlineKeyboardMarkup:
    """شاشة المحفظة: شحن، سجل العمليات، طلبات الشحن."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_DEPOSIT_NOW, callback_data=pack(NS_DEPOSIT, "start"))],
            [
                InlineKeyboardButton(ar.BTN_WALLET_HISTORY, callback_data=pack(NS_WALLET, "history", 1)),
                InlineKeyboardButton(ar.BTN_MY_DEPOSITS, callback_data=pack(NS_DEPOSIT, "list", 1)),
            ],
            [home_button()],
        ]
    )


def history_kb(*, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """سجل العمليات المالية مع الترقيم."""
    rows: list[list[InlineKeyboardButton]] = []
    nav = pagination_row(NS_WALLET, "history", page=page, total_pages=total_pages)
    if nav:
        rows.append(nav)
    rows.append(back_home_row(NS_WALLET, "home"))
    return InlineKeyboardMarkup(rows)


# =============================== الشحن ================================= #


def deposit_methods_kb(methods: list[PaymentMethod]) -> InlineKeyboardMarkup:
    """أزرار طرق الشحن المفعّلة."""
    rows = [
        [InlineKeyboardButton(method.name_ar, callback_data=pack(NS_DEPOSIT, "method", method.id))]
        for method in methods
    ]
    rows.append(back_home_row(NS_WALLET, "home"))
    return InlineKeyboardMarkup(rows)


def deposits_kb(payments: list[Payment], *, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """قائمة طلبات الشحن السابقة مع إمكانية إلغاء المعلّق منها."""
    rows: list[list[InlineKeyboardButton]] = []
    for payment in payments:
        if payment.status == PaymentStatus.PENDING:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🛑 إلغاء طلب #{payment.id}",
                        callback_data=pack(NS_DEPOSIT, "cancel", payment.id),
                    )
                ]
            )
    nav = pagination_row(NS_DEPOSIT, "list", page=page, total_pages=total_pages)
    if nav:
        rows.append(nav)
    rows.append(back_home_row(NS_WALLET, "home"))
    return InlineKeyboardMarkup(rows)


# =============================== الدعم ================================= #


def support_kb() -> InlineKeyboardMarkup:
    """شاشة الدعم الرئيسية."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_TICKET_NEW, callback_data=pack(NS_SUPPORT, "new"))],
            [InlineKeyboardButton(ar.BTN_TICKETS_LIST, callback_data=pack(NS_SUPPORT, "list", 1))],
            [home_button()],
        ]
    )


def ticket_categories_kb() -> InlineKeyboardMarkup:
    """أزرار تصنيفات التذكرة الجديدة."""
    rows = [
        [InlineKeyboardButton(label, callback_data=pack(NS_SUPPORT, "cat", code))]
        for code, label in ar.TICKET_CATEGORIES.items()
    ]
    rows.append([InlineKeyboardButton(ar.BTN_CANCEL, callback_data=pack(NS_FLOW, "cancel"))])
    return InlineKeyboardMarkup(rows)


def tickets_kb(tickets: list[SupportTicket], *, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """قائمة تذاكر المستخدم."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(ticket_list_line(ticket), callback_data=pack(NS_SUPPORT, "view", ticket.id))]
        for ticket in tickets
    ]
    nav = pagination_row(NS_SUPPORT, "list", page=page, total_pages=total_pages)
    if nav:
        rows.append(nav)
    rows.append(back_home_row(NS_SUPPORT, "home"))
    return InlineKeyboardMarkup(rows)


def ticket_card_kb(ticket: SupportTicket) -> InlineKeyboardMarkup:
    """بطاقة التذكرة: الرد والإغلاق (تظهر فقط ما دامت مفتوحة)."""
    rows: list[list[InlineKeyboardButton]] = []
    if ticket.is_open:
        rows.append(
            [
                InlineKeyboardButton(ar.BTN_TICKET_REPLY, callback_data=pack(NS_SUPPORT, "reply", ticket.id)),
                InlineKeyboardButton(ar.BTN_TICKET_CLOSE, callback_data=pack(NS_SUPPORT, "close", ticket.id)),
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(ar.BTN_TICKET_REPLY, callback_data=pack(NS_SUPPORT, "reply", ticket.id))]
        )
    rows.append(back_home_row(NS_SUPPORT, "list", 1))
    return InlineKeyboardMarkup(rows)


# ============================== الحساب ================================= #


def account_kb(unread: int) -> InlineKeyboardMarkup:
    """شاشة الحساب مع عدّاد الإشعارات غير المقروءة."""
    badge = f" ({unread})" if unread else ""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_NOTIFICATIONS + badge, callback_data=pack(NS_ACCOUNT, "notif", 1))],
            [
                InlineKeyboardButton(ar.BTN_WALLET, callback_data=pack(NS_WALLET, "home")),
                InlineKeyboardButton(ar.BTN_ORDERS, callback_data=pack(NS_ORDERS, "list", "all", 1)),
            ],
            [home_button()],
        ]
    )


def notifications_kb(*, page: int, total_pages: int, unread: int) -> InlineKeyboardMarkup:
    """صندوق الإشعارات مع زر تعليم الكل كمقروء."""
    rows: list[list[InlineKeyboardButton]] = []
    if unread:
        rows.append(
            [InlineKeyboardButton(ar.BTN_MARK_ALL_READ, callback_data=pack(NS_ACCOUNT, "readall"))]
        )
    nav = pagination_row(NS_ACCOUNT, "notif", page=page, total_pages=total_pages)
    if nav:
        rows.append(nav)
    rows.append(back_home_row(NS_ACCOUNT, "home"))
    return InlineKeyboardMarkup(rows)


def info_kb() -> InlineKeyboardMarkup:
    """شاشة المعلومات."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ar.BTN_SUPPORT, callback_data=pack(NS_SUPPORT, "home"))],
            [home_button()],
        ]
    )


def notifications_only_back_kb() -> InlineKeyboardMarkup:
    """لوحة رجوع بسيطة تُستخدم بعد الإجراءات السريعة."""
    return InlineKeyboardMarkup([back_home_row(NS_ACCOUNT, "home")])

"""
شاشات تصفح الخدمات: التصنيفات ← خدمات التصنيف (بصفحات) ← بطاقة الخدمة.

كل البيانات تأتي من `CatalogService`، ولا تُعرض خدمة غير متاحة للطلب
(معطّلة أو مؤرشفة أو مزودها متوقف) — القائمة نفسها مفلترة من طبقة الخدمات.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from handlers.common.guards import guarded
from handlers.common.ui import answer_callback, render
from keyboards.callback_data import unpack
from keyboards.user_kb import categories_kb, service_card_kb, services_kb
from middlewares.context import require_user, services_of
from texts import ar
from utils.formatters import money_text, safe

#: عدد الخدمات المعروضة في كل صفحة
PAGE_SIZE = 6


@guarded
async def categories_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض التصنيفات التي تحتوي خدمات متاحة فعلاً."""
    await answer_callback(update)
    services = services_of(context)
    categories = await services.catalog.categories()
    if not categories:
        await render(update, context, ar.CATEGORIES_EMPTY, categories_kb([]))
        return
    await render(update, context, ar.CATEGORIES_TITLE, categories_kb(categories))


@guarded
async def services_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض صفحة من خدمات تصنيف معيّن."""
    await answer_callback(update)
    data = unpack(update.callback_query.data if update.callback_query else None)
    category_id = data.int_arg(0)
    page_number = max(1, data.int_arg(1, 1))

    services = services_of(context)
    symbol = await services.settings.money_symbol()
    page = await services.catalog.services_page(category_id, page=page_number, page_size=PAGE_SIZE)

    categories = {category.id: category for category in await services.catalog.categories()}
    category = categories.get(category_id)
    if category is None or not page.items:
        await render(
            update,
            context,
            ar.SERVICES_EMPTY,
            services_kb([], category_id=category_id, page=1, total_pages=1, symbol=symbol),
        )
        return

    text = ar.SERVICES_TITLE.format(
        emoji=category.emoji,
        category=safe(category.name_ar),
        page=page.page,
        pages=page.total_pages,
    )
    await render(
        update,
        context,
        text,
        services_kb(
            page.items,
            category_id=category_id,
            page=page.page,
            total_pages=page.total_pages,
            symbol=symbol,
        ),
    )


@guarded
async def service_card_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بطاقة خدمة واحدة: السعر والحدود ومدة التنفيذ ورصيد المستخدم."""
    await answer_callback(update)
    data = unpack(update.callback_query.data if update.callback_query else None)
    service_id = data.int_arg(0)
    category_id = data.int_arg(1)
    page = max(1, data.int_arg(2, 1))

    services = services_of(context)
    user = require_user(context)
    symbol = await services.settings.money_symbol()
    service = await services.catalog.get_available_service(service_id)

    description = f"📝 {safe(service.description, limit=300)}\n" if service.description else ""
    text = ar.SERVICE_CARD.format(
        name=safe(service.name),
        price=money_text(service.price_per_1000_units, symbol),
        min_quantity=service.min_quantity,
        max_quantity=service.max_quantity,
        avg_time=safe(service.avg_time_text) or ar.AVG_TIME_UNKNOWN,
        description=description,
        balance=money_text(user.balance_units, symbol),
    )
    await render(update, context, text, service_card_kb(service, category_id=category_id, page=page))

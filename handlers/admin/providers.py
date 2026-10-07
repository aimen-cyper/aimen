"""
قسم «🔌 المزودون» في لوحة التحكم: إدارة المزودين وربط الـAPI.

ما يُنفَّذ هنا:
    • قائمة المزودين وبطاقة كل مزود (الحالة، الرصيد، عدد خدماتنا منه، آخر خطأ).
    • إضافة مزود جديد: اسم ← رابط ← نوع ← مفتاح ← تأكيد، ثم اختبار اتصال فوري.
    • تغيير مفتاح API، تفعيل/إيقاف، اختبار الاتصال، تحديث الرصيد.
    • مزامنة أسعار وحدود خدماتنا مع قائمة المزود الحالية.
    • استيراد خدمة من قائمة المزود بهامش ربح محدد.

قواعد أمنية ملزمة في هذا الملف:
    • **لا يُعرض مفتاح API كاملاً أبداً** — فقط آخر أربعة محارف (`api_key_hint`)،
      ولا يُسجَّل المفتاح في اللوق ولا في سجل التدقيق.
    • رسالة المفتاح التي يرسلها الأدمن تُحذف من المحادثة فور قراءتها قدر الإمكان.
    • كل إجراء يفحص صلاحيته على الـcallback نفسه عبر `admin_required`.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from core.exceptions import ProviderError, ValidationError
from core.permissions import Permission
from database.models import Provider
from handlers.common.conversation import conversation
from handlers.admin.common import (
    ADMIN_FLOW_KEY,
    PAGE_SIZE,
    admin_required,
    args,
    cancel_admin_flow,
    clear_flow,
    flow,
    paginate,
    start_flow,
    symbol_of,
    update_flow,
)
from handlers.common.fallbacks import exit_conversation
from handlers.common.guards import guarded
from handlers.common.ui import answer_callback, render, send
from integrations.providers.base import SUPPORTED_SERVICE_TYPES, ProviderServiceItem
from integrations.providers.registry import available_types, is_supported
from keyboards.admin_kb import (
    back_panel_kb,
    cancel_only_kb,
    category_picker_kb,
    confirm_cancel_kb,
    provider_card_kb,
    provider_import_results_kb,
    provider_services_kb,
    provider_type_kb,
    providers_home_kb,
)
from keyboards.callback_data import NS_ADM_PRV, NS_FLOW, pattern
from middlewares.context import require_user, services_of
from texts import admin_ar as t
from utils.formatters import datetime_text, money_text, safe
from utils.logging_setup import get_logger
from utils.net_security import validate_public_https_url
from utils.validators import clean_text, normalize_digits

logger = get_logger(__name__)

#: حالات محادثة «إضافة مزود»
ASK_NAME, ASK_URL, ASK_TYPE, ASK_CURRENCY, ASK_KEY, CONFIRM_NEW = range(6)

#: حالة محادثة «تغيير المفتاح»
ASK_NEW_KEY = 10

#: حالة محادثة «العملة وسعر الصرف»
ASK_RATE_EDIT = 11

#: حالات محادثة «استيراد خدمة من المزود»
ASK_SEARCH, PICK_ITEM, PICK_CATEGORY, ASK_MARGIN, CONFIRM_IMPORT = range(20, 25)

#: أقصى عدد نتائج بحث تُعرض كأزرار (الزيادة تُربك لا تفيد)
IMPORT_RESULTS_LIMIT = 8

#: مفتاح تخزين نتائج بحث المزود مؤقتاً داخل بيانات المحادثة
IMPORT_ITEMS_KEY = "import_items"


# ======================================================================== #
#                              أدوات داخلية                                #
# ======================================================================== #


async def _load_provider(context: ContextTypes.DEFAULT_TYPE, provider_id: int) -> Provider:
    """
    تحميل مزود بالمعرف أو رفع خطأ عربي واضح.

    الاستثناءات: ValidationError إذا كان الزر يحمل معرفاً لم يعد موجوداً.
    """
    services = services_of(context)
    provider = await services.providers.get(provider_id)
    if provider is None:
        raise ValidationError(
            f"مزود غير موجود: {provider_id}", user_message=t.PROVIDER_NOT_FOUND
        )
    return provider


async def _provider_card_text(context: ContextTypes.DEFAULT_TYPE, provider: Provider) -> str:
    """بناء نص بطاقة المزود (بلا أي جزء من المفتاح عدا آخر أربعة محارف)."""
    services = services_of(context)
    health = await services.providers.health(provider)
    services_count = await services.repos.providers.count_services(provider.id)

    if not provider.is_active:
        state = t.PROVIDER_STATE_OFF
    elif not health.is_usable:
        state = t.PROVIDER_STATE_BLOCKED.format(reason=safe(health.reason, limit=60))
    else:
        state = t.PROVIDER_STATE_OK

    last_error_line = ""
    if provider.last_error:
        last_error_line = t.PROVIDER_LAST_ERROR_LINE.format(
            error=safe(provider.last_error, limit=120)
        )

    rate, _problem = await services.providers.rate_state(provider)
    system_code, _symbol, _decimals = await services.settings.currency()
    equivalent = ""
    if rate is None:
        rate_text = t.PROVIDER_RATE_NONE
    elif provider.exchange_rate:
        rate_text = f"1 {safe(provider.currency, limit=6)} = {safe(provider.exchange_rate, limit=24)} {safe(system_code, limit=6)}"
        equivalent = (
            f" (≈ {money_text(services.providers.to_system_units(provider.balance_units, rate))} "
            f"{safe(system_code, limit=6)})"
        )
    else:
        rate_text = "نفس عملة النظام"
    return t.PROVIDER_CARD.format(
        name=safe(provider.name, limit=40),
        provider_id=provider.id,
        api_url=safe(provider.api_url, limit=70),
        key_hint=safe(provider.api_key_hint or "????", limit=8),
        api_type=safe(provider.api_type, limit=20),
        state=state,
        balance=money_text(provider.balance_units),
        currency=safe(provider.currency or "USD", limit=6),
        balance_equivalent=equivalent,
        rate_text=rate_text,
        balance_checked=datetime_text(provider.balance_checked_at) if provider.balance_checked_at else "—",
        services_count=services_count,
        failures=provider.consecutive_failures,
        last_success=datetime_text(provider.last_success_at) if provider.last_success_at else "—",
        last_error_line=last_error_line,
    )


async def _render_provider(
    update: Update, context: ContextTypes.DEFAULT_TYPE, provider_id: int, *, as_new: bool = False
) -> None:
    """عرض بطاقة المزود (تحرير الرسالة الحالية أو إرسال رسالة جديدة بعد عملية)."""
    from handlers.admin.common import permissions_of

    provider = await _load_provider(context, provider_id)
    text = await _provider_card_text(context, provider)
    perms = await permissions_of(context)
    markup = provider_card_kb(provider, perms)
    if as_new:
        await send(update, context, text, markup)
    else:
        await render(update, context, text, markup)


# ======================================================================== #
#                            الشاشات (قراءة)                               #
# ======================================================================== #


@guarded
@admin_required(Permission.PROVIDERS_VIEW)
async def providers_home_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """قائمة المزودين مع عدّادات سريعة."""
    await answer_callback(update)
    from handlers.admin.common import permissions_of

    services = services_of(context)
    providers = await services.providers.list_all()
    perms = await permissions_of(context)

    if not providers:
        await render(update, context, t.PROVIDERS_EMPTY, providers_home_kb([], perms))
        return

    linked = 0
    for provider in providers:
        linked += await services.repos.providers.count_services(provider.id)

    text = t.PROVIDERS_HOME.format(
        total=len(providers),
        active=sum(1 for provider in providers if provider.is_active),
        linked_services=linked,
    )
    await render(update, context, text, providers_home_kb(providers, perms))


@guarded
@admin_required(Permission.PROVIDERS_VIEW)
async def provider_card_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بطاقة مزود واحد."""
    await answer_callback(update)
    await _render_provider(update, context, args(update).int_arg(0))


@guarded
@admin_required(Permission.SERVICES_VIEW)
async def provider_services_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """قائمة خدماتنا المرتبطة بهذا المزود (مُرقَّمة)."""
    await answer_callback(update)
    data = args(update)
    provider_id = data.int_arg(0)
    provider = await _load_provider(context, provider_id)

    services = services_of(context)
    admin = require_user(context)
    items = await services.catalog.services_of_provider(admin, provider_id)
    page, total_pages, offset = paginate(len(items), data.int_arg(1, 1))
    symbol = await symbol_of(context)

    await render(
        update,
        context,
        t.PROVIDER_SERVICES_TITLE.format(
            name=safe(provider.name, limit=40), total=len(items), page=page, pages=total_pages
        ),
        provider_services_kb(
            items[offset : offset + PAGE_SIZE],
            provider_id=provider_id,
            page=page,
            total_pages=total_pages,
            symbol=symbol,
        ),
    )


# ======================================================================== #
#                      إجراءات مباشرة (اختبار/رصيد/تفعيل)                  #
# ======================================================================== #


@guarded
@admin_required(Permission.PROVIDERS_TEST)
async def test_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """اختبار اتصال فوري بالمزود وعرض النتيجة (بلا أي سر في الرسالة)."""
    await answer_callback(update, t.PROVIDER_TEST_RUNNING)
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)

    services = services_of(context)
    admin = require_user(context)
    ok, message = await services.providers.test_connection(admin, provider_id)

    await render(
        update,
        context,
        t.PROVIDER_TEST_RESULT.format(
            icon="✅" if ok else "❌",
            name=safe(provider.name, limit=40),
            message=safe(message, limit=200),
        ),
        back_panel_kb(),
    )
    await _render_provider(update, context, provider_id, as_new=True)


@guarded
@admin_required(Permission.PROVIDERS_TEST)
async def refresh_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تحديث رصيدنا لدى المزود وحفظ لقطة تاريخية (داخل الخدمة)."""
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)
    services = services_of(context)

    try:
        balance = await services.providers.check_balance(provider)
    except ProviderError as exc:
        await answer_callback(update, exc.user_message, alert=True)
        await _render_provider(update, context, provider_id)
        return

    await answer_callback(
        update,
        t.PROVIDER_BALANCE_UPDATED.format(
            name=provider.name,
            balance=money_text(balance.amount_units),
            currency=provider.currency or balance.currency,
        ).replace("<b>", "").replace("</b>", ""),
        alert=True,
    )
    await _render_provider(update, context, provider_id)


@guarded
@admin_required(Permission.PROVIDERS_UPDATE)
async def toggle_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    تفعيل أو إيقاف مزود.

    الإيقاف لا يلغي الطلبات الجارية — يمنع فقط إرسال طلبات جديدة إليه،
    والمتابعة تستمر حتى تصل الطلبات القائمة إلى حالتها النهائية.
    """
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)
    services = services_of(context)
    admin = require_user(context)

    new_state = not bool(provider.is_active)
    changed = await services.providers.set_active(admin, provider_id, new_state)
    message = (
        (t.PROVIDER_TOGGLED_ON if new_state else t.PROVIDER_TOGGLED_OFF).format(
            name=provider.name
        )
        if changed
        else t.NOTHING_CHANGED
    )
    await answer_callback(update, message, alert=True)
    await _render_provider(update, context, provider_id)


# ======================================================================== #
#                          محادثة: إضافة مزود جديد                         #
# ======================================================================== #


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def start_new_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء إضافة مزود: سؤال الاسم."""
    await answer_callback(update)
    start_flow(context)
    await render(update, context, t.PROVIDER_ASK_NAME, cancel_only_kb())
    return ASK_NAME


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def receive_provider_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال اسم المزود ثم سؤال رابط الـAPI."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_NAME

    name = clean_text(message.text, field="اسم المزود", max_length=60, min_length=2)
    update_flow(context, name=name)
    await send(update, context, t.PROVIDER_ASK_URL, cancel_only_kb())
    return ASK_URL


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def receive_provider_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال رابط الـAPI (يُتحقق منه) ثم سؤال نوع الواجهة."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_URL

    api_url = validate_public_https_url(message.text.strip())
    update_flow(context, api_url=api_url)
    await send(update, context, t.PROVIDER_ASK_TYPE, provider_type_kb())
    return ASK_TYPE


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def receive_provider_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال نوع الواجهة المختار ثم طلب المفتاح."""
    await answer_callback(update)
    api_type = args(update).arg(0)
    if not is_supported(api_type):
        await answer_callback(
            update,
            t.PROVIDER_TYPE_UNSUPPORTED.format(types=", ".join(available_types())),
            alert=True,
        )
        return ASK_TYPE

    update_flow(context, api_type=api_type)
    system_code, _symbol, _decimals = await services_of(context).settings.currency()
    await render(update, context, t.PROVIDER_ASK_CURRENCY.format(system=safe(system_code, limit=6)), cancel_only_kb())
    return ASK_CURRENCY


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def receive_provider_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال عملة المزود وسعر صرفها (يُتحقق من اتساقهما) ثم طلب المفتاح."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_CURRENCY

    services = services_of(context)
    code, rate = services.providers.parse_currency_spec(message.text)
    code, rate = await services.providers.validate_currency(code, rate)   # يرفض عملة مختلفة بلا سعر
    update_flow(context, currency=code, exchange_rate=rate)
    await send(update, context, t.PROVIDER_ASK_KEY, cancel_only_kb())
    return ASK_KEY


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def receive_provider_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    استقبال مفتاح API وعرض شاشة التأكيد.

    المفتاح يبقى في ذاكرة المحادثة فقط حتى لحظة التشفير والحفظ، ونحاول حذف
    رسالة الأدمن التي تحمله من المحادثة فوراً.
    """
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_KEY

    api_key = message.text.strip()
    if len(api_key) < 8:
        await send(update, context, t.PROVIDER_KEY_TOO_SHORT, cancel_only_kb())
        return ASK_KEY

    data = update_flow(context, api_key=api_key)
    await _try_delete(message)

    await send(
        update,
        context,
        t.PROVIDER_NEW_CONFIRM.format(
            name=safe(str(data["name"]), limit=40),
            api_url=safe(str(data["api_url"]), limit=70),
            api_type=safe(str(data["api_type"]), limit=20),
            currency_text=safe(
                f"{data['currency']} × {data['exchange_rate']}" if data.get("exchange_rate") else str(data["currency"]),
                limit=40,
            ),
            key_hint=api_key[-4:],
        ),
        confirm_cancel_kb(),
    )
    return CONFIRM_NEW


async def _try_delete(message) -> None:
    """
    محاولة حذف رسالة تحمل سراً (مفتاح API).

    الحذف قد يفشل لأسباب مشروعة (مرّ وقت طويل، أو البوت بلا صلاحية الحذف في
    هذه المحادثة) — نسجّل ذلك ولا نُسقط العملية، فالمفتاح محفوظ مشفّراً أصلاً.
    """
    try:
        await message.delete()
    except Exception as exc:                                   # noqa: BLE001
        logger.info("تعذّر حذف رسالة المفتاح من المحادثة: %s", type(exc).__name__)


@guarded
@admin_required(Permission.PROVIDERS_CREATE)
async def confirm_new_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إنشاء المزود فعلياً ثم اختبار اتصاله مباشرة."""
    await answer_callback(update)
    data = flow(context)
    if not data.get("name") or not data.get("api_url") or not data.get("api_key"):
        await render(update, context, t.ERR_SESSION_LOST)
        return ConversationHandler.END

    services = services_of(context)
    admin = require_user(context)
    provider = await services.providers.create_provider(
        admin,
        name=str(data["name"]),
        api_url=str(data["api_url"]),
        api_key=str(data["api_key"]),
        api_type=str(data.get("api_type") or "perfect_panel"),
        currency=str(data["currency"]) if data.get("currency") else None,
        exchange_rate=str(data["exchange_rate"]) if data.get("exchange_rate") else None,
    )
    clear_flow(context)                                   # يمسح المفتاح من الذاكرة فوراً
    logger.info("أضاف الأدمن %s المزود %s", admin.telegram_id, provider.id)

    await render(
        update,
        context,
        t.PROVIDER_CREATED.format(name=safe(provider.name, limit=40), provider_id=provider.id),
    )

    ok, message = await services.providers.test_connection(admin, provider.id)
    await send(
        update,
        context,
        t.PROVIDER_TEST_RESULT.format(
            icon="✅" if ok else "❌",
            name=safe(provider.name, limit=40),
            message=safe(message, limit=200),
        ),
    )
    await _render_provider(update, context, provider.id, as_new=True)
    return ConversationHandler.END


# ======================================================================== #
#                        محادثة: تغيير مفتاح الـAPI                        #
# ======================================================================== #


@guarded
@admin_required(Permission.PROVIDERS_KEY_UPDATE)
async def start_key_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء تغيير مفتاح مزود."""
    await answer_callback(update)
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)
    start_flow(context, provider_id=provider_id)

    await render(
        update,
        context,
        t.PROVIDER_ASK_NEW_KEY.format(
            name=safe(provider.name, limit=40), key_hint=safe(provider.api_key_hint or "????")
        ),
        cancel_only_kb(),
    )
    return ASK_NEW_KEY


@guarded
@admin_required(Permission.PROVIDERS_KEY_UPDATE)
async def receive_new_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """حفظ المفتاح الجديد مشفّراً ثم اختبار الاتصال به."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_NEW_KEY

    data = flow(context)
    if not data.get("provider_id"):
        await send(update, context, t.ERR_SESSION_LOST)
        return ConversationHandler.END

    api_key = message.text.strip()
    if len(api_key) < 8:
        await send(update, context, t.PROVIDER_KEY_TOO_SHORT, cancel_only_kb())
        return ASK_NEW_KEY

    await _try_delete(message)
    services = services_of(context)
    admin = require_user(context)
    provider_id = int(data["provider_id"])
    changed = await services.providers.update_api_key(admin, provider_id, api_key)
    clear_flow(context)

    provider = await _load_provider(context, provider_id)
    await send(
        update,
        context,
        t.PROVIDER_KEY_UPDATED.format(
            name=safe(provider.name, limit=40), key_hint=safe(provider.api_key_hint or "????")
        )
        if changed
        else t.PROVIDER_KEY_UNCHANGED,
    )

    ok, result = await services.providers.test_connection(admin, provider_id)
    await send(
        update,
        context,
        t.PROVIDER_TEST_RESULT.format(
            icon="✅" if ok else "❌",
            name=safe(provider.name, limit=40),
            message=safe(result, limit=200),
        ),
    )
    await _render_provider(update, context, provider_id, as_new=True)
    return ConversationHandler.END


# ======================================================================== #
#                   محادثة: العملة وسعر الصرف لمزود قائم                    #
# ======================================================================== #


async def _currency_summary(context: ContextTypes.DEFAULT_TYPE, provider: Provider) -> str:
    """نص مختصر للعملة الحالية: «USD × 530» أو «YER»."""
    return f"{provider.currency} × {provider.exchange_rate}" if provider.exchange_rate else str(provider.currency)


@guarded
@admin_required(Permission.PROVIDERS_UPDATE)
async def start_rate_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء تعديل عملة المزود وسعر صرفه."""
    await answer_callback(update)
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)
    start_flow(context, provider_id=provider_id)
    system_code, _symbol, _decimals = await services_of(context).settings.currency()
    await render(
        update,
        context,
        t.PROVIDER_ASK_RATE_EDIT.format(
            name=safe(provider.name, limit=40),
            current=safe(await _currency_summary(context, provider), limit=40),
            system=safe(system_code, limit=6),
        ),
        cancel_only_kb(),
    )
    return ASK_RATE_EDIT


@guarded
@admin_required(Permission.PROVIDERS_UPDATE)
async def receive_rate_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """حفظ العملة وسعر الصرف الجديدين (مع تدقيق) ثم عرض بطاقة المزود."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_RATE_EDIT

    data = flow(context)
    if not data.get("provider_id"):
        await send(update, context, t.ERR_SESSION_LOST)
        return ConversationHandler.END

    services = services_of(context)
    admin = require_user(context)
    provider_id = int(data["provider_id"])
    code, rate = services.providers.parse_currency_spec(message.text)
    provider = await services.providers.set_currency(admin, provider_id, currency=code, exchange_rate=rate)
    clear_flow(context)
    await send(
        update,
        context,
        t.PROVIDER_RATE_UPDATED.format(
            name=safe(provider.name, limit=40), current=safe(await _currency_summary(context, provider), limit=40)
        ),
    )
    await _render_provider(update, context, provider_id, as_new=True)
    return ConversationHandler.END


# ======================================================================== #
#                          مزامنة خدمات المزود                             #
# ======================================================================== #


@guarded
@admin_required(Permission.SERVICES_SYNC)
async def sync_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    جلب قائمة خدمات المزود وتحديث تكلفة وحدود خدماتنا المرتبطة به.

    لا يُغيَّر سعر البيع ولا تُحذف أي خدمة — النتيجة تقرير يقرر الأدمن بناءً عليه.
    """
    await answer_callback(update, t.PROVIDER_SYNC_RUNNING)
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)

    services = services_of(context)
    admin = require_user(context)
    symbol = await symbol_of(context)

    try:
        items = await services.providers.fetch_services(admin, provider)
    except ProviderError as exc:
        await render(
            update,
            context,
            t.PROVIDER_TEST_RESULT.format(
                icon="❌", name=safe(provider.name, limit=40), message=safe(exc.user_message)
            ),
            back_panel_kb(),
        )
        await _render_provider(update, context, provider_id, as_new=True)
        return

    report = await services.catalog.sync_provider_services(
        admin, provider_id=provider_id, items=items
    )

    missing_lines = "".join(
        t.PROVIDER_SYNC_MISSING_LINE.format(
            name=safe(service.name, limit=30), service_id=service.id
        )
        for service in report.missing[:5]
    )
    negative_lines = ""
    if report.negative_margin:
        negative_lines = t.PROVIDER_SYNC_NEGATIVE_HEADER + "".join(
            t.PROVIDER_SYNC_NEGATIVE_LINE.format(
                name=safe(service.name, limit=30),
                price=money_text(service.price_per_1000_units, symbol),
                cost=money_text(service.provider_price_per_1000_units or 0, symbol),
            )
            for service in report.negative_margin[:5]
        )

    unsupported_lines = ""
    if report.unsupported:
        unsupported_lines = t.PROVIDER_SYNC_UNSUPPORTED_HEADER + "".join(
            t.PROVIDER_SYNC_MISSING_LINE.format(name=safe(service.name, limit=30), service_id=service.id)
            for service in report.unsupported[:5]
        )

    await render(
        update,
        context,
        t.PROVIDER_SYNC_RESULT.format(
            name=safe(provider.name, limit=40),
            provider_items=report.provider_items,
            checked=report.checked,
            updated=report.updated,
            unchanged=report.unchanged,
            missing=len(report.missing),
            missing_lines=missing_lines,
            negative_lines=negative_lines + unsupported_lines,
        ),
        back_panel_kb(),
    )
    await _render_provider(update, context, provider_id, as_new=True)


# ======================================================================== #
#                   محادثة: استيراد خدمة من قائمة المزود                   #
# ======================================================================== #


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def start_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بدء استيراد خدمة: سؤال كلمة البحث داخل قائمة المزود."""
    await answer_callback(update)
    provider_id = args(update).int_arg(0)
    provider = await _load_provider(context, provider_id)
    start_flow(context, provider_id=provider_id)

    await render(
        update,
        context,
        t.PROVIDER_IMPORT_ASK_SEARCH.format(name=safe(provider.name, limit=40)),
        cancel_only_kb(),
    )
    return ASK_SEARCH


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def receive_import_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    جلب قائمة خدمات المزود وتصفيتها بكلمة البحث.

    النتائج تُحفظ في بيانات المحادثة ليُشار إليها بالفهرس في الأزرار (أرقام
    بعض المزودين أطول من أن تُوضع داخل `callback_data`).
    """
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_SEARCH

    data = flow(context)
    if not data.get("provider_id"):
        await send(update, context, t.ERR_SESSION_LOST)
        return ConversationHandler.END

    query = clean_text(message.text, field="كلمة البحث", max_length=60, min_length=1)
    provider = await _load_provider(context, int(data["provider_id"]))
    services = services_of(context)
    admin = require_user(context)
    symbol = await symbol_of(context)

    try:
        items = await services.providers.fetch_services(admin, provider)
    except ProviderError as exc:
        clear_flow(context)
        await send(update, context, f"❌ {safe(exc.user_message)}", back_panel_kb())
        return ConversationHandler.END

    # الأنواع غير المدعومة (Package/Custom Comments/…) لا تُعرض للاستيراد أصلاً، ويُذكر عددها
    importable = [item for item in items if item.service_type in SUPPORTED_SERVICE_TYPES]
    hidden = len(items) - len(importable)
    matched = _filter_items(importable, query)
    if not matched:
        await send(
            update,
            context,
            t.PROVIDER_IMPORT_NO_RESULTS.format(query=safe(query, limit=40)),
            cancel_only_kb(),
        )
        return ASK_SEARCH

    shown = matched[:IMPORT_RESULTS_LIMIT]
    update_flow(context, **{IMPORT_ITEMS_KEY: [_item_to_dict(item) for item in shown]})
    await send(
        update,
        context,
        t.PROVIDER_IMPORT_RESULTS.format(
            name=safe(provider.name, limit=40), shown=len(shown), total=len(matched)
        )
        + (t.PROVIDER_IMPORT_HIDDEN_TYPES.format(count=hidden) if hidden else ""),
        provider_import_results_kb(shown, provider_id=provider.id, symbol=symbol),
    )
    return PICK_ITEM


def _filter_items(items: list[ProviderServiceItem], query: str) -> list[ProviderServiceItem]:
    """تصفية خدمات المزود بكلمة بحث (`*` تعني بلا تصفية)."""
    if query.strip() == "*":
        return list(items)
    words = [word for word in normalize_digits(query).lower().split() if word]
    if not words:
        return list(items)
    return [
        item
        for item in items
        if all(word in f"{item.name} {item.category} {item.provider_service_id}".lower() for word in words)
    ]


def _item_to_dict(item: ProviderServiceItem) -> dict:
    """تحويل عنصر خدمة مزود إلى قاموس بسيط لحفظه في بيانات المحادثة."""
    return {
        "provider_service_id": item.provider_service_id,
        "name": item.name,
        "rate_per_1000_units": item.rate_per_1000_units,
        "min_quantity": item.min_quantity,
        "max_quantity": item.max_quantity,
        "category": item.category,
        "service_type": item.service_type,
    }


def _dict_to_item(data: dict) -> ProviderServiceItem:
    """إعادة بناء عنصر خدمة من القاموس المحفوظ."""
    return ProviderServiceItem(
        provider_service_id=str(data["provider_service_id"]),
        name=str(data["name"]),
        rate_per_1000_units=int(data["rate_per_1000_units"]),
        min_quantity=int(data["min_quantity"]),
        max_quantity=int(data["max_quantity"]),
        category=str(data.get("category") or ""),
        service_type=str(data.get("service_type") or "default"),
    )


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def pick_import_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """اختيار خدمة من نتائج البحث ثم سؤال التصنيف الذي ستُدرج فيه."""
    await answer_callback(update)
    data = flow(context)
    saved = data.get(IMPORT_ITEMS_KEY) or []
    index = args(update).int_arg(0, -1)

    if index < 0 or index >= len(saved):
        await answer_callback(update, t.PROVIDER_IMPORT_SESSION_LOST, alert=True)
        return ConversationHandler.END

    item = _dict_to_item(saved[index])
    update_flow(context, picked_index=index)

    services = services_of(context)
    admin = require_user(context)
    categories = await services.catalog.admin_categories(admin)
    symbol = await symbol_of(context)

    await render(
        update,
        context,
        t.PROVIDER_IMPORT_ITEM.format(
            name=safe(item.name, limit=60),
            provider_service_id=safe(item.provider_service_id, limit=20),
            cost=money_text(item.rate_per_1000_units, symbol),
            min_q=item.min_quantity,
            max_q=item.max_quantity,
            category=safe(item.category or "—", limit=30),
        ),
        category_picker_kb(categories, NS_ADM_PRV, "cat"),
    )
    return PICK_CATEGORY


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def pick_import_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """حفظ التصنيف المختار ثم سؤال هامش الربح."""
    await answer_callback(update)
    data = flow(context)
    saved = data.get(IMPORT_ITEMS_KEY) or []
    index = int(data.get("picked_index", -1))
    if index < 0 or index >= len(saved):
        await answer_callback(update, t.PROVIDER_IMPORT_SESSION_LOST, alert=True)
        return ConversationHandler.END

    category_id = args(update).int_arg(0)
    services = services_of(context)
    admin = require_user(context)
    category = await services.catalog.admin_get_category(admin, category_id)
    update_flow(context, category_id=category_id)

    item = _dict_to_item(saved[index])
    symbol = await symbol_of(context)
    example = services.catalog.price_with_margin(item.rate_per_1000_units, 40)

    await render(
        update,
        context,
        t.PROVIDER_IMPORT_ASK_MARGIN.format(
            cost=money_text(item.rate_per_1000_units, symbol),
            example=money_text(example, symbol),
        )
        + f"\n🏷 التصنيف: {safe(category.title, limit=30)}",
        cancel_only_kb(),
    )
    return ASK_MARGIN


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def receive_margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """استقبال هامش الربح وعرض ملخص الاستيراد للتأكيد."""
    message = update.effective_message
    if message is None or not message.text:
        await send(update, context, t.ERR_NOT_TEXT, cancel_only_kb())
        return ASK_MARGIN

    data = flow(context)
    saved = data.get(IMPORT_ITEMS_KEY) or []
    index = int(data.get("picked_index", -1))
    if index < 0 or index >= len(saved) or not data.get("category_id"):
        await send(update, context, t.PROVIDER_IMPORT_SESSION_LOST)
        return ConversationHandler.END

    raw = normalize_digits(message.text.strip())
    if not raw.isdigit():
        raise ValidationError(
            f"هامش غير رقمي: {raw}", user_message="❌ أرسل رقماً صحيحاً للهامش (مثال: 40)."
        )

    margin = int(raw)
    item = _dict_to_item(saved[index])
    services = services_of(context)
    admin = require_user(context)
    price = services.catalog.price_with_margin(item.rate_per_1000_units, margin)
    update_flow(context, margin=margin)

    provider = await _load_provider(context, int(data["provider_id"]))
    category = await services.catalog.admin_get_category(admin, int(data["category_id"]))
    symbol = await symbol_of(context)

    await send(
        update,
        context,
        t.PROVIDER_IMPORT_CONFIRM.format(
            name=safe(item.name, limit=60),
            category=safe(category.title, limit=30),
            provider=safe(provider.name, limit=30),
            provider_service_id=safe(item.provider_service_id, limit=20),
            cost=money_text(item.rate_per_1000_units, symbol),
            price=money_text(price, symbol),
            margin=margin,
            profit=money_text(price - item.rate_per_1000_units, symbol),
            min_q=item.min_quantity,
            max_q=item.max_quantity,
        ),
        confirm_cancel_kb(t.BTN_CONFIRM_IMPORT),
    )
    return CONFIRM_IMPORT


@guarded
@admin_required(Permission.SERVICES_CREATE)
async def confirm_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """تنفيذ الاستيراد فعلياً (الخدمة تمنع تكرار استيراد نفس خدمة المزود)."""
    await answer_callback(update)
    data = flow(context)
    saved = data.get(IMPORT_ITEMS_KEY) or []
    index = int(data.get("picked_index", -1))
    if index < 0 or index >= len(saved) or "margin" not in data:
        await render(update, context, t.PROVIDER_IMPORT_SESSION_LOST)
        return ConversationHandler.END

    services = services_of(context)
    admin = require_user(context)
    item = _dict_to_item(saved[index])
    provider_id = int(data["provider_id"])

    service = await services.catalog.import_provider_service(
        admin,
        provider_id=provider_id,
        item=item,
        category_id=int(data["category_id"]),
        margin_percent=int(data["margin"]),
    )
    clear_flow(context)
    logger.info("استورد الأدمن %s الخدمة %s", admin.telegram_id, service.id)

    await render(
        update,
        context,
        t.PROVIDER_IMPORT_DONE.format(name=safe(service.name, limit=60), service_id=service.id),
    )
    await _render_provider(update, context, provider_id, as_new=True)
    return ConversationHandler.END


# ======================================================================== #
#                                التسجيل                                   #
# ======================================================================== #


def _fallbacks() -> list:
    """المسارات الاحتياطية الموحّدة لمحادثات هذا القسم."""
    return [
        CallbackQueryHandler(cancel_admin_flow, pattern=pattern(NS_FLOW, "cancel")),
        CommandHandler("cancel", cancel_admin_flow),
        CommandHandler("start", exit_conversation(ADMIN_FLOW_KEY)),
    ]


def conversations() -> list[ConversationHandler]:
    """محادثات قسم المزودين: إضافة مزود، تغيير المفتاح، استيراد خدمة."""
    new_provider = conversation(
        entry_points=[
            CallbackQueryHandler(start_new_provider, pattern=pattern(NS_ADM_PRV, "new"))
        ],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_provider_name)],
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_provider_url)],
            ASK_TYPE: [
                CallbackQueryHandler(receive_provider_type, pattern=pattern(NS_ADM_PRV, "type"))
            ],
            ASK_CURRENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_provider_currency)],
            ASK_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_provider_key)],
            CONFIRM_NEW: [
                CallbackQueryHandler(confirm_new_provider, pattern=pattern(NS_FLOW, "confirm"))
            ],
        },
        fallbacks=_fallbacks(),
        name="admin_provider_new",
        per_chat=True,
        per_user=True,
    )

    key_update = conversation(
        entry_points=[CallbackQueryHandler(start_key_update, pattern=pattern(NS_ADM_PRV, "key"))],
        states={ASK_NEW_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_key)]},
        fallbacks=_fallbacks(),
        name="admin_provider_key",
        per_chat=True,
        per_user=True,
    )

    rate_update = conversation(
        entry_points=[CallbackQueryHandler(start_rate_update, pattern=pattern(NS_ADM_PRV, "cur"))],
        states={ASK_RATE_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rate_update)]},
        fallbacks=_fallbacks(),
        name="admin_provider_rate",
        per_chat=True,
        per_user=True,
    )

    import_service = conversation(
        entry_points=[CallbackQueryHandler(start_import, pattern=pattern(NS_ADM_PRV, "imp"))],
        states={
            ASK_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_import_query)],
            PICK_ITEM: [
                CallbackQueryHandler(pick_import_item, pattern=pattern(NS_ADM_PRV, "pick"))
            ],
            PICK_CATEGORY: [
                CallbackQueryHandler(pick_import_category, pattern=pattern(NS_ADM_PRV, "cat"))
            ],
            ASK_MARGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_margin)],
            CONFIRM_IMPORT: [
                CallbackQueryHandler(confirm_import, pattern=pattern(NS_FLOW, "confirm"))
            ],
        },
        fallbacks=_fallbacks(),
        name="admin_provider_import",
        per_chat=True,
        per_user=True,
    )
    return [new_provider, key_update, rate_update, import_service]


def screens() -> list[CallbackQueryHandler]:
    """أزرار شاشات قسم المزودين (خارج المحادثات)."""
    return [
        CallbackQueryHandler(providers_home_screen, pattern=pattern(NS_ADM_PRV, "home")),
        CallbackQueryHandler(provider_card_screen, pattern=pattern(NS_ADM_PRV, "view")),
        CallbackQueryHandler(provider_services_screen, pattern=pattern(NS_ADM_PRV, "svcs")),
        CallbackQueryHandler(test_provider, pattern=pattern(NS_ADM_PRV, "test")),
        CallbackQueryHandler(refresh_balance, pattern=pattern(NS_ADM_PRV, "bal")),
        CallbackQueryHandler(toggle_provider, pattern=pattern(NS_ADM_PRV, "toggle")),
        CallbackQueryHandler(sync_provider, pattern=pattern(NS_ADM_PRV, "sync")),
    ]

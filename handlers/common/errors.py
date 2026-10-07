"""
المعالج العام للأخطاء غير المتوقعة.

ما يفعله عند أي استثناء يصل إليه:
    1. يسجّله في اللوق مع كامل التتبع (Traceback).
    2. يحفظه في جدول `error_logs` ليراه الأدمن لاحقاً داخل البوت (المرحلة 4).
    3. يُبلغ المستخدم برسالة عربية مهذّبة بلا أي تفاصيل تقنية.

مبدأ ملزم: **لا ابتلاع أخطاء**. كل خطأ يُسجَّل ويُرى، والمستخدم لا يُترك
أمام شاشة صامتة.
"""

from __future__ import annotations

import html
import time
import traceback

from telegram import Update
from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from core.permissions import Permission
from handlers.common.ui import answer_callback, send
from texts import ar
from utils.logging_setup import get_logger
from utils.security import sanitize
from utils.throttle import AlertThrottle

logger = get_logger(__name__)

#: أقصى طول نص خطأ يُخزَّن في قاعدة البيانات
MAX_STORED_ERROR = 4000

#: أقصى طول لمقتطف الخطأ داخل تنبيه الأدمن
MAX_ALERT_SNIPPET = 200

#: مفتاح مُهدِّئ تنبيهات الأخطاء في `bot_data`
THROTTLE_KEY = "error_alert_throttle"


def _describe_update(update: object) -> str:
    """وصف مختصر للتحديث الذي سبّب الخطأ (بلا بيانات حساسة)."""
    if not isinstance(update, Update):
        return "تحديث غير معروف"
    user = update.effective_user
    parts = [f"user={user.id}" if user else "user=?"]
    if update.callback_query is not None:
        parts.append(f"button={update.callback_query.data}")
    elif update.effective_message is not None and update.effective_message.text:
        text = update.effective_message.text
        # نصّ المستخدم العادي قد يحوي سراً (مفتاح مزود يُكتب في محادثة) فلا نسجّله؛ الأوامر فقط
        if text.startswith("/"):
            parts.append(f"command={text.split()[0][:30]!r}")
        else:
            parts.append(f"text=<{len(text)} حرف>")
    return " | ".join(parts)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    المعالج العام: يُستدعى لكل استثناء لم يُلتقط داخل المعالجات.

    أخطاء الشبكة المؤقتة تُسجَّل كتحذير فقط (ستُعاد المحاولة تلقائياً)،
    وغيرها يُسجَّل كخطأ كامل في اللوق وقاعدة البيانات.
    """
    error = context.error
    description = _describe_update(update)

    if isinstance(error, (TimedOut, NetworkError, RetryAfter)):
        logger.warning("خطأ شبكة مؤقت (%s): %s", description, error)
        return

    if isinstance(error, Forbidden):
        # المستخدم حظر البوت — نعلّمه في قاعدة البيانات ولا نزعج اللوق بخطأ كامل
        logger.info("المستخدم حظر البوت (%s)", description)
        await _mark_blocked(update, context)
        return

    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    logger.error("خطأ غير متوقع (%s): %s\n%s", description, error, trace)

    await _store_error(context, description, error, trace)
    await _alert_admins(context, error)

    if isinstance(update, Update):
        # فشل إبلاغ المستخدم لا يجب أن يخفي الخطأ الأصلي ولا أن يولّد خطأً جديداً
        try:
            await answer_callback(update)
            await send(update, context, ar.ERR_GENERIC)
        except Exception as notify_error:          # noqa: BLE001 - المعالج العام لا يُسقَط
            logger.warning("تعذر إبلاغ المستخدم بالخطأ: %s", notify_error)


def _error_fingerprint(error: BaseException | None) -> str:
    """
    بصمة الخطأ لتجميع المتشابه: نوعه + موضع رمي الاستثناء (آخر إطار في التتبع).

    الموضع أدق من النوع وحده (`KeyError` في مكانين مختلفين خطآن مختلفان)،
    وأدق من النص (الذي يحوي أرقاماً متغيرة فيكسر التجميع).
    """
    if error is None:
        return "unknown"
    location = "?"
    tb = error.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        location = f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}"
    return f"{type(error).__name__}@{location}"


async def _alert_admins(context: ContextTypes.DEFAULT_TYPE, error: BaseException | None) -> None:
    """
    تنبيه من يملك `logs.view` بخطأ غير متوقع — مرة لكل بصمة في فترة التهدئة.

    يُعطَّل بالإعداد `error_alert_enabled`. النص المرسل نوع الخطأ + مقتطف منقّى فقط
    (لا تتبّع كامل ولا بيانات مستخدم)، والتفاصيل في «السجلات ← الأخطاء».
    فشل التنبيه نفسه يُسجَّل ولا يُسقط المعالج.
    """
    services = context.application.bot_data.get("services")
    if services is None:
        return
    try:
        if not await services.settings.get_bool("error_alert_enabled", True):
            return
        cooldown_minutes = await services.settings.get_int("error_alert_cooldown_minutes", 15)
        throttle = context.application.bot_data.setdefault(THROTTLE_KEY, AlertThrottle())
        fingerprint = _error_fingerprint(error)
        decision = throttle.check(fingerprint, cooldown_seconds=cooldown_minutes * 60)
        if not decision.allow:
            return

        snippet = sanitize(f"{error}")[:MAX_ALERT_SNIPPET] if error else ""
        repeated = f"\nتكرر قبل ذلك: {decision.suppressed} مرة (كُبتت تنبيهاتها)." if decision.suppressed else ""
        message = (
            f"النوع: {type(error).__name__ if error else 'غير معروف'}\n"
            f"الموضع: {fingerprint.split('@', 1)[-1]}\n"
            f"المقتطف: {html.escape(snippet) or '—'}{repeated}\n"
            "التفاصيل في: لوحة التحكم ← السجلات ← الأخطاء."
        )
        async with services.db.transaction() as tx:
            await services.notifications.notify_admins(
                tx,
                permission=Permission.LOGS_VIEW,
                token=f"error:{fingerprint}:{_epoch_bucket(cooldown_minutes)}",
                title="🚨 خطأ غير متوقع في البوت",
                message=message,
            )
    except Exception as alert_error:               # noqa: BLE001 - تنبيه الخطأ لا يُسقط المعالج العام
        logger.error("تعذر إرسال تنبيه الخطأ للأدمن: %s", alert_error)


def _epoch_bucket(cooldown_minutes: int) -> int:
    """رقم الشريحة الزمنية الحالية (يمنع تكرار إدراج الإشعار نفسه ضمن فترة التهدئة)."""
    return int(time.time() // max(60, cooldown_minutes * 60))


async def _store_error(
    context: ContextTypes.DEFAULT_TYPE, description: str, error: BaseException | None, trace: str
) -> None:
    """حفظ الخطأ في جدول `error_logs` (مع عدم إسقاط البوت إن فشل الحفظ نفسه)."""
    services = context.application.bot_data.get("services")
    if services is None:
        return
    try:
        async with services.db.transaction() as tx:
            await services.repos.error_logs.log(
                tx,
                level="ERROR",
                source="telegram_handler",
                error_type=type(error).__name__,
                message=f"{type(error).__name__}: {error}"[:500],
                traceback_snippet=html.unescape(trace)[:MAX_STORED_ERROR],
                update_summary=description,
            )
    except Exception as storage_error:            # noqa: BLE001 - تسجيل فشل التسجيل نفسه
        logger.error("تعذر حفظ الخطأ في قاعدة البيانات: %s", storage_error)


async def _mark_blocked(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تعليم المستخدم الذي حظر البوت حتى تتوقف محاولات الإرسال إليه."""
    services = context.application.bot_data.get("services")
    if services is None or not isinstance(update, Update):
        return
    telegram_user = update.effective_user
    if telegram_user is None:
        return
    try:
        user = await services.users.get_by_telegram(telegram_user.id)
        if user is not None:
            await services.users.mark_bot_blocked(user.id, True)
    except Exception as exc:                      # noqa: BLE001 - لا نُسقط المعالج العام
        logger.error("تعذر تعليم المستخدم كحاظر للبوت: %s", exc)

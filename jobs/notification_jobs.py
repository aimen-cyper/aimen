"""
مهمة إرسال الإشعارات من الطابور إلى تيليجرام.

لماذا طابور بدل الإرسال المباشر؟
    لأن الإرسال المباشر داخل المعاملة يربط نجاح العملية المالية بنجاح الشبكة.
    هنا: العملية تُسجَّل في قاعدة البيانات وتُدرج رسالتها في `notifications`،
    ثم تأتي هذه المهمة كل بضع ثوانٍ وتُرسل ما تراكم. انقطاع الشبكة يؤخر
    الإشعار فقط ولا يُفسد أي عملية.
"""

from __future__ import annotations

from telegram.ext import ContextTypes

from jobs.telegram_sender import TelegramNotificationSender
from utils.logging_setup import get_logger

logger = get_logger(__name__)

#: أقصى عدد إشعارات تُرسل في الجولة الواحدة (حماية من حدود تيليجرام)
BATCH_LIMIT = 20


async def dispatch_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    جولة إرسال واحدة: تحجز دفعة من الطابور وترسلها ثم تسجّل الحصيلة.

    الأخطاء هنا لا تُسقط المهمة الدورية: خدمة الإشعارات تُصنّف كل فشل
    (محظور / مؤقت / نهائي) وتتعامل معه، وما يصل إلى هنا يُسجَّل في اللوق.
    """
    services = context.application.bot_data["services"]
    sender = TelegramNotificationSender(context.bot)
    report = await services.notifications.dispatch_pending(sender, limit=BATCH_LIMIT)
    if report.claimed:
        logger.info("جولة إشعارات: %s", report.as_dict())

    # نسخة تنبيهات الأدمن إلى قناة التنبيهات (إن عُيّنت في الإعدادات أو `ADMIN_ALERT_CHAT_ID`)
    config = context.application.bot_data.get("config")
    await services.notifications.flush_alert_channel(
        sender, fallback_chat_id=getattr(config, "admin_alert_chat_id", None)
    )

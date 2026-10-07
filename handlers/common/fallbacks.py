"""
المسارات الاحتياطية: ما يحدث عند ضغط زر قديم أو إرسال نص خارج أي محادثة.

الهدف: ألا يصطدم المستخدم بصمت أبداً. أي تفاعل غير متوقع يُقابَل برسالة
عربية توضّح ما حدث وتعيده إلى القائمة الرئيسية.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from handlers.common import navigation
from handlers.common.guards import guarded
from handlers.common.ui import answer_callback, render, send
from keyboards.callback_data import CALLBACK_VERSION, CallbackError, unpack
from keyboards.user_kb import main_menu_kb
from texts import ar
from utils.logging_setup import get_logger

logger = get_logger(__name__)


@guarded
async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    زر لم يلتقطه أي معالج: إما من إصدار قديم أو من رسالة قديمة جداً.

    نميّز الحالتين حتى تكون الرسالة صادقة مع المستخدم.
    """
    query = update.callback_query
    raw = query.data if query else ""
    try:
        data = unpack(raw)
        outdated = not data.is_current_version
    except CallbackError:
        outdated = False

    logger.info("زر غير معالَج: %r (الإصدار الحالي v%s)", raw, CALLBACK_VERSION)
    message = ar.ERR_OLD_BUTTON if outdated else ar.ERR_UNKNOWN_BUTTON
    await answer_callback(update, message, alert=True)
    await render(update, context, message, main_menu_kb())


@guarded
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر غير معروف (/anything)."""
    await send(update, context, ar.UNKNOWN_COMMAND, main_menu_kb())


@guarded
async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نص حر خارج أي محادثة — نذكّر المستخدم بأن التنقل بالأزرار."""
    await send(update, context, ar.UNKNOWN_TEXT, main_menu_kb())


@guarded
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """الأمر /cancel خارج أي محادثة: تطمين المستخدم وعرض القائمة."""
    await send(update, context, ar.CANCELLED, main_menu_kb())


def exit_conversation(*flow_keys: str):
    """
    بناء معالج يُنهي محادثة جارية عند إرسال /start ويعيد المستخدم للقائمة.

    المدخلات:
        flow_keys : مفاتيح بيانات المحادثة التي تُمسح قبل الخروج.

    المخرجات: معالج جاهز للوضع في `fallbacks` الخاصة بالمحادثة.
    """

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """إنهاء المحادثة الحالية وعرض شاشة البداية."""
        if context.user_data is not None:
            for key in flow_keys:
                context.user_data.pop(key, None)
        await navigation.start_command(update, context)
        return ConversationHandler.END

    return handler

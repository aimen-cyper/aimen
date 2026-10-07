"""
أدوات عرض موحّدة: إرسال الشاشات وتحريرها والرد على ضغطات الأزرار.

لماذا؟
    • التنقل في البوت يكون بتحرير نفس الرسالة (`edit_message_text`) لا بإغراق
      المحادثة برسائل جديدة — هذا يحتاج تعاملاً موحّداً مع نوعي التحديث
      (ضغطة زر / رسالة نصية).
    • تيليجرام يرفض تحرير رسالة بنفس محتواها برسالة الخطأ
      «message is not modified»؛ وهي حالة طبيعية (ضغط الزر مرتين) نتعامل معها
      بوضوح بدل كتمان كل الأخطاء.
"""

from __future__ import annotations

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from utils.logging_setup import get_logger

logger = get_logger(__name__)

#: نص خطأ تيليجرام عند تحرير رسالة بمحتوى مطابق (ليس خطأً حقيقياً)
NOT_MODIFIED = "message is not modified"


async def answer_callback(update: Update, text: str = "", *, alert: bool = False) -> None:
    """
    الرد على ضغطة الزر لإزالة مؤشر الانتظار عند المستخدم.

    يُستدعى في بداية كل معالج أزرار؛ تجاهله يُبقي الزر «يدور» عند المستخدم.
    """
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer(text=text or None, show_alert=alert)
    except BadRequest as exc:
        # الاستعلام القديم (أكثر من 48 ساعة) لا يمكن الرد عليه — نسجّل ونكمل
        logger.debug("تعذر الرد على ضغطة زر قديمة: %s", exc)


async def render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    disable_preview: bool = True,
) -> None:
    """
    عرض شاشة: تحرير الرسالة الحالية إن جاء التحديث من زر، وإلا إرسال رسالة جديدة.

    المدخلات:
        text   : نص الشاشة (بتنسيق HTML).
        markup : لوحة الأزرار المرافقة.
    """
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
            )
            return
        except BadRequest as exc:
            if NOT_MODIFIED in str(exc).lower():
                return                      # المستخدم ضغط نفس الزر — لا جديد يُعرض
            logger.warning("تعذر تحرير الرسالة، سيُرسل بديل: %s", exc)

    await send(update, context, text, markup, disable_preview=disable_preview)


async def send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    disable_preview: bool = True,
) -> None:
    """إرسال رسالة جديدة إلى محادثة المستخدم (تُستخدم للنتائج والتنبيهات)."""
    chat = update.effective_chat
    if chat is None:
        logger.warning("تحديث بلا محادثة — تعذر الإرسال")
        return
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=disable_preview),
        )
    except Forbidden:
        # المستخدم حظر البوت — تُعلَّم الحالة في قاعدة البيانات عبر مهمة الإشعارات
        logger.info("المستخدم %s حظر البوت", chat.id)

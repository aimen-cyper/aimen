"""
جسر الإرسال بين طابور الإشعارات وتيليجرام.

خدمة الإشعارات لا تعرف تيليجرام إطلاقاً — تطلب فقط دالة إرسال بالشكل:
    ``async sender(telegram_id, title, message) -> message_id | None``

هذا الملف يوفّر تلك الدالة ويُترجم أخطاء تيليجرام إلى استثناءات الطابور
حتى يقرر الطابور بنفسه: يُعيد المحاولة (مؤقت) أم يتوقف (محظور/نهائي).
"""

from __future__ import annotations

import html

from core.exceptions import (
    NotificationBlockedError,
    NotificationPermanentError,
    NotificationTemporaryError,
)
from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

#: أقصى طول نص رسالة في تيليجرام (نقتطع قبله بهامش أمان)
MAX_MESSAGE_LEN = 3900


class TelegramNotificationSender:
    """مُرسل إشعارات عبر تيليجرام مع تصنيف دقيق للأخطاء."""

    def __init__(self, bot: Bot) -> None:
        """تهيئة المرسل ببوت تيليجرام الجاهز."""
        self.bot = bot

    async def __call__(self, telegram_id: int, title: str, message: str) -> int | None:
        """
        إرسال إشعار واحد.

        المخرجات: رقم الرسالة في تيليجرام عند النجاح.

        الاستثناءات:
            NotificationBlockedError   : المستخدم حظر البوت (لا تُعاد المحاولة).
            NotificationTemporaryError : شبكة/مهلة/حد معدل (تُعاد المحاولة).
            NotificationPermanentError : رسالة غير صالحة أو محادثة غير موجودة.
        """
        text = self._format(title, message)
        try:
            sent = await self.bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Forbidden as exc:
            raise NotificationBlockedError(f"المستخدم {telegram_id} حظر البوت: {exc}") from exc
        except RetryAfter as exc:
            raise NotificationTemporaryError(
                f"تجاوز حد الإرسال، إعادة المحاولة بعد {exc.retry_after}ث"
            ) from exc
        except (TimedOut, NetworkError) as exc:
            raise NotificationTemporaryError(f"خطأ شبكة أثناء الإرسال: {exc}") from exc
        except BadRequest as exc:
            text_error = str(exc).lower()
            if "chat not found" in text_error or "user is deactivated" in text_error:
                raise NotificationBlockedError(f"محادثة غير متاحة: {exc}") from exc
            raise NotificationPermanentError(f"رسالة مرفوضة من تيليجرام: {exc}") from exc
        return sent.message_id

    @staticmethod
    def _format(title: str, message: str) -> str:
        """تنسيق الإشعار: عنوان عريض ثم النص، مع تهريب HTML واقتطاع الطويل."""
        safe_title = html.escape(title or "")
        safe_message = html.escape(message or "")
        body = f"<b>{safe_title}</b>\n\n{safe_message}" if safe_title else safe_message
        if len(body) > MAX_MESSAGE_LEN:
            body = body[: MAX_MESSAGE_LEN - 1] + "…"
        return body


class TelegramBroadcastSender:
    """
    مُرسل الرسائل الجماعية (نص أو صورة بتعليق) بنفس تصنيف أخطاء الإشعارات.

    لماذا مُرسل مستقل عن `TelegramNotificationSender`؟
        إشعار النظام له عنوان ونص ويُنسَّق بـHTML، أما الإذاعة فنص يكتبه الأدمن
        بحروفه وقد يرافقه ملف. خلطهما في دالة واحدة كان سيجبرنا على معاملات
        اختيارية تُربك الطرفين. التصنيف الدقيق للأخطاء مشترك عبر `_classify`.
    """

    def __init__(self, bot: Bot) -> None:
        """تهيئة المرسل ببوت تيليجرام الجاهز."""
        self.bot = bot

    async def __call__(
        self,
        telegram_id: int,
        text: str,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> int | None:
        """
        إرسال رسالة إذاعة واحدة.

        المخرجات: رقم الرسالة في تيليجرام عند النجاح.

        الاستثناءات: نفس استثناءات الإشعارات (محظور / مؤقت / نهائي) ليتعامل
        معها طابور الإذاعة بنفس المنطق المختبر.
        """
        body = html.escape(text or "")[:MAX_MESSAGE_LEN]
        try:
            if media_file_id and media_type == "photo":
                sent = await self.bot.send_photo(
                    chat_id=telegram_id,
                    photo=media_file_id,
                    caption=body[:1024] or None,
                    parse_mode=ParseMode.HTML,
                )
            elif media_file_id and media_type == "document":
                sent = await self.bot.send_document(
                    chat_id=telegram_id,
                    document=media_file_id,
                    caption=body[:1024] or None,
                    parse_mode=ParseMode.HTML,
                )
            else:
                sent = await self.bot.send_message(
                    chat_id=telegram_id,
                    text=body,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
        except Forbidden as exc:
            raise NotificationBlockedError(f"المستخدم {telegram_id} حظر البوت: {exc}") from exc
        except RetryAfter as exc:
            raise NotificationTemporaryError(
                f"تجاوز حد الإرسال، إعادة المحاولة بعد {exc.retry_after}ث"
            ) from exc
        except (TimedOut, NetworkError) as exc:
            raise NotificationTemporaryError(f"خطأ شبكة أثناء الإرسال: {exc}") from exc
        except BadRequest as exc:
            text_error = str(exc).lower()
            if "chat not found" in text_error or "user is deactivated" in text_error:
                raise NotificationBlockedError(f"محادثة غير متاحة: {exc}") from exc
            raise NotificationPermanentError(f"رسالة مرفوضة من تيليجرام: {exc}") from exc
        return sent.message_id

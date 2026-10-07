"""
خدمة الإشعارات (Outbox Pattern).

المبدأ: لا نرسل رسالة تيليجرام أثناء معاملة قاعدة البيانات — بل نضع الإشعار في طابور
داخل نفس المعاملة التي غيّرت الحالة. النتيجة:
    • لا حدث بلا إشعار (لأن الإشعار يُحفظ مع الحدث نفسه).
    • لا إشعار بلا حدث (لأن الإشعار يسقط مع تراجع المعاملة).
    • فشل تيليجرام لا يُسقط العملية المالية إطلاقاً.

الإرسال الفعلي يتم لاحقاً عبر `dispatch_pending` التي يستدعيها job دوري (المرحلة 6)،
وهي تأخذ دالة إرسال من الخارج حتى تبقى هذه الطبقة مستقلة تماماً عن مكتبة تيليجرام.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.constants import (
    NOTIF_KEY_MANUAL,
    NOTIF_KEY_ORDER,
    NOTIF_KEY_PAYMENT,
    NOTIF_KEY_TICKET,
)
from core.enums import NotificationType
from core.exceptions import (
    NotificationBlockedError,
    NotificationPermanentError,
    NotificationTemporaryError,
)
from database.database import Database, Tx
from database.models import Notification
from database.repositories import Repositories
from services.settings_service import SettingsService
from utils.logging_setup import get_logger
from utils.security import sanitize

logger = get_logger(__name__)

#: توقيع دالة الإرسال التي تُمرَّر من طبقة تيليجرام
#: تُرجع معرف الرسالة المرسلة (أو None) وترفع أحد استثناءات الإشعارات عند الفشل.
NotificationSender = Callable[[int, str, str], Awaitable[int | None]]


@dataclass(slots=True)
class DispatchReport:
    """حصيلة جولة إرسال واحدة من الطابور."""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        """تحويل الحصيلة إلى قاموس للتسجيل في اللوق."""
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
        }


class NotificationService:
    """إدراج الإشعارات في الطابور وإرسالها لاحقاً بأمان."""

    def __init__(self, db: Database, repos: Repositories, settings: SettingsService) -> None:
        """تهيئة الخدمة."""
        self.db = db
        self.repos = repos
        self.settings = settings
        #: نسخ تنبيهات الأدمن المنتظرة للإرسال إلى قناة التنبيهات (في الذاكرة، محدودة، best-effort):
        #: الإرسال الدائم يبقى للأدمن أنفسهم عبر الطابور؛ القناة نسخة إضافية لا يتوقف عليها شيء
        self._alert_outbox: deque[tuple[str, str]] = deque(maxlen=100)

    # ------------------------------- الإدراج ------------------------------ #

    async def enqueue(
        self,
        tx: Tx,
        *,
        user_id: int,
        type: str,
        title: str,
        message: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """
        وضع إشعار في الطابور داخل معاملة قائمة.

        المخرجات: معرف الإشعار، أو None إذا كان `dedupe_key` موجوداً مسبقاً
                  (أي أن الإشعار أُرسل أو مجدول أصلاً — وهذا سلوك مقصود لمنع التكرار).
        """
        import json

        return await self.repos.notifications.enqueue(
            tx,
            user_id=user_id,
            type=type,
            title=title,
            message=message,
            dedupe_key=dedupe_key,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
        )

    async def notify_order(
        self,
        tx: Tx,
        *,
        user_id: int,
        order_id: int,
        event: str,
        title: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """إشعار متعلق بطلب، بمفتاح منع تكرار مبني على (الطلب + الحدث)."""
        return await self.enqueue(
            tx,
            user_id=user_id,
            type=event,
            title=title,
            message=message,
            dedupe_key=NOTIF_KEY_ORDER.format(order_id=order_id, event=event),
            payload=payload,
        )

    async def notify_payment(
        self,
        tx: Tx,
        *,
        user_id: int,
        payment_id: int,
        event: str,
        title: str,
        message: str,
    ) -> int | None:
        """إشعار متعلق بطلب شحن."""
        return await self.enqueue(
            tx,
            user_id=user_id,
            type=event,
            title=title,
            message=message,
            dedupe_key=NOTIF_KEY_PAYMENT.format(payment_id=payment_id, event=event),
        )

    async def notify_ticket(
        self, tx: Tx, *, user_id: int, ticket_id: int, event: str, title: str, message: str
    ) -> int | None:
        """إشعار متعلق بتذكرة دعم."""
        return await self.enqueue(
            tx,
            user_id=user_id,
            type=event,
            title=title,
            message=message,
            dedupe_key=NOTIF_KEY_TICKET.format(ticket_id=ticket_id, event=event),
        )

    async def notify_user_manual(
        self, tx: Tx, *, user_id: int, token: str, title: str, message: str, type: str = NotificationType.SYSTEM
    ) -> int | None:
        """إشعار يدوي/عام بمفتاح منع تكرار يحدده المتصل."""
        return await self.enqueue(
            tx,
            user_id=user_id,
            type=type,
            title=title,
            message=message,
            dedupe_key=NOTIF_KEY_MANUAL.format(token=token),
        )

    async def notify_admins(
        self,
        tx: Tx,
        *,
        permission: str,
        token: str,
        title: str,
        message: str,
        type: str = NotificationType.SYSTEM,
    ) -> int:
        """
        إرسال تنبيه لكل من يملك صلاحية معينة (مثل `orders.view` أو `payments.approve`).

        المخرجات: عدد الإشعارات التي أُدرجت فعلاً (المكررة لا تُحتسب).

        ملاحظة: نستخدم الصلاحية لا الدور، حتى يصل التنبيه لمن مُنح الصلاحية استثناءً.
        """
        admins = await self._users_with_permission(permission)
        inserted = 0
        for user_id in admins:
            notification_id = await self.enqueue(
                tx,
                user_id=user_id,
                type=type,
                title=title,
                message=message,
                dedupe_key=NOTIF_KEY_MANUAL.format(token=f"{token}:{user_id}"),
            )
            if notification_id is not None:
                inserted += 1
        if inserted:
            self._alert_outbox.append((title, message))      # نسخة واحدة للقناة لا لكل أدمن
        return inserted

    async def flush_alert_channel(self, sender: NotificationSender, *, fallback_chat_id: int | None = None) -> int:
        """
        إرسال نسخ تنبيهات الأدمن المتراكمة إلى قناة/مجموعة التنبيهات (إن عُيّنت).

        القناة المستهدفة: الإعداد `admin_alert_chat_id` (يُعدَّل من البوت) وإلا متغير البيئة
        `ADMIN_ALERT_CHAT_ID` (`fallback_chat_id`). بلا قناة تُفرَّغ النسخ (لا تتراكم بلا نهاية).
        فشل الإرسال يُسجَّل ولا يُعاد (لا يتوقف عليه شيء: الأدمن استلموا تنبيههم عبر الطابور).

        المخرجات: عدد النسخ المرسلة فعلاً.
        """
        if not self._alert_outbox:
            return 0
        raw = (await self.settings.get_str("admin_alert_chat_id", "")).strip()
        chat_id = int(raw) if raw.lstrip("-").isdigit() else fallback_chat_id
        pending = list(self._alert_outbox)
        self._alert_outbox.clear()
        if chat_id is None:
            return 0

        sent = 0
        for title, message in pending:
            try:
                await sender(chat_id, title, message)
            except (NotificationBlockedError, NotificationPermanentError, NotificationTemporaryError) as exc:
                logger.warning("تعذّر إرسال نسخة التنبيه لقناة التنبيهات: %s", sanitize(str(exc))[:200])
                continue
            sent += 1
        return sent

    async def _users_with_permission(self, permission: str) -> list[int]:
        """
        جلب معرفات المستخدمين الذين يملكون صلاحية محددة (من الدور أو باستثناء فردي).

        يستثني المحظورين ومن حظروا البوت حتى لا نُراكم إشعارات لا تصل.
        """
        rows = await self.db.fetchall(
            """
            SELECT DISTINCT u.id
              FROM users u
              LEFT JOIN role_permissions rp ON rp.role_code = u.role AND rp.permission_code = ?
              LEFT JOIN user_permissions up ON up.user_id = u.id AND up.permission_code = ?
             WHERE u.is_banned = 0
               AND u.blocked_bot = 0
               AND (
                     (rp.permission_code IS NOT NULL AND COALESCE(up.allow, 1) = 1)
                  OR up.allow = 1
                   )
            """,
            (permission, permission),
        )
        return [row["id"] for row in rows]

    # ------------------------------- الإرسال ------------------------------ #

    async def dispatch_pending(
        self, sender: NotificationSender, *, limit: int = 25, lease_seconds: int = 60
    ) -> DispatchReport:
        """
        إرسال دفعة من الإشعارات المنتظرة.

        المدخلات:
            sender : دالة غير متزامنة (telegram_id, title, message) → معرف الرسالة.
                     يجب أن ترفع NotificationBlockedError إذا حظر المستخدم البوت،
                     وNotificationTemporaryError عند فشل قابل لإعادة المحاولة.

        المخرجات: تقرير مختصر بعدد ما أُرسل وفشل وتُخطّي.

        ملاحظة مهمة: الحجز والإرسال والتعليم تتم في معاملات منفصلة قصيرة،
        فلا تبقى قاعدة البيانات مقفولة أثناء انتظار الشبكة.
        """
        report = DispatchReport()

        async with self.db.transaction() as tx:
            batch = await self.repos.notifications.claim_batch(
                tx, limit=limit, lease_seconds=lease_seconds
            )
        report.claimed = len(batch)
        if not batch:
            return report

        for notification in batch:
            telegram_id = await self._telegram_id_for(notification.user_id)
            if telegram_id is None:
                async with self.db.transaction() as tx:
                    await self.repos.notifications.mark_skipped(
                        tx, notification.id, "المستخدم غير موجود"
                    )
                report.skipped += 1
                continue

            try:
                message_id = await sender(telegram_id, notification.title, notification.message)
            except NotificationBlockedError as exc:
                await self._handle_blocked(notification, str(exc))
                report.skipped += 1
            except NotificationTemporaryError as exc:
                async with self.db.transaction() as tx:
                    await self.repos.notifications.mark_failed(
                        tx, notification.id, error=sanitize(str(exc))
                    )
                report.failed += 1
            except NotificationPermanentError as exc:
                async with self.db.transaction() as tx:
                    await self.repos.notifications.mark_failed(
                        tx, notification.id, error=sanitize(str(exc)), permanent=True
                    )
                report.failed += 1
            else:
                async with self.db.transaction() as tx:
                    await self.repos.notifications.mark_sent(tx, notification.id, message_id)
                report.sent += 1

        logger.info("جولة إرسال إشعارات: %s", report.as_dict())
        return report

    async def _handle_blocked(self, notification: Notification, error: str) -> None:
        """تعليم الإشعار كمتخطى وتسجيل أن المستخدم حظر البوت (فلا نزعجه بإشعارات لاحقة)."""
        async with self.db.transaction() as tx:
            await self.repos.notifications.mark_skipped(tx, notification.id, sanitize(error))
            await self.repos.users.set_blocked_bot(tx, notification.user_id, True)
        logger.info("المستخدم %s حظر البوت — تم تخطي الإشعار %s", notification.user_id, notification.id)

    async def _telegram_id_for(self, user_id: int) -> int | None:
        """جلب معرف تيليجرام لمستخدم داخلي."""
        user = await self.repos.users.get_by_id(user_id)
        return user.telegram_id if user else None

    # ------------------------------- القراءة ------------------------------ #

    async def user_inbox(self, user_id: int, *, limit: int = 10, offset: int = 0) -> list[Notification]:
        """آخر إشعارات المستخدم لعرضها داخل البوت."""
        return await self.repos.notifications.list_for_user(user_id, limit=limit, offset=offset)

    async def unread_count(self, user_id: int) -> int:
        """عدد الإشعارات غير المقروءة."""
        return await self.repos.notifications.count_unread(user_id)

    async def mark_all_read(self, user_id: int) -> int:
        """تعليم كل إشعارات المستخدم كمقروءة."""
        async with self.db.transaction() as tx:
            return await self.repos.notifications.mark_read_for_user(tx, user_id)

    async def queued_count(self) -> int:
        """عدد الإشعارات المنتظرة في الطابور (مؤشر صحة النظام)."""
        return await self.repos.notifications.count_queued()

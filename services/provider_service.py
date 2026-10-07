"""
خدمة المزودين: إدارة بياناتهم، وتشفير مفاتيحهم، والاتصال بهم بأمان.

قواعد صارمة:
    • مفتاح API يُخزَّن مشفّراً، ولا يُفك إلا لحظة الاستدعاء داخل هذه الخدمة.
    • لا يظهر المفتاح في أي سجل أو رسالة خطأ (كل النصوص تمر على `sanitize`).
    • كل استدعاء خارجي يُسجَّل في `provider_requests` بنجاحه أو فشله ومدته.
    • قاطع دائرة: بعد عدد أخطاء متتالية يُعطَّل المزود مؤقتاً ويُنبَّه الأدمن.
    • لا تُفتح معاملة قاعدة بيانات أثناء انتظار الشبكة.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from core.enums import ProviderAction
from core.exceptions import (
    ProviderError,
    ProviderPermanentError,
    ProviderTemporaryError,
    ProviderUncertainError,
    ValidationError,
)
from core.permissions import Permission
from core.setting_rules import normalize_decimal_text
from database.database import Database
from database.models import Order, Provider, Service, User
from database.repositories import Repositories
from integrations.providers.base import (
    ProviderBalance,
    ProviderClient,
    ProviderOrderResult,
    ProviderServiceItem,
    ProviderStatusResult,
)
from integrations.providers.registry import create_client, is_supported
from services.audit_service import AuditService
from services.auth_service import AuthService
from services.notification_service import NotificationService
from services.settings_service import SettingsService
from texts import templates
from utils.datetime_utils import is_past, iso_plus_seconds
from utils.logging_setup import get_logger
from utils.security import SecretBox, secret_hint
from utils.net_security import validate_public_https_url
from utils.validators import clean_text

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """حالة صحة مزود كما تُعرض في لوحة الأدمن."""

    provider: Provider
    is_usable: bool
    reason: str


class ProviderService:
    """كل تعامل مع مزودي الخدمات يمر من هنا."""

    def __init__(
        self,
        db: Database,
        repos: Repositories,
        settings: SettingsService,
        audit: AuditService,
        auth: AuthService,
        notifications: NotificationService,
        secret_box: SecretBox,
    ) -> None:
        """تهيئة الخدمة مع صندوق التشفير المسؤول عن مفاتيح المزودين."""
        self.db = db
        self.repos = repos
        self.settings = settings
        self.audit = audit
        self.auth = auth
        self.notifications = notifications
        self.secret_box = secret_box

    # ------------------------------- القراءة ------------------------------ #

    async def get(self, provider_id: int) -> Provider | None:
        """جلب مزود بالمعرف."""
        return await self.repos.providers.get(provider_id)

    async def list_all(self, *, active_only: bool = False) -> list[Provider]:
        """قائمة المزودين (كلهم أو المفعّلون فقط)."""
        return await self.repos.providers.list_all(active_only=active_only)

    async def health(self, provider: Provider) -> ProviderHealth:
        """
        تقييم جاهزية المزود للاستخدام الآن.

        المخرجات: كائن يوضح هل يمكن الإرسال إليه ولماذا لا، بصيغة عربية للعرض.
        """
        if not provider.is_active:
            return ProviderHealth(provider, False, "المزود معطّل يدوياً")
        if provider.disabled_until and not is_past(provider.disabled_until):
            return ProviderHealth(provider, False, "المزود موقوف مؤقتاً بعد أخطاء متتالية")
        if not is_supported(provider.api_type):
            return ProviderHealth(provider, False, f"نوع API غير مدعوم: {provider.api_type}")
        _, problem = await self.rate_state(provider)
        if problem:
            return ProviderHealth(provider, False, problem)
        return ProviderHealth(provider, True, "جاهز")

    # ------------------------------ العملة ------------------------------- #

    async def rate_state(self, provider: Provider) -> tuple[Decimal | None, str | None]:
        """
        سعر تحويل عملة المزود إلى عملة النظام، أو سبب تعذّره.

        القاعدة: سعر مضبوط ⇒ يُستعمل. بلا سعر ⇒ مقبول فقط إن كانت عملة المزود هي عملة النظام (السعر 1)،
        وإلا (None، رسالة عربية) — فلا يُفترض 1:1 بصمت أبداً (كان ذلك يُسعِّر بالدولار كأنه ريال).
        """
        system_code, _, _ = await self.settings.currency()
        if provider.exchange_rate:
            return Decimal(provider.exchange_rate), None
        provider_code = (provider.currency or "").strip().upper()
        if provider_code and provider_code == system_code.strip().upper():
            return Decimal(1), None
        return None, (
            f"عملة المزود ({provider_code or '؟'}) تختلف عن عملة النظام ({system_code}) ولم يُحدَّد سعر الصرف"
        )

    async def system_rate(self, provider: Provider) -> Decimal:
        """
        سعر التحويل إلى عملة النظام لهذا المزود.

        الاستثناءات: ProviderError برسالة عربية تدلّ الأدمن على مكان الإصلاح.
        """
        rate, problem = await self.rate_state(provider)
        if rate is None:
            raise ProviderError(
                problem or "سعر الصرف غير محدد",
                user_message=f"⚠️ {problem}. حدّده من بطاقة المزود («💱 العملة وسعر الصرف»).",
            )
        return rate

    @staticmethod
    def to_system_units(units: int, rate: Decimal) -> int:
        """تحويل مبلغ بوحدات المال من عملة المزود إلى عملة النظام (تقريب نصف لأعلى، Decimal فقط)."""
        return int((Decimal(units) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    async def _convert_known(self, provider: Provider, units: int | None) -> int | None:
        """
        تحويل مبلغ وارد من المزود إلى عملة النظام إن أمكن، وإلا None (لا قيمة خاطئة تُخزَّن).

        يُستعمل لمسارات المتابعة (الحالة) حيث لا يجوز أن يعطّل غياب سعر الصرف تتبّع طلب أُرسل فعلاً.
        """
        if units is None:
            return None
        rate, problem = await self.rate_state(provider)
        if rate is None:
            logger.warning("مبلغ من المزود %s لم يُحوَّل: %s", provider.id, problem)
            return None
        return self.to_system_units(units, rate)

    @staticmethod
    def parse_currency_spec(text: str) -> tuple[str, str | None]:
        """
        تحليل إدخال الأدمن «العملة [سعر الصرف]» مثل `USD 530` أو `YER`.

        المخرجات: (رمز العملة بأحرف كبيرة، سعر الصرف المطبَّع أو None).
        الاستثناءات: ValidationError برسالة عربية.
        """
        parts = (text or "").replace("،", " ").split()
        if not 1 <= len(parts) <= 2:
            raise ValidationError(
                f"إدخال عملة غير صالح: {text!r}",
                user_message="❌ أرسل رمز العملة ثم سعر الصرف (اختياري إن كانت عملة النظام). مثال: <code>USD 530</code>",
            )
        code = parts[0].strip().upper()
        if not (2 <= len(code) <= 6 and code.isascii() and code.isalpha()):
            raise ValidationError(
                f"رمز عملة غير صالح: {code!r}", user_message="❌ رمز العملة أحرف لاتينية (مثل USD أو SAR)."
            )
        rate = normalize_decimal_text(parts[1]) if len(parts) == 2 else None
        return code, rate

    async def validate_currency(self, currency: str, exchange_rate: str | None) -> tuple[str, str | None]:
        """اتساق العملة وسعرها: عملة مختلفة عن النظام تتطلب سعراً (ولا يُقبل رفض ضمني لاحقاً)."""
        system_code, _, _ = await self.settings.currency()
        code = currency.strip().upper()
        rate = normalize_decimal_text(exchange_rate) if exchange_rate not in (None, "") else None
        if rate is None and code != system_code.strip().upper():
            raise ValidationError(
                f"عملة {code} تختلف عن عملة النظام {system_code} بلا سعر صرف",
                user_message=f"❌ عملة المزود ({code}) تختلف عن عملة النظام ({system_code}) — أرسل سعر الصرف أيضاً. مثال: <code>{code} 530</code>",
            )
        return code, rate

    async def set_currency(
        self, actor: User, provider_id: int, *, currency: str, exchange_rate: str | None
    ) -> Provider:
        """
        ضبط عملة المزود وسعر صرفها إلى عملة النظام.

        لا يغيّر أسعار خدمات مستوردة سابقاً: التكلفة المخزّنة تُحدَّث عند المزامنة التالية، لذلك تنبّه الواجهة
        الأدمن لتشغيل «🔄 مزامنة» بعد التغيير.

        الاستثناءات: PermissionDeniedError · ValidationError · ProviderError (مزود غير موجود)
        """
        await self.auth.require_permission(
            actor, Permission.PROVIDERS_UPDATE, context=f"provider:{provider_id}"
        )
        code, rate = await self.validate_currency(currency, exchange_rate)
        async with self.db.transaction() as tx:
            before = await self.repos.providers.get(provider_id)
            if before is None:
                raise ProviderError(f"مزود غير موجود: {provider_id}", user_message="❌ المزود غير موجود.")
            await self.repos.providers.update_fields(tx, provider_id, currency=code, exchange_rate=rate)
            await self.audit.log(
                tx,
                action="providers.set_currency",
                admin_id=actor.id,
                target_type="provider",
                target_id=provider_id,
                before={"currency": before.currency, "exchange_rate": before.exchange_rate},
                after={"currency": code, "exchange_rate": rate},
            )
        provider = await self.repos.providers.get(provider_id)
        assert provider is not None
        logger.info("ضبط الأدمن %s عملة المزود %s: %s × %s", actor.id, provider_id, code, rate)
        return provider

    async def is_usable(self, provider: Provider) -> bool:
        """هل يمكن إرسال طلبات لهذا المزود الآن؟"""
        return (await self.health(provider)).is_usable

    # ------------------------------ الإدارة ------------------------------- #

    async def create_provider(
        self,
        actor: User,
        *,
        name: str,
        api_url: str,
        api_key: str,
        api_type: str = "perfect_panel",
        currency: str | None = None,
        exchange_rate: str | None = None,
        timeout_seconds: int = 30,
    ) -> Provider:
        """
        إضافة مزود جديد مع تشفير مفتاحه قبل التخزين.

        `currency=None` ⇒ عملة النظام (سعر الصرف غير لازم). عملة مختلفة تتطلب `exchange_rate`.

        الاستثناءات: PermissionDeniedError · ValidationError · EncryptionError
        """
        await self.auth.require_permission(actor, Permission.PROVIDERS_CREATE, context="provider.create")
        clean_name = clean_text(name, field="اسم المزود", max_length=60, min_length=2)
        clean_gateway = validate_public_https_url(api_url)   # https + مضيف عام (منع SSRF)
        key = (api_key or "").strip()
        if len(key) < 8:
            raise ProviderError(
                "مفتاح API قصير جداً", user_message="❌ مفتاح API غير صالح."
            )
        system_code, _, _ = await self.settings.currency()
        clean_currency, clean_rate = await self.validate_currency(currency or system_code, exchange_rate)

        async with self.db.transaction() as tx:
            # الفحص داخل المعاملة نفسها (BEGIN IMMEDIATE) فلا يتسلل مكرر عند إنشاءين متزامنين
            if await tx.fetchval("SELECT 1 FROM providers WHERE name = ?", (clean_name,)):
                raise ProviderError(
                    f"اسم المزود {clean_name} مستخدم", user_message="❌ يوجد مزود بنفس الاسم."
                )
            provider = await self.repos.providers.create(
                tx,
                name=clean_name,
                api_url=clean_gateway,
                api_key_enc=self.secret_box.encrypt(key),
                api_key_hint=secret_hint(key),
                api_type=api_type,
                currency=clean_currency,
                exchange_rate=clean_rate,
                timeout_seconds=timeout_seconds,
            )
            await self.audit.log(
                tx,
                action="providers.create",
                admin_id=actor.id,
                target_type="provider",
                target_id=provider.id,
                after={
                    "name": clean_name, "api_url": clean_gateway, "api_type": api_type,
                    "currency": clean_currency, "exchange_rate": clean_rate,
                },
            )
        logger.info("أضاف الأدمن %s المزود %s (%s)", actor.id, provider.id, clean_name)
        return provider

    async def update_api_key(self, actor: User, provider_id: int, new_key: str) -> bool:
        """
        تحديث مفتاح API لمزود (يُخزَّن مشفّراً، ولا يُسجَّل في أي مكان).

        الاستثناءات: PermissionDeniedError · ProviderError
        """
        await self.auth.require_permission(
            actor, Permission.PROVIDERS_KEY_UPDATE, context=f"provider:{provider_id}"
        )
        key = (new_key or "").strip()
        if len(key) < 8:
            raise ProviderError("مفتاح API قصير جداً", user_message="❌ مفتاح API غير صالح.")

        async with self.db.transaction() as tx:
            changed = await self.repos.providers.update_fields(
                tx,
                provider_id,
                api_key_enc=self.secret_box.encrypt(key),
                api_key_hint=secret_hint(key),
            )
            if changed:
                # لا نُسجّل المفتاح نفسه — فقط أن التغيير حدث ومن نفّذه
                await self.audit.log(
                    tx,
                    action="providers.key.update",
                    admin_id=actor.id,
                    target_type="provider",
                    target_id=provider_id,
                    after={"api_key_hint": secret_hint(key)},
                )
        return changed

    async def set_active(self, actor: User, provider_id: int, active: bool) -> bool:
        """تفعيل أو تعطيل مزود يدوياً."""
        await self.auth.require_permission(
            actor, Permission.PROVIDERS_UPDATE, context=f"provider:{provider_id}"
        )
        async with self.db.transaction() as tx:
            changed = await self.repos.providers.set_active(tx, provider_id, active)
            if changed:
                await self.audit.log(
                    tx,
                    action="providers.update",
                    admin_id=actor.id,
                    target_type="provider",
                    target_id=provider_id,
                    after={"is_active": active},
                )
        return changed

    # ------------------------------ الاتصال ------------------------------- #

    def client_for(self, provider: Provider) -> ProviderClient:
        """
        بناء عميل اتصال للمزود بعد فك تشفير مفتاحه.

        الاستثناءات:
            EncryptionError : المفتاح المخزَّن لا يُفك بمفتاح التشفير الحالي.
            ProviderError   : نوع API غير مدعوم.
        """
        api_key = self.secret_box.decrypt(provider.api_key_enc)
        return create_client(
            api_type=provider.api_type,
            api_url=provider.api_url,
            api_key=api_key,
            timeout_seconds=provider.timeout_seconds,
        )

    async def _log_request(
        self,
        *,
        provider: Provider,
        action: str,
        is_success: bool,
        started: float,
        request_summary: str,
        response_snippet: str,
        error_type: str | None = None,
        order_id: int | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """تسجيل استدعاء المزود في قاعدة البيانات بعد تنقيته من الأسرار."""
        duration_ms = int((time.monotonic() - started) * 1000)
        async with self.db.transaction() as tx:
            await self.repos.provider_requests.log(
                tx,
                provider_id=provider.id,
                action=action,
                is_success=is_success,
                order_id=order_id,
                request_summary=request_summary,
                response_snippet=response_snippet,
                duration_ms=duration_ms,
                error_type=error_type,
                secrets=secrets,
            )

    async def _record_success(self, provider: Provider) -> None:
        """تصفير عدّاد الأخطاء بعد نجاح الاتصال."""
        async with self.db.transaction() as tx:
            await self.repos.providers.record_success(tx, provider.id)

    async def _record_failure(self, provider: Provider, error: str) -> int:
        """
        تسجيل خطأ متتالٍ، وتعطيل المزود مؤقتاً عند بلوغ الحد، مع تنبيه الأدمن.

        المخرجات: عدد الأخطاء المتتالية بعد هذا الخطأ.
        """
        threshold = await self.settings.get_int("provider_failure_threshold", 5)
        disable_minutes = await self.settings.get_int("provider_disable_minutes", 15)

        async with self.db.transaction() as tx:
            current = await self.repos.providers.record_failure(tx, provider.id, error)
            if current >= threshold:
                await self.repos.providers.update_fields(
                    tx, provider.id, disabled_until=iso_plus_seconds(disable_minutes * 60)
                )
                title, message = templates.admin_provider_disabled(
                    provider_name=provider.name, failures=current, minutes=disable_minutes
                )
                await self.notifications.notify_admins(
                    tx,
                    permission=Permission.PROVIDERS_VIEW,
                    token=f"provider:{provider.id}:disabled:{current}",
                    title=title,
                    message=message,
                )
                logger.error(
                    "تعطيل مؤقت للمزود %s بعد %s أخطاء متتالية", provider.name, current
                )
        return current

    # ------------------------------ العمليات ------------------------------ #

    async def submit_order(self, provider: Provider, service: Service, order: Order) -> ProviderOrderResult:
        """
        إرسال طلب إلى المزود.

        المخرجات: نتيجة فيها رقم الطلب عند المزود.

        الاستثناءات:
            ProviderTemporaryError  : فشل مؤقت — يُعاد المحاولة لاحقاً.
            ProviderUncertainError  : نتيجة غير مؤكدة — ممنوع إعادة الإرسال آلياً.
            ProviderPermanentError  : رفض نهائي — فشل الطلب واسترجاع المبلغ.
        """
        # قبل الاتصال بالمزود وليس بعده: لو رُفض بسبب العملة بعد قبول المزود للطلب لضاعت النتيجة
        try:
            rate = await self.system_rate(provider)
        except ProviderError as exc:
            raise ProviderTemporaryError(str(exc), user_message=exc.user_message) from exc
        system_code, _, _ = await self.settings.currency()

        client = self.client_for(provider)
        summary = f"add service={service.provider_service_id} quantity={order.quantity} link={order.link}"
        started = time.monotonic()
        try:
            result = await client.add_order(
                provider_service_id=service.provider_service_id,
                link=order.link,
                quantity=order.quantity,
            )
        except ProviderUncertainError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.ADD, is_success=False, started=started,
                request_summary=summary, response_snippet=str(exc),
                error_type=type(exc).__name__, order_id=order.id, secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        except ProviderError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.ADD, is_success=False, started=started,
                request_summary=summary, response_snippet=str(exc),
                error_type=type(exc).__name__, order_id=order.id, secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        finally:
            await client.close()

        if not result.provider_order_id:
            raise ProviderPermanentError("المزود لم يُرجع رقم طلب صالحاً")

        await self._log_request(
            provider=provider, action=ProviderAction.ADD, is_success=True, started=started,
            request_summary=summary, response_snippet=str(result.raw)[:500],
            order_id=order.id, secrets=client.secrets(),
        )
        await self._record_success(provider)
        if result.charge_units is not None:
            result = replace(
                result, charge_units=self.to_system_units(result.charge_units, rate), currency=system_code
            )
        return result

    async def fetch_status(self, provider: Provider, order: Order) -> ProviderStatusResult:
        """
        الاستعلام عن حالة طلب لدى المزود.

        الاستثناءات: ProviderTemporaryError · ProviderPermanentError
        """
        if not order.provider_order_id:
            raise ProviderPermanentError(f"الطلب #{order.id} بلا رقم عند المزود")

        client = self.client_for(provider)
        started = time.monotonic()
        try:
            result = await client.get_order_status(order.provider_order_id)
        except ProviderError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.STATUS, is_success=False, started=started,
                request_summary=f"status order={order.provider_order_id}", response_snippet=str(exc),
                error_type=type(exc).__name__, order_id=order.id, secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        finally:
            await client.close()

        await self._log_request(
            provider=provider, action=ProviderAction.STATUS, is_success=True, started=started,
            request_summary=f"status order={order.provider_order_id}",
            response_snippet=str(result.raw)[:500], order_id=order.id, secrets=client.secrets(),
        )
        await self._record_success(provider)
        return await self._to_system_status(provider, result)

    async def _to_system_status(
        self, provider: Provider, result: ProviderStatusResult
    ) -> ProviderStatusResult:
        """تحويل مبلغ حالة الطلب (إن وُجد) إلى عملة النظام، أو إسقاطه إن تعذّر السعر."""
        if result.charge_units is None:
            return result
        system_code, _, _ = await self.settings.currency()
        return replace(
            result, charge_units=await self._convert_known(provider, result.charge_units), currency=system_code
        )

    async def fetch_multi_status(
        self, provider: Provider, orders: Sequence[Order]
    ) -> dict[str, ProviderStatusResult]:
        """
        الاستعلام عن حالة عدة طلبات لدى مزود واحد في **استدعاء واحد**.

        لماذا؟ فحص 20 طلباً فردياً = 20 طلب HTTP كل دقيقتين، وهو ما يستنزف حدود
        المزود ويبطئ الجولة. معيار Perfect Panel يدعم `orders=1,2,3` فنستفيد منه،
        والمزود الذي لا يدعمه يرجع تلقائياً للفحص الفردي (التطبيق الافتراضي في
        `ProviderClient.get_multi_status`).

        المخرجات: قاموس {رقم الطلب لدى المزود: نتيجة الحالة} — قد ينقص منه ما لم
                  يُجب عنه المزود، والمستدعي يتولى فحص الناقص فردياً.

        الاستثناءات: ProviderTemporaryError · ProviderPermanentError
        """
        provider_order_ids = [
            order.provider_order_id for order in orders if order.provider_order_id
        ]
        if not provider_order_ids:
            return {}

        client = self.client_for(provider)
        started = time.monotonic()
        try:
            results = await client.get_multi_status(provider_order_ids)
        except ProviderError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.STATUS, is_success=False, started=started,
                request_summary=f"multi-status count={len(provider_order_ids)}",
                response_snippet=str(exc), error_type=type(exc).__name__,
                secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        finally:
            await client.close()

        await self._log_request(
            provider=provider, action=ProviderAction.STATUS, is_success=True, started=started,
            request_summary=f"multi-status count={len(provider_order_ids)}",
            response_snippet=f"answered={len(results)}/{len(provider_order_ids)}",
            secrets=client.secrets(),
        )
        await self._record_success(provider)
        return {key: await self._to_system_status(provider, value) for key, value in results.items()}

    async def check_balance(self, provider: Provider) -> ProviderBalance:
        """
        قراءة رصيدنا لدى المزود وتحديثه في قاعدة البيانات مع لقطة تاريخية.

        يُرسل تنبيهاً للأدمن إذا نزل الرصيد تحت الحد المحدد.
        """
        client = self.client_for(provider)
        started = time.monotonic()
        try:
            balance = await client.get_balance()
        except ProviderError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.BALANCE, is_success=False, started=started,
                request_summary="balance", response_snippet=str(exc),
                error_type=type(exc).__name__, secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        finally:
            await client.close()

        reported_currency = (balance.currency or "").strip().upper()
        async with self.db.transaction() as tx:
            await self.repos.providers.update_balance(tx, provider.id, balance.amount_units)
            await self.repos.providers.add_balance_snapshot(
                tx, provider.id, balance.amount_units, currency=reported_currency or provider.currency
            )

        if reported_currency and reported_currency != (provider.currency or "").strip().upper():
            logger.warning(
                "المزود %s يُبلِّغ بعملة %s بينما المسجَّل %s", provider.id, reported_currency, provider.currency
            )
            title, message = templates.admin_provider_currency_mismatch(
                provider_name=provider.name, configured=provider.currency, reported=reported_currency
            )
            async with self.db.transaction() as tx:
                await self.notifications.notify_admins(
                    tx,
                    permission=Permission.PROVIDERS_UPDATE,
                    token=f"provider:{provider.id}:currency:{reported_currency}",
                    title=title,
                    message=message,
                )

        # عتبة التنبيه بعملة النظام ⇒ نقارن بمكافئ الرصيد وليس بالرقم الخام بعملة المزود
        threshold = provider.low_balance_threshold_units or await self.settings.get_int(
            "provider_low_balance_alert_units", 0
        )
        equivalent, problem = await self.rate_state(provider)
        if threshold and equivalent is not None:
            equivalent_units = self.to_system_units(balance.amount_units, equivalent)
            if equivalent_units < threshold:
                symbol = await self.settings.money_symbol()
                title, message = templates.admin_provider_low_balance(
                    provider_name=provider.name, balance_units=equivalent_units, symbol=symbol
                )
                async with self.db.transaction() as tx:
                    await self.notifications.notify_admins(
                        tx,
                        permission=Permission.PROVIDERS_VIEW,
                        token=f"provider:{provider.id}:low:{equivalent_units}",
                        title=title,
                        message=message,
                    )
        elif threshold:
            logger.warning("تعذّرت مقارنة رصيد المزود %s بالعتبة: %s", provider.id, problem)

        await self._log_request(
            provider=provider, action=ProviderAction.BALANCE, is_success=True, started=started,
            request_summary="balance", response_snippet=f"amount_units={balance.amount_units}",
            secrets=client.secrets(),
        )
        await self._record_success(provider)
        return balance

    async def fetch_services(self, actor: User, provider: Provider) -> list[ProviderServiceItem]:
        """
        جلب قائمة خدمات المزود (تمهيداً للاستيراد في المرحلة 5).

        الاستثناءات: PermissionDeniedError · ProviderError
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_SYNC, context=f"provider:{provider.id}"
        )
        rate = await self.system_rate(provider)      # قبل الاتصال: لا جلب بلا إمكانية تحويل
        client = self.client_for(provider)
        started = time.monotonic()
        try:
            items = await client.get_services()
        except ProviderError as exc:
            await self._log_request(
                provider=provider, action=ProviderAction.SERVICES, is_success=False, started=started,
                request_summary="services", response_snippet=str(exc),
                error_type=type(exc).__name__, secrets=client.secrets(),
            )
            await self._record_failure(provider, str(exc))
            raise
        finally:
            await client.close()

        await self._log_request(
            provider=provider, action=ProviderAction.SERVICES, is_success=True, started=started,
            request_summary="services", response_snippet=f"count={len(items)}", secrets=client.secrets(),
        )
        await self._record_success(provider)

        # أسعار المزود بعملته ⇒ عملة النظام (مرة واحدة هنا، فيبقى الكتالوج كله بعملة واحدة)
        system_code, _, _ = await self.settings.currency()
        return [
            replace(item, rate_per_1000_units=self.to_system_units(item.rate_per_1000_units, rate), currency=system_code)
            for item in items
        ]

    async def test_connection(self, actor: User, provider_id: int) -> tuple[bool, str]:
        """
        اختبار اتصال المزود من لوحة الأدمن.

        المخرجات: (نجح؟، رسالة عربية للعرض) — لا تحتوي الرسالة أي سر.
        """
        await self.auth.require_permission(
            actor, Permission.PROVIDERS_TEST, context=f"provider:{provider_id}"
        )
        provider = await self.repos.providers.get(provider_id)
        if provider is None:
            return False, "المزود غير موجود."
        try:
            balance = await self.check_balance(provider)
        except ProviderTemporaryError as exc:
            return False, f"فشل مؤقت في الاتصال: {exc.user_message}"
        except ProviderError as exc:
            return False, f"فشل الاتصال: {exc.user_message}"
        symbol = provider.currency or "USD"
        text = f"الاتصال ناجح ✅ — الرصيد: {balance.amount_units / 10_000:.4f} {symbol}"
        rate, problem = await self.rate_state(provider)
        if rate is None:
            return True, f"{text}\n⚠️ {problem}"
        if rate != 1:
            system_code, _, _ = await self.settings.currency()
            text += f" (≈ {self.to_system_units(balance.amount_units, rate) / 10_000:.2f} {system_code})"
        return True, text

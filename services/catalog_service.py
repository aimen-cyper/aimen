"""
خدمة الكتالوج: التصنيفات والخدمات وحساب سعر الطلب قبل تأكيده.

مبدأ مهم: سعر الخدمة يُقرأ من قاعدة البيانات في لحظة الطلب ويُخزَّن كلقطة داخل الطلب،
فتغيير السعر لاحقاً لا يؤثر على الطلبات القديمة ولا على المحاسبة.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from core.constants import PAGE_SIZE
from core.exceptions import DuplicateOperationError, ServiceNotAvailableError, ValidationError
from core.money import format_units, order_cost_units, unit_price_units
from core.permissions import Permission
from database.database import Database
from database.models import Service, ServiceCategory, User
from database.repositories import Repositories
from integrations.providers.base import SUPPORTED_SERVICE_TYPES, ProviderServiceItem
from services.audit_service import AuditService
from services.auth_service import AuthService
from services.settings_service import SettingsService
from utils.logging_setup import get_logger
from utils.validators import clean_text, validate_quantity

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Quote:
    """تسعيرة طلب قبل تأكيده."""

    service: Service
    quantity: int
    cost_units: int
    symbol: str
    decimals: int

    @property
    def cost_text(self) -> str:
        """التكلفة بصيغة نصية جاهزة للعرض."""
        return format_units(self.cost_units, symbol=self.symbol, decimals=self.decimals)

    @property
    def price_per_1000_text(self) -> str:
        """سعر الألف بصيغة نصية."""
        return format_units(
            self.service.price_per_1000_units, symbol=self.symbol, decimals=self.decimals
        )


#: أدنى وأقصى هامش ربح مقبول عند الاستيراد (بالمئة) — حارس ضد خطأ إدخال يبيع بخسارة
MIN_MARGIN_PERCENT: int = 0
MAX_MARGIN_PERCENT: int = 1000


@dataclass(frozen=True, slots=True)
class SyncReport:
    """
    نتيجة مزامنة أسعار وحدود خدمات مزود مع قائمته الحالية.

    الحقول:
        checked        : عدد خدماتنا المرتبطة بهذا المزود.
        updated        : كم خدمة تغيّرت تكلفتها أو حدودها فعلياً.
        unchanged      : كم خدمة كانت مطابقة أصلاً.
        missing        : خدمات لم تعد موجودة لدى المزود (تُعرض للأدمن ولا تُحذف).
        negative_margin: خدمات صار سعر بيعها أقل من تكلفتها بعد المزامنة.
        provider_items : عدد الخدمات التي أرسلها المزود.
        unsupported    : خدمات مستوردة صار نوعها لدى المزود غير مدعوم فعُطِّلت (لا تُباع حتى يراجعها الأدمن).
    """

    checked: int
    updated: int
    unchanged: int
    missing: list[Service]
    negative_margin: list[Service]
    provider_items: int
    unsupported: list[Service] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Page:
    """صفحة من قائمة (خدمات أو غيرها) مع معلومات التنقل."""

    items: list
    page: int
    total_items: int
    page_size: int

    @property
    def total_pages(self) -> int:
        """عدد الصفحات الكلي (واحد على الأقل)."""
        return max(1, -(-self.total_items // self.page_size))

    @property
    def has_next(self) -> bool:
        """هل توجد صفحة تالية؟"""
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        """هل توجد صفحة سابقة؟"""
        return self.page > 1


class CatalogService:
    """تصفح الخدمات وحساب التسعيرة وإدارة الكتالوج من لوحة الأدمن."""

    def __init__(
        self,
        db: Database,
        repos: Repositories,
        settings: SettingsService,
        audit: AuditService,
        auth: AuthService,
    ) -> None:
        """تهيئة الخدمة."""
        self.db = db
        self.repos = repos
        self.settings = settings
        self.audit = audit
        self.auth = auth

    # ------------------------------- التصفح ------------------------------- #

    async def categories(self) -> list[ServiceCategory]:
        """التصنيفات التي تحتوي خدمات متاحة فعلاً (لا نعرض تصنيفاً فارغاً للمستخدم)."""
        return await self.repos.categories.list_with_available_services()

    async def services_page(self, category_id: int, *, page: int = 1, page_size: int = PAGE_SIZE) -> Page:
        """صفحة من خدمات تصنيف معيّن."""
        page = max(1, page)
        total = await self.repos.services.count_by_category(category_id, only_available=True)
        items = await self.repos.services.list_by_category(
            category_id, only_available=True, limit=page_size, offset=(page - 1) * page_size
        )
        return Page(items=list(items), page=page, total_items=total, page_size=page_size)

    async def search(self, text: str, *, limit: int = 10) -> list[Service]:
        """بحث نصي في أسماء الخدمات المتاحة."""
        cleaned = clean_text(text, field="نص البحث", max_length=60, min_length=2)
        return await self.repos.services.search(cleaned, limit=limit)

    async def get_available_service(self, service_id: int) -> Service:
        """
        جلب خدمة متاحة للطلب.

        الاستثناءات:
            ServiceNotAvailableError : الخدمة غير موجودة أو معطّلة أو مؤرشفة
                                       أو مزودها متوقف.
        """
        service = await self.repos.services.get_available(service_id)
        if service is None:
            raise ServiceNotAvailableError(f"الخدمة {service_id} غير متاحة للطلب")
        return service

    async def quote(self, service: Service, raw_quantity: str | int) -> Quote:
        """
        حساب تكلفة الطلب بعد التحقق من الكمية.

        الاستثناءات:
            QuantityOutOfRangeError : الكمية خارج حدود الخدمة.
        """
        quantity = validate_quantity(
            raw_quantity, min_quantity=service.min_quantity, max_quantity=service.max_quantity
        )
        cost = order_cost_units(service.price_per_1000_units, quantity)
        _, symbol, decimals = await self.settings.currency()
        return Quote(
            service=service, quantity=quantity, cost_units=cost, symbol=symbol, decimals=decimals
        )

    async def unit_price(self, service: Service) -> str:
        """سعر الوحدة الواحدة (للعرض التفصيلي)."""
        _, symbol, decimals = await self.settings.currency()
        price = unit_price_units(service.price_per_1000_units)
        return f"{price:.6f} {symbol}".strip()

    # ------------------------ قراءة من لوحة الأدمن ------------------------ #

    async def admin_categories(self, actor: User) -> list[ServiceCategory]:
        """
        كل التصنيفات كما هي في قاعدة البيانات (حتى الفارغة والمعطّلة).

        تختلف عن `categories()` الموجّهة للعميل: الأدمن يحتاج رؤية ما لا يراه العميل
        ليستطيع إصلاحه.

        الاستثناءات: PermissionDeniedError
        """
        await self.auth.require_permission(actor, Permission.SERVICES_VIEW, context="catalog")
        return await self.repos.categories.list_all(active_only=False)

    async def admin_services_page(
        self, actor: User, category_id: int, *, page: int = 1, page_size: int = PAGE_SIZE
    ) -> Page:
        """
        صفحة خدمات تصنيف في لوحة الأدمن — تشمل المعطّلة (لا تشمل المؤرشفة).

        الاستثناءات: PermissionDeniedError
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_VIEW, context=f"category:{category_id}"
        )
        page = max(1, page)
        total = await self.repos.services.count_by_category(category_id, only_available=False)
        items = await self.repos.services.list_by_category(
            category_id, only_available=False, limit=page_size, offset=(page - 1) * page_size
        )
        return Page(items=list(items), page=page, total_items=total, page_size=page_size)

    async def admin_get_service(self, actor: User, service_id: int) -> Service:
        """
        جلب خدمة بأي حالة لعرض بطاقتها الإدارية.

        الاستثناءات: PermissionDeniedError · ServiceNotAvailableError (غير موجودة)
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_VIEW, context=f"service:{service_id}"
        )
        service = await self.repos.services.get(service_id)
        if service is None:
            raise ServiceNotAvailableError(
                f"الخدمة {service_id} غير موجودة", user_message="❌ الخدمة غير موجودة."
            )
        return service

    async def active_services_count(self) -> int:
        """عدد الخدمات المفعّلة (يُعرض في رأس شاشة الكتالوج)."""
        return await self.repos.services.count_active()

    async def admin_get_category(self, actor: User, category_id: int) -> ServiceCategory:
        """
        جلب تصنيف بالمعرف لعرض عنوانه فوق قائمة خدماته.

        الاستثناءات: PermissionDeniedError · ServiceNotAvailableError (غير موجود)
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_VIEW, context=f"category:{category_id}"
        )
        category = await self.repos.categories.get(category_id)
        if category is None:
            raise ServiceNotAvailableError(
                f"التصنيف {category_id} غير موجود", user_message="❌ التصنيف غير موجود."
            )
        return category

    # --------------------------- إدارة الكتالوج --------------------------- #

    async def create_category(self, actor: User, *, name_ar: str, emoji: str = "📦", sort_order: int = 100) -> ServiceCategory:
        """إضافة تصنيف جديد (صلاحية services.create)."""
        await self.auth.require_permission(actor, Permission.SERVICES_CREATE, context="category.create")
        clean_name = clean_text(name_ar, field="اسم التصنيف", max_length=60, min_length=2)
        async with self.db.transaction() as tx:
            category = await self.repos.categories.create(
                tx, name_ar=clean_name, emoji=emoji, sort_order=sort_order
            )
            await self.audit.log(
                tx,
                action="services.category.create",
                admin_id=actor.id,
                target_type="category",
                target_id=category.id,
                after={"name_ar": clean_name},
            )
        return category

    async def create_service(
        self,
        actor: User,
        *,
        category_id: int,
        provider_id: int,
        provider_service_id: str,
        name: str,
        price_per_1000_units: int,
        min_quantity: int,
        max_quantity: int,
        provider_price_per_1000_units: int | None = None,
        description: str | None = None,
        link_pattern: str | None = None,
        service_type: str = "default",
    ) -> Service:
        """
        إضافة خدمة جديدة للكتالوج.

        الاستثناءات: PermissionDeniedError · ValidationError · DuplicateOperationError
        (خدمة المزود نفسها موجودة — تُفحص داخل المعاملة وتحميها قيود فريدة في القاعدة ضد السباق).
        """
        await self.auth.require_permission(actor, Permission.SERVICES_CREATE, context="service.create")
        clean_name = clean_text(name, field="اسم الخدمة", max_length=120, min_length=3)
        self._ensure_supported_type(service_type, name=clean_name)
        duplicate = DuplicateOperationError(
            f"خدمة المزود {provider_service_id} مستوردة مسبقاً للمزود {provider_id}",
            user_message="⚠️ هذه الخدمة مضافة مسبقاً لهذا المزود.",
        )
        try:
            async with self.db.transaction() as tx:
                # الفحص داخل المعاملة نفسها (BEGIN IMMEDIATE) فلا يتسلل مكرر عند ضغطتين متزامنتين
                if await tx.fetchval(
                    "SELECT 1 FROM services WHERE provider_id = ? AND provider_service_id = ?",
                    (provider_id, str(provider_service_id)),
                ):
                    raise duplicate
                service = await self.repos.services.create(
                    tx,
                    category_id=category_id,
                    provider_id=provider_id,
                    provider_service_id=str(provider_service_id),
                    name=clean_name,
                    price_per_1000_units=price_per_1000_units,
                    min_quantity=min_quantity,
                    max_quantity=max_quantity,
                    provider_price_per_1000_units=provider_price_per_1000_units,
                    description=description,
                    link_pattern=link_pattern,
                    service_type=service_type,
                )
                await self.audit.log(
                    tx,
                    action="services.create",
                    admin_id=actor.id,
                    target_type="service",
                    target_id=service.id,
                    after={
                        "name": clean_name,
                        "price_per_1000_units": price_per_1000_units,
                        "provider_id": provider_id,
                    },
                )
        except sqlite3.IntegrityError as exc:
            if "services.provider_id" in str(exc) or "uq_services_provider_service" in str(exc):
                raise duplicate from exc          # سباق نادر تجاوز الفحص: القيد الفريد حسم الأمر
            raise
        logger.info("أضاف الأدمن %s الخدمة %s (%s)", actor.id, service.id, clean_name)
        return service

    @staticmethod
    def _ensure_supported_type(service_type: str, *, name: str) -> None:
        """
        رفض أنواع الخدمات التي لا يدعمها مسار الطلب (Package/Custom Comments/Subscriptions…).

        السبب: هذه الأنواع تحتاج حقولاً إضافية (تعليقات، اسم مستخدم، مدة…) فلو استُوردت لأُرسل طلب ناقص
        الحقول للمزود وخُصم مال العميل ثم فشل أو نُفِّذ خطأً.
        """
        if service_type not in SUPPORTED_SERVICE_TYPES:
            raise ValidationError(
                f"نوع الخدمة غير مدعوم: {service_type} ({name})",
                user_message=(
                    f"⚠️ نوع هذه الخدمة («{service_type}») غير مدعوم حالياً: يحتاج حقولاً إضافية "
                    "لا يجمعها مسار الطلب. المدعوم: الخدمات العادية (default) فقط."
                ),
            )

    async def change_price(self, actor: User, service_id: int, new_price_units: int) -> bool:
        """
        تغيير سعر خدمة مع حفظ السعر القديم في السجل التاريخي.

        الاستثناءات: PermissionDeniedError
        """
        await self.auth.require_permission(actor, Permission.SERVICES_PRICE, context=f"service:{service_id}")
        service = await self.repos.services.get(service_id)
        if service is None:
            return False
        async with self.db.transaction() as tx:
            changed = await self.repos.services.change_price(
                tx, service_id, new_price_units=new_price_units, changed_by=actor.id, source="admin"
            )
            if changed:
                await self.audit.log(
                    tx,
                    action="services.price",
                    admin_id=actor.id,
                    target_type="service",
                    target_id=service_id,
                    before={"price_per_1000_units": service.price_per_1000_units},
                    after={"price_per_1000_units": new_price_units},
                )
        return changed

    async def set_service_active(self, actor: User, service_id: int, active: bool) -> bool:
        """تفعيل أو تعطيل خدمة (لا حذف — الطلبات القديمة تبقى سليمة)."""
        await self.auth.require_permission(actor, Permission.SERVICES_ARCHIVE, context=f"service:{service_id}")
        async with self.db.transaction() as tx:
            changed = await self.repos.services.set_active(tx, service_id, active)
            if changed:
                await self.audit.log(
                    tx,
                    action="services.update",
                    admin_id=actor.id,
                    target_type="service",
                    target_id=service_id,
                    after={"is_active": active},
                )
        return changed

    async def archive_service(self, actor: User, service_id: int) -> bool:
        """أرشفة خدمة (إخفاؤها نهائياً مع الاحتفاظ ببياناتها التاريخية)."""
        await self.auth.require_permission(actor, Permission.SERVICES_ARCHIVE, context=f"service:{service_id}")
        async with self.db.transaction() as tx:
            changed = await self.repos.services.archive(tx, service_id)
            if changed:
                await self.audit.log(
                    tx,
                    action="services.archive",
                    admin_id=actor.id,
                    target_type="service",
                    target_id=service_id,
                    after={"is_archived": True},
                )
        return changed

    async def negative_margin_report(self, actor: User) -> list[Service]:
        """
        الخدمات التي أصبح سعر بيعها أقل من تكلفتها لدى المزود.

        الاستثناءات: PermissionDeniedError (تحتاج صلاحية عرض الخدمات).
        """
        await self.auth.require_permission(actor, Permission.SERVICES_VIEW, context="services.margin")
        return await self.repos.services.negative_margin_services()

    # ===================================================================== #
    #                الاستيراد والمزامنة من المزود (المرحلة 5)               #
    # ===================================================================== #

    @staticmethod
    def price_with_margin(cost_per_1000_units: int, margin_percent: int) -> int:
        """
        حساب سعر البيع من تكلفة المزود وهامش ربح بالمئة.

        المدخلات:
            cost_per_1000_units : تكلفة الألف لدى المزود بوحدات المال الصحيحة.
            margin_percent      : الهامش بالمئة (50 ⇐ السعر = التكلفة × 1.5).

        المخرجات: سعر الألف بوحدات صحيحة (تقريب لأقرب وحدة، والحد الأدنى وحدة واحدة
                  حتى لا تنتج خدمة بسعر صفر عند تكلفة ضئيلة).

        الاستثناءات: ValidationError عند هامش خارج الحدود أو تكلفة سالبة.
        """
        if margin_percent < MIN_MARGIN_PERCENT or margin_percent > MAX_MARGIN_PERCENT:
            raise ValidationError(
                f"هامش ربح خارج الحدود: {margin_percent}",
                user_message=(
                    f"❌ هامش الربح يجب أن يكون بين {MIN_MARGIN_PERCENT} و{MAX_MARGIN_PERCENT}%."
                ),
            )
        if cost_per_1000_units < 0:
            raise ValidationError("تكلفة سالبة", user_message="❌ تكلفة المزود غير صالحة.")

        # حساب صحيح بالكامل (لا عوائم): (التكلفة × (100 + الهامش) + 50) ÷ 100
        price = (cost_per_1000_units * (100 + margin_percent) + 50) // 100
        return max(price, 1)

    async def import_provider_service(
        self,
        actor: User,
        *,
        provider_id: int,
        item: ProviderServiceItem,
        category_id: int,
        margin_percent: int,
    ) -> Service:
        """
        استيراد خدمة من قائمة المزود إلى الكتالوج بسعر = التكلفة + هامش.

        تمنع الدالة استيراد نفس خدمة المزود مرتين (مصدر شائع لخدمات مكرّرة في
        الواجهة)، وتحفظ تكلفة المزود مع الخدمة حتى يُحسب الهامش لاحقاً.

        المخرجات: الخدمة بعد إنشائها.

        الاستثناءات:
            PermissionDeniedError   : بلا صلاحية `services.create`.
            ValidationError         : تصنيف غير موجود أو هامش غير صالح.
            DuplicateOperationError : الخدمة مستوردة من قبل.
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_CREATE, context=f"provider:{provider_id}"
        )
        category = await self.repos.categories.get(category_id)
        if category is None:
            raise ValidationError(
                f"تصنيف غير موجود: {category_id}", user_message="❌ التصنيف غير موجود."
            )

        existing = await self.repos.services.find_by_provider_service(
            provider_id, item.provider_service_id
        )
        if existing is not None:
            raise DuplicateOperationError(
                f"خدمة المزود {item.provider_service_id} مستوردة مسبقاً (#{existing.id})",
                user_message=f"⚠️ هذه الخدمة مستوردة مسبقاً باسم: {existing.name}",
            )

        self._ensure_supported_type(item.service_type, name=item.name)
        price = self.price_with_margin(item.rate_per_1000_units, margin_percent)
        service = await self.create_service(
            actor,
            category_id=category_id,
            provider_id=provider_id,
            provider_service_id=item.provider_service_id,
            name=item.name,
            price_per_1000_units=price,
            min_quantity=item.min_quantity,
            max_quantity=item.max_quantity,
            provider_price_per_1000_units=item.rate_per_1000_units,
            service_type=item.service_type,
        )
        logger.info(
            "استورد الأدمن %s الخدمة %s من المزود %s بهامش %s%%",
            actor.id, service.id, provider_id, margin_percent,
        )
        return service

    async def sync_provider_services(
        self, actor: User, *, provider_id: int, items: list[ProviderServiceItem]
    ) -> SyncReport:
        """
        مزامنة خدماتنا المرتبطة بمزود مع قائمته الحالية.

        ما تفعله: تحديث **تكلفة المزود** وحدود الكمية لكل خدمة عندنا.
        ما لا تفعله عمداً:
            • لا تغيّر سعر البيع (قرار تجاري بيد الأدمن).
            • لا تحذف ولا توقف أي خدمة اختفت من المزود — تُدرجها في `missing`
              ليقرر الأدمن، لأن الحذف الآلي قد يُسقط خدمة رائجة بسبب عطل مؤقت.

        المخرجات: `SyncReport` بكل الأرقام والقوائم اللازمة لعرض نتيجة واضحة.

        الاستثناءات: PermissionDeniedError (تحتاج `services.sync`).
        """
        await self.auth.require_permission(
            actor, Permission.SERVICES_SYNC, context=f"provider:{provider_id}"
        )
        by_provider_id = {item.provider_service_id: item for item in items}
        ours = await self.repos.services.list_by_provider(provider_id, limit=1000)

        updated: list[tuple[Service, dict]] = []
        missing: list[Service] = []
        unsupported: list[Service] = []
        unchanged = 0

        for service in ours:
            item = by_provider_id.get(str(service.provider_service_id))
            if item is None:
                missing.append(service)
                continue
            if item.service_type not in SUPPORTED_SERVICE_TYPES:
                # النوع تغيّر لدى المزود إلى ما لا ندعمه: لا نعدّل سعره ولا نتركه يُباع
                if service.is_active:
                    unsupported.append(service)
                continue

            changes: dict[str, int] = {}
            if item.rate_per_1000_units != (service.provider_price_per_1000_units or 0):
                changes["provider_price_per_1000_units"] = item.rate_per_1000_units
            if item.min_quantity != service.min_quantity:
                changes["min_quantity"] = item.min_quantity
            if item.max_quantity != service.max_quantity:
                changes["max_quantity"] = item.max_quantity

            if changes:
                updated.append((service, changes))
            else:
                unchanged += 1

        if unsupported:
            async with self.db.transaction() as tx:
                for service in unsupported:
                    await self.repos.services.set_active(tx, service.id, False)
                await self.audit.log(
                    tx,
                    action="services.sync_disable_unsupported",
                    admin_id=actor.id,
                    target_type="provider",
                    target_id=provider_id,
                    after={"disabled_services": [service.id for service in unsupported]},
                )

        if updated:
            async with self.db.transaction() as tx:
                for service, changes in updated:
                    await self.repos.services.update_fields(tx, service.id, **changes)
                await self.audit.log(
                    tx,
                    action="services.sync",
                    admin_id=actor.id,
                    target_type="provider",
                    target_id=provider_id,
                    before={"updated_services": [service.id for service, _ in updated]},
                    after={
                        "provider_items": len(items),
                        "updated": len(updated),
                        "missing": len(missing),
                    },
                )

        # الخدمات التي صارت تُباع بأقل من تكلفتها بعد تحديث التكلفة
        refreshed = await self.repos.services.list_by_provider(provider_id, limit=1000)
        negative = [
            service
            for service in refreshed
            if service.provider_price_per_1000_units
            and service.price_per_1000_units < service.provider_price_per_1000_units
        ]

        logger.info(
            "مزامنة المزود %s: %s خدمة لدى المزود، حُدّثت %s، مفقودة %s",
            provider_id, len(items), len(updated), len(missing),
        )
        return SyncReport(
            checked=len(ours),
            updated=len(updated),
            unchanged=unchanged,
            missing=missing,
            negative_margin=negative,
            provider_items=len(items),
            unsupported=unsupported,
        )

    async def services_of_provider(self, actor: User, provider_id: int) -> list[Service]:
        """قائمة خدماتنا المرتبطة بمزود معيّن (لعرضها في بطاقة المزود)."""
        await self.auth.require_permission(
            actor, Permission.SERVICES_VIEW, context=f"provider:{provider_id}"
        )
        return await self.repos.services.list_by_provider(provider_id, limit=1000)

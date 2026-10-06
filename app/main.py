"""FastAPI application: HTTP API for dataset derivative permission checks."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings, settings as default_settings
from .database import Database
from .schemas import (
    AdmissionDeniedResponse,
    AdmissionEntryResult,
    AdmissionRecord,
    BasisEntry,
    BlockerEntry,
    ChangeRevocationRequest,
    CreateResponse,
    DeliveryAdmissionRequest,
    DeliveryAdmissionResponse,
    ErrorDetail,
    ImpactedVersion,
    KeyRotationResponse,
    LicenseChangeBatchRequest,
    LicenseChangeBatchResponse,
    LicenseChangeBatchResult,
    LicenseChangeRequest,
    LicenseChangeResponse,
    PermissionResponse,
    PermissionSegment,
    PermissionTimelineResponse,
    PreviewChangeRequest,
    PreviewChangeStub,
    PreviewQuery,
    PreviewResponse,
    ProofExportResponse,
    ProofPackage,
    PublicKeyListResponse,
    PublicKeyResponse,
    ReleasePublishRequest,
    ReleasePublishResponse,
    ReleaseResult,
    ReleaseSlotResult,
    RevocationReceipt,
    RevocationResponse,
    SlotHead,
    SlotPermissionResponse,
    SlotResolution,
    SubscriptionCreateRequest,
    SubscriptionCreateResponse,
    SubscriptionEventPage,
    SubscriptionResponse,
    VersionCreateRequest,
    VersionResponse,
    VoucherDecision,
    VoucherIssueRequest,
    VoucherResponse,
)
from .seed import seed_demo_data
from .service import (
    AdmissionDeniedError,
    InvalidTimeError,
    LicenseService,
    NotFoundError,
    ServiceError,
)
from .signing import KeyStore, SigningKeyUnavailableError

logger = logging.getLogger("license_service")


def _parse_aware_param(raw: str, name: str) -> datetime:
    """Parse an ISO 8601 query parameter that must carry a timezone."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidTimeError(
            f"{name} 参数必须是带时区的 ISO 8601 时刻，例如 2026-07-01T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        raise InvalidTimeError(f"{name} 参数必须带时区信息（例如结尾的 Z 或 +00:00）")
    return parsed


class AppState:
    def __init__(
        self, db: Database, service: LicenseService, key_store: KeyStore
    ) -> None:
        self.db = db
        self.service = service
        self.key_store = key_store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        db = Database(settings.db_path)
        if settings.init_db:
            db.init_schema()
            logger.info("数据库 schema 已就绪：%s", settings.db_path)
        if settings.seed_demo and settings.init_db:
            seed_demo_data(db)
        key_store = KeyStore(settings.signing_keys_dir)
        if settings.init_signing_key:
            # Missing keys bootstrap a genesis key here; an unwritable directory
            # does not stop startup — only proof exports fail explicitly (503).
            try:
                active = key_store.ensure_active_key()
                logger.info("签名密钥已就绪：%s（目录 %s）", active.key_id, settings.signing_keys_dir)
            except SigningKeyUnavailableError as exc:
                logger.error("签名密钥初始化失败，证明包导出将不可用：%s", exc.message)
        if settings.echo:
            logger.info(
                "配置 DB_PATH=%s SIGNING_KEYS_DIR=%s HOST=%s PORT=%s INIT_DB=%s SEED_DEMO=%s",
                settings.db_path,
                settings.signing_keys_dir,
                settings.host,
                settings.port,
                settings.init_db,
                settings.seed_demo,
            )
        app.state.app_state = AppState(db, LicenseService(db, key_store), key_store)
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="数据集派生许可判定服务",
        version="1.0.0",
        description=(
            "登记数据集版本与其固定父版本引用、登记按 UTC 时刻生效的允许用途变更，"
            "并基于自身及全部祖先的最近一次有效变更的用途交集判定可否使用。"
        ),
        lifespan=lifespan,
    )

    def service_from(request: Request) -> LicenseService:
        return request.app.state.app_state.service  # type: ignore[no-any-return]

    # -- error handling ---------------------------------------------------

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        detail = ErrorDetail(error=exc.code, message=exc.message, details=exc.details)
        return JSONResponse(status_code=exc.status_code, content=detail.model_dump())

    @app.exception_handler(AdmissionDeniedError)
    async def handle_admission_denied(
        _: Request, exc: AdmissionDeniedError
    ) -> JSONResponse:
        # Registered on the concrete subclass so it wins over the ServiceError
        # handler: a 403 business denial additionally carries one first-failure
        # report per failed entry. It is a read-only outcome — no admission row
        # exists for it, so retrying the same delivery id re-evaluates.
        body = AdmissionDeniedResponse(
            error=exc.code,
            message=exc.message,
            details=exc.details,
            failures=exc.failures,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
        detail = ErrorDetail(
            error="invalid_time",
            message="无法解析带时区的 UTC 时刻：" + str(exc),
        )
        return JSONResponse(status_code=422, content=detail.model_dump())

    @app.exception_handler(SigningKeyUnavailableError)
    async def handle_signing_key_unavailable(
        _: Request, exc: SigningKeyUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    # -- health & meta ----------------------------------------------------

    @app.get("/health", tags=["meta"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- versions ---------------------------------------------------------

    @app.post(
        "/versions",
        response_model=CreateResponse,
        status_code=201,
        tags=["versions"],
        summary="登记数据集版本并固定父版本引用（不可修改）",
    )
    def create_version(payload: VersionCreateRequest, request: Request) -> CreateResponse:
        version = service_from(request).register_version(
            version_id=payload.version_id,
            parent_ids=payload.parent_ids,
            note=payload.note,
        )
        return CreateResponse(replayed=False, version=VersionResponse(**version))

    @app.get(
        "/versions",
        response_model=list[VersionResponse],
        tags=["versions"],
        summary="列出所有版本",
    )
    def list_versions(request: Request) -> list[VersionResponse]:
        return [VersionResponse(**v) for v in service_from(request).list_versions()]

    @app.get(
        "/versions/{version_id}",
        response_model=VersionResponse,
        tags=["versions"],
        summary="查询版本详情（含父版本与许可变更）",
    )
    def get_version(version_id: str, request: Request) -> VersionResponse:
        return VersionResponse(**service_from(request).get_version(version_id))

    # -- version-slot releases (unified publish & historical replay) ------

    @app.post(
        "/releases",
        response_model=ReleasePublishResponse,
        status_code=201,
        tags=["releases"],
        summary="按版本槽统一发布多个已登记版本（整次原子、幂等、修订不可跳过）",
        responses={
            200: {"description": "相同发布标识与规范化后相同内容重试，返回首次冻结结果"},
            201: {"description": "首次发布：各槽修订推进与不可变发布记录在同一事务落库"},
            404: {"description": "清单中存在未登记的目标版本，整次发布拒绝"},
            409: {"description": "槽名重复/首次预期修订非 0/预期修订不匹配/目标非严格后代/生效时刻不晚于上次/发布标识复用内容不同"},
            422: {"description": "槽位清单为空、槽名为空白或生效时刻无时区"},
        },
    )
    def publish_release(
        payload: ReleasePublishRequest, request: Request
    ) -> JSONResponse | ReleasePublishResponse:
        result, replayed = service_from(request).publish_release(
            release_id=payload.release_id,
            effective_at=payload.effective_at,
            slots=[item.model_dump() for item in payload.slots],
        )
        body = ReleasePublishResponse(
            replayed=replayed,
            release=ReleaseResult(
                release_id=result["release_id"],
                effective_at=result["effective_at"],
                created_at=result["created_at"],
                slot_count=result["slot_count"],
                slots=[ReleaseSlotResult(**entry) for entry in result["slots"]],
            ),
        )
        if replayed:
            # 200 OK makes an idempotent retry distinguishable from the first
            # publish; no slot revision is advanced on a replay.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/releases",
        response_model=list[ReleaseResult],
        tags=["releases"],
        summary="列出全部不可变发布记录（按首次发布时间排序）",
    )
    def list_releases(request: Request) -> list[ReleaseResult]:
        return [
            ReleaseResult(
                release_id=result["release_id"],
                effective_at=result["effective_at"],
                created_at=result["created_at"],
                slot_count=result["slot_count"],
                slots=[ReleaseSlotResult(**entry) for entry in result["slots"]],
            )
            for result in service_from(request).list_releases()
        ]

    @app.get(
        "/releases/{release_id}",
        response_model=ReleaseResult,
        tags=["releases"],
        summary="按发布标识取回首次冻结的发布记录（历史不可改写）",
        responses={404: {"description": "发布不存在"}},
    )
    def get_release(release_id: str, request: Request) -> ReleaseResult:
        result = service_from(request).get_release(release_id)
        return ReleaseResult(
            release_id=result["release_id"],
            effective_at=result["effective_at"],
            created_at=result["created_at"],
            slot_count=result["slot_count"],
            slots=[ReleaseSlotResult(**entry) for entry in result["slots"]],
        )

    @app.get(
        "/slots",
        response_model=list[SlotHead],
        tags=["releases"],
        summary="列出全部版本槽当前最新发布（修订指针）",
    )
    def list_slot_heads(request: Request) -> list[SlotHead]:
        return [SlotHead(**row) for row in service_from(request).list_slot_heads()]

    @app.get(
        "/slots/by-name/{slot}",
        response_model=SlotHead,
        tags=["releases"],
        summary="按槽名查询当前最新发布；该槽从未发布返回 404",
        responses={404: {"description": "槽位从未发布"}},
    )
    def get_slot_head(slot: str, request: Request) -> SlotHead:
        return SlotHead(**service_from(request).get_slot_head(slot))

    @app.get(
        "/slot-resolutions/{slot}",
        response_model=SlotResolution,
        tags=["releases"],
        summary="按槽名与带时区时刻解析当时最近的已发布目标版本",
        responses={
            404: {
                "description": "槽位从未发布（slot_never_published），或该时刻之前尚无生效发布（no_release_effective_at_or_before）"
            },
            422: {"description": "查询时刻缺少时区或无法解析"},
        },
    )
    def resolve_slot(
        slot: str,
        request: Request,
        at: str = Query(..., description="带时区的历史时刻，如 2026-07-01T00:00:00Z"),
    ) -> SlotResolution:
        parsed = _parse_aware_param(at, "at")
        result = service_from(request).resolve_slot(slot, parsed)
        return SlotResolution(**result)

    @app.get(
        "/slot-permissions/{slot}",
        response_model=SlotPermissionResponse,
        tags=["releases"],
        summary="按槽与带时区时刻解析历史目标版本，并用现有规则判定该时刻指定用途",
        responses={
            404: {"description": "槽位在该时刻尚未发布（明确报错，不猜测版本）"},
            422: {"description": "查询时刻缺少时区或无法解析"},
        },
    )
    def check_slot_permission(
        slot: str,
        request: Request,
        use: str = Query(..., description="拟使用的用途（去首尾空格后判定）"),
        at: str = Query(..., description="带时区的历史时刻，如 2026-07-01T00:00:00Z"),
    ) -> SlotPermissionResponse:
        parsed = _parse_aware_param(at, "at")
        result = service_from(request).check_slot_permission(slot, use, parsed)
        return SlotPermissionResponse(
            slot=result["resolution"]["slot"],
            use=result["use"],
            at=result["at"],
            resolution=SlotResolution(**result["resolution"]),
            version_id=result["version_id"],
            allowed=result["allowed"],
            closure=result["closure"],
            basis=[BasisEntry(**b) for b in result["basis"]],
            blockers=[BlockerEntry(**b) for b in result["blockers"]],
            allowed_uses_intersection=result["allowed_uses_intersection"],
        )

    # -- delivery admissions (whole-window immutable manifests) -----------

    @app.post(
        "/delivery-admissions",
        response_model=DeliveryAdmissionResponse,
        status_code=201,
        tags=["delivery-admissions"],
        summary="判定整份交付清单在整个使用窗口内是否持续获准，并原子固化不可变准入记录",
        responses={
            200: {"description": "同交付号与规范化后相同的有序输入重试，返回首次冻结的准入记录"},
            201: {"description": "全部条目全窗口允许：各条分段判定与准入记录在同一事务固化"},
            403: {
                "description": (
                    "清单未在整个窗口内获准：返回每条未通过条目的首个失败时段与阻断原因，"
                    "槽尚未发布也算失败；不留任何准入记录"
                )
            },
            404: {"description": "条目引用了未知版本，整单拒绝且不落库"},
            409: {"description": "交付号复用到不同窗口或不同清单内容"},
            422: {"description": "条目为空、时刻无时区、空/倒置窗口、空白用途或空白引用"},
        },
    )
    def submit_delivery_admission(
        payload: DeliveryAdmissionRequest, request: Request
    ) -> JSONResponse | DeliveryAdmissionResponse:
        result, replayed = service_from(request).submit_delivery_admission(
            delivery_id=payload.delivery_id,
            start=payload.start,
            end=payload.end,
            entries=[item.model_dump() for item in payload.entries],
        )
        body = DeliveryAdmissionResponse(
            replayed=replayed,
            admission=AdmissionRecord(
                delivery_id=result["delivery_id"],
                created_at=result["created_at"],
                start=result["start"],
                end=result["end"],
                entry_count=result["entry_count"],
                entries=[AdmissionEntryResult(**entry) for entry in result["entries"]],
            ),
        )
        if replayed:
            # 200 OK makes an idempotent retry distinguishable from the first
            # freeze; the stored record is returned verbatim and never rebuilt.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/delivery-admissions/{delivery_id}",
        response_model=AdmissionRecord,
        tags=["delivery-admissions"],
        summary="按交付号取回首次冻结的不可变准入记录（全窗口分段解析版本与判定依据）",
        responses={404: {"description": "准入记录不存在（被拒绝的清单不会固化记录）"}},
    )
    def get_delivery_admission(
        delivery_id: str, request: Request
    ) -> AdmissionRecord:
        result = service_from(request).get_delivery_admission(delivery_id)
        return AdmissionRecord(
            delivery_id=result["delivery_id"],
            created_at=result["created_at"],
            start=result["start"],
            end=result["end"],
            entry_count=result["entry_count"],
            entries=[AdmissionEntryResult(**entry) for entry in result["entries"]],
        )

    # -- license changes --------------------------------------------------

    @app.post(
        "/license-changes",
        response_model=CreateResponse,
        status_code=201,
        tags=["license-changes"],
        summary="登记按 UTC 时刻生效的允许用途变更（幂等）",
        responses={
            200: {"description": "相同标识与内容的重复提交，返回已存在结果"},
            201: {"description": "新变更已创建"},
            409: {"description": "标识相同内容不同，或同版本同时刻存在不同变更"},
        },
    )
    def create_change(
        payload: LicenseChangeRequest, request: Request
    ) -> JSONResponse | CreateResponse:
        change, replayed = service_from(request).submit_change(
            change_id=payload.change_id,
            version_id=payload.version_id,
            effective_at=payload.effective_at,
            allowed_uses=payload.allowed_uses,
        )
        body = CreateResponse(replayed=replayed, change=LicenseChangeResponse(**change))
        if replayed:
            # 200 OK makes idempotent replays distinguishable from creation.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/license-changes/{change_id}",
        response_model=LicenseChangeResponse,
        tags=["license-changes"],
        summary="按标识查询许可变更",
    )
    def get_change(change_id: str, request: Request) -> LicenseChangeResponse:
        return LicenseChangeResponse(**service_from(request).get_change(change_id))

    @app.post(
        "/license-changes/batches",
        response_model=LicenseChangeBatchResponse,
        status_code=201,
        tags=["license-changes"],
        summary="在同一事务内按序导入多条许可变更，逐条呈现真实订阅翻转（整批原子、幂等）",
        responses={
            200: {"description": "同批次标识与规范化后完全相同的列表重试，返回首次冻结结果"},
            201: {"description": "批次首次提交：全部变更与翻转事件在同一事务落库"},
            404: {"description": "批次内存在未知版本，整批拒绝且不落任何库"},
            409: {"description": "批内标识重复、与已存记录冲突或同版本同时刻冲突，或批次标识复用内容不同"},
            422: {"description": "变更列表为空或任一字段非法（含无时区时刻、空用途），整批拒绝"},
        },
    )
    def submit_changes_batch(
        payload: LicenseChangeBatchRequest, request: Request
    ) -> JSONResponse | LicenseChangeBatchResponse:
        result, replayed = service_from(request).submit_changes_batch(
            batch_id=payload.batch_id,
            items=[item.model_dump() for item in payload.changes],
        )
        body = LicenseChangeBatchResponse(
            replayed=replayed,
            batch=LicenseChangeBatchResult(**result),
        )
        if replayed:
            # 200 OK makes an idempotent batch retry distinguishable from the
            # first commit; events are never replayed.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/license-changes/batches/{batch_id}",
        response_model=LicenseChangeBatchResult,
        tags=["license-changes"],
        summary="按批次标识取回首次冻结的批次结果（逐条变更与新增事件数）",
        responses={404: {"description": "批次不存在"}},
    )
    def get_batch(batch_id: str, request: Request) -> LicenseChangeBatchResult:
        return LicenseChangeBatchResult(
            **service_from(request).get_batch(batch_id)
        )

    @app.post(
        "/license-changes/preview",
        response_model=PreviewResponse,
        tags=["license-changes"],
        summary="预演许可变更对目标版本及全部后代的影响（不写入变更）",
        responses={
            200: {"description": "预演完成，返回允许状态发生变化的版本及前后判定"},
            404: {"description": "目标版本不存在"},
            409: {"description": "变更标识冲突，或同版本同生效时刻存在不同变更"},
            422: {"description": "生效/查询时刻缺少时区或无法解析，或参数非法"},
        },
    )
    def preview_change(
        payload: PreviewChangeRequest, request: Request
    ) -> PreviewResponse:
        result = service_from(request).preview_change(
            change_id=payload.change_id,
            version_id=payload.version_id,
            effective_at=payload.effective_at,
            allowed_uses=payload.allowed_uses,
            use=payload.use,
            at=payload.at,
        )
        return PreviewResponse(
            replayed=result["replayed"],
            change=PreviewChangeStub(**result["change"]),
            query=PreviewQuery(**result["query"]),
            scope=result["scope"],
            impacted_versions=[
                ImpactedVersion(**entry) for entry in result["impacted_versions"]
            ],
        )

    @app.post(
        "/license-changes/revocations",
        response_model=RevocationResponse,
        status_code=201,
        tags=["license-changes"],
        summary="作废一条已登记的许可变更（幂等）",
        responses={
            200: {"description": "同一作废标识与内容重试，返回原回执"},
            201: {"description": "作废已登记，返回回执"},
            404: {"description": "目标许可变更不存在"},
            409: {"description": "作废标识复用指向不同内容，或该变更已被另一标识作废"},
        },
    )
    def revoke_change(
        payload: ChangeRevocationRequest, request: Request
    ) -> JSONResponse | RevocationResponse:
        receipt, replayed = service_from(request).revoke_change(
            change_id=payload.change_id,
            revocation_id=payload.revocation_id,
            reason=payload.reason,
        )
        body = RevocationResponse(
            replayed=replayed, receipt=RevocationReceipt(**receipt)
        )
        if replayed:
            # 200 OK distinguishes an idempotent retry from a first-time void.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/license-changes/revocations/{revocation_id}",
        response_model=RevocationReceipt,
        tags=["license-changes"],
        summary="按作废标识追溯作废回执",
        responses={404: {"description": "作废回执不存在"}},
    )
    def get_revocation(revocation_id: str, request: Request) -> RevocationReceipt:
        return RevocationReceipt(**service_from(request).get_revocation(revocation_id))

    # -- permission check -------------------------------------------------

    @app.get(
        "/permissions/check",
        response_model=PermissionResponse,
        tags=["permissions"],
        summary="判定给定版本在某 UTC 时刻是否可用于某用途",
        responses={422: {"description": "时刻缺少时区或无法解析"}},
    )
    def check_permission(
        request: Request,
        version_id: str = Query(..., description="被查询的数据集版本"),
        use: str = Query(..., description="拟使用的用途"),
        at: str = Query(..., alias="at", description="UTC 时刻（带时区），如 2026-07-01T00:00:00Z"),
    ) -> PermissionResponse:
        parsed = _parse_aware_param(at, "at")
        result = service_from(request).check_permission(version_id, use, parsed)
        return PermissionResponse(
            version_id=result["version_id"],
            use=result["use"],
            at=result["at"],
            allowed=result["allowed"],
            closure=result["closure"],
            basis=[BasisEntry(**b) for b in result["basis"]],
            blockers=[BlockerEntry(**b) for b in result["blockers"]],
            allowed_uses_intersection=result["allowed_uses_intersection"],
        )

    @app.get(
        "/permissions/timeline",
        response_model=PermissionTimelineResponse,
        tags=["permissions"],
        summary="把半开区间 [start, end) 切成连续、无重叠的许可判定片段",
        responses={
            404: {"description": "版本不存在"},
            422: {"description": "起止时刻缺少时区或无法解析，或区间为空/倒置"},
        },
    )
    def permission_timeline(
        request: Request,
        version_id: str = Query(..., description="被查询的数据集版本"),
        use: str = Query(..., description="拟使用的用途"),
        start: str = Query(..., description="区间开始时刻（带时区，含），如 2026-01-15T00:00:00Z"),
        end: str = Query(..., description="区间结束时刻（带时区，不含），如 2026-07-15T00:00:00Z"),
    ) -> PermissionTimelineResponse:
        start_dt = _parse_aware_param(start, "start")
        end_dt = _parse_aware_param(end, "end")
        result = service_from(request).check_permission_timeline(
            version_id, use, start_dt, end_dt
        )
        return PermissionTimelineResponse(
            version_id=result["version_id"],
            use=result["use"],
            start=result["start"],
            end=result["end"],
            closure=result["closure"],
            segments=[PermissionSegment(**segment) for segment in result["segments"]],
        )

    @app.post(
        "/permissions/vouchers",
        response_model=VoucherResponse,
        status_code=201,
        tags=["permissions"],
        summary="把一次用途判定作为可追溯凭证原子签发并持久保存",
        responses={
            200: {"description": "同标识与同输入重试，返回首次保存的原凭证"},
            201: {"description": "首次签发：判定与保存已原子完成，返回凭证"},
            404: {"description": "版本不存在"},
            409: {"description": "凭证标识复用到不同输入"},
            422: {"description": "判定时刻缺少时区或无法解析，或用途为空白"},
        },
    )
    def issue_voucher(
        payload: VoucherIssueRequest, request: Request
    ) -> JSONResponse | VoucherResponse:
        voucher, replayed = service_from(request).issue_voucher(
            voucher_id=payload.voucher_id,
            version_id=payload.version_id,
            use=payload.use,
            at=payload.at,
        )
        body = VoucherResponse(
            replayed=replayed, voucher=VoucherDecision(**voucher)
        )
        if replayed:
            # 200 OK distinguishes an idempotent retry from first-time issuing.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/permissions/vouchers/{voucher_id}",
        response_model=VoucherDecision,
        tags=["permissions"],
        summary="按凭证标识查询首次保存的判定凭证（历史不可改写）",
        responses={404: {"description": "凭证不存在"}},
    )
    def get_voucher(voucher_id: str, request: Request) -> VoucherDecision:
        return VoucherDecision(**service_from(request).get_voucher(voucher_id))

    # -- permission result subscriptions (flip event streams) -------------

    @app.post(
        "/permissions/subscriptions",
        response_model=SubscriptionCreateResponse,
        status_code=201,
        tags=["permissions"],
        summary="关注许可结果：原子固化建立当时的允许状态基线（幂等）",
        responses={
            200: {"description": "同标识与同输入重试，返回首次建立的原订阅"},
            201: {"description": "首次建立：判定与基线保存已原子完成，返回订阅"},
            404: {"description": "版本不存在"},
            409: {"description": "订阅标识复用到不同输入"},
            422: {"description": "判定时刻缺少时区或无法解析，或用途为空白"},
        },
    )
    def create_subscription(
        payload: SubscriptionCreateRequest, request: Request
    ) -> JSONResponse | SubscriptionCreateResponse:
        subscription, replayed = service_from(request).create_subscription(
            subscription_id=payload.subscription_id,
            version_id=payload.version_id,
            use=payload.use,
            at=payload.at,
        )
        body = SubscriptionCreateResponse(
            replayed=replayed, subscription=SubscriptionResponse(**subscription)
        )
        if replayed:
            # 200 OK distinguishes an idempotent retry from first-time creation.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/permissions/subscriptions/{subscription_id}",
        response_model=SubscriptionResponse,
        tags=["permissions"],
        summary="按订阅标识查询订阅与其不可变的允许状态基线",
        responses={404: {"description": "订阅不存在"}},
    )
    def get_subscription(subscription_id: str, request: Request) -> SubscriptionResponse:
        return SubscriptionResponse(
            **service_from(request).get_subscription(subscription_id)
        )

    @app.get(
        "/permissions/subscriptions/{subscription_id}/events",
        response_model=SubscriptionEventPage,
        tags=["permissions"],
        summary="按订阅标识与序号游标读取允许状态翻转事件（稳定顺序）",
        responses={404: {"description": "订阅不存在"}},
    )
    def read_subscription_events(
        subscription_id: str,
        request: Request,
        after_seq: int = Query(
            0, ge=0, description="序号游标：仅返回严格大于该序号的事件；0 表示从头读"
        ),
        limit: int = Query(
            100, ge=1, le=1000, description="本页最多返回的事件条数（1–1000）"
        ),
    ) -> SubscriptionEventPage:
        result = service_from(request).read_subscription_events(
            subscription_id, after_seq, limit
        )
        return SubscriptionEventPage(**result)

    # -- signed voucher proof packages (offline audit) --------------------

    @app.post(
        "/permissions/vouchers/{voucher_id}/proof",
        response_model=ProofExportResponse,
        status_code=201,
        tags=["permissions"],
        summary="按凭证标识导出固定签名证明包（首次固化，重复/并发返回同一内容）",
        responses={
            200: {"description": "重复或并发导出：返回已固化的同一证明包"},
            201: {"description": "首次导出：证明包已签名并原子固化"},
            404: {"description": "凭证不存在"},
            503: {"description": "本地签名密钥不可用（缺失/损坏/目录不可写）"},
        },
    )
    def export_voucher_proof(
        voucher_id: str, request: Request
    ) -> JSONResponse | ProofExportResponse:
        package, replayed = service_from(request).export_voucher_proof(voucher_id)
        body = ProofExportResponse(replayed=replayed, proof=ProofPackage(**package))
        if replayed:
            # 200 distinguishes a repeat/concurrent export from first freezing.
            return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        return body

    @app.get(
        "/permissions/vouchers/{voucher_id}/proof",
        response_model=ProofPackage,
        tags=["permissions"],
        summary="取回已固化的签名证明包（不触发签名，不重签）",
        responses={404: {"description": "凭证不存在，或该凭证尚未导出过证明包"}},
    )
    def get_voucher_proof(voucher_id: str, request: Request) -> ProofPackage:
        return ProofPackage(**service_from(request).get_voucher_proof(voucher_id))

    # -- signing keys ------------------------------------------------------

    @app.get(
        "/signing/keys",
        response_model=PublicKeyListResponse,
        tags=["signing"],
        summary="列出全部保留的签发公钥（含轮换前旧公钥）",
    )
    def list_signing_keys(request: Request) -> PublicKeyListResponse:
        state = request.app.state.app_state
        keys = state.key_store.list_public_keys()
        active_id = next(
            (str(entry["key_id"]) for entry in keys if entry.get("active") is True),
            state.key_store.active_key_id(),
        )
        return PublicKeyListResponse(
            active_key_id=active_id,
            keys=[PublicKeyResponse(**entry) for entry in keys],
        )

    @app.get(
        "/signing/keys/{key_id}/public",
        response_model=PublicKeyResponse,
        tags=["signing"],
        summary="按密钥标识取回签发公钥（轮换后的旧公钥仍可取回验签）",
        responses={404: {"description": "密钥标识不存在"}},
    )
    def get_signing_public_key(key_id: str, request: Request) -> PublicKeyResponse:
        state = request.app.state.app_state
        key = state.key_store.get_public_key(key_id)
        if key is None:
            raise NotFoundError("签名公钥不存在", {"key_id": key_id})
        active_id = state.key_store.active_key_id()
        return PublicKeyResponse(**key.public_metadata(active=key_id == active_id))

    @app.post(
        "/signing/keys/rotate",
        response_model=KeyRotationResponse,
        status_code=201,
        tags=["signing"],
        summary="轮换活跃签名密钥（生成并启用新密钥，旧密钥全部保留可验签）",
        responses={503: {"description": "密钥目录不可用，或活跃密钥已丢失"}},
    )
    def rotate_signing_key(request: Request) -> KeyRotationResponse:
        state = request.app.state.app_state
        new_key, previous_id = state.key_store.rotate()
        logger.info(
            "签名密钥已轮换：%s -> %s", previous_id or "<none>", new_key.key_id
        )
        return KeyRotationResponse(
            replayed=False,
            previous_key_id=previous_id,
            key=PublicKeyResponse(**new_key.public_metadata(active=True)),
        )

    return app


app = create_app()

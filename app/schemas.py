"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_aware(value: datetime) -> datetime:
    """Reject naive datetimes; normalise any aware value to UTC."""
    if value.tzinfo is None:
        raise ValueError(
            "时间必须带时区信息，例如 2026-01-01T00:00:00Z 或 2026-01-01T08:00:00+08:00"
        )
    return value.astimezone(timezone.utc)


def _normalise_uses(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        use = raw.strip()
        if not use:
            raise ValueError("allowed_uses 不能包含空字符串")
        if use not in cleaned:
            cleaned.append(use)
    return cleaned


# -- requests --------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    version_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="版本标识；省略时自动生成 UUID"
    )
    parent_ids: list[str] = Field(
        default_factory=list, description="固定引用的已存在父版本（注册后不可修改）"
    )
    note: str = Field(default="", max_length=512)

    @field_validator("parent_ids")
    @classmethod
    def _unique_parents(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            pid = raw.strip()
            if not pid:
                raise ValueError("parent_ids 不能包含空字符串")
            if pid not in cleaned:
                cleaned.append(pid)
        return cleaned


class LicenseChangeRequest(BaseModel):
    change_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="变更标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="变更所属的已登记版本")
    effective_at: datetime = Field(description="UTC 生效时刻（必须带时区）")
    allowed_uses: list[str] = Field(description="该次变更后允许的用途全集（按全集覆盖）")

    _check_time = field_validator("effective_at")(_ensure_aware)

    @field_validator("allowed_uses")
    @classmethod
    def _uses(cls, values: list[str]) -> list[str]:
        return _normalise_uses(values)


class LicenseChangeBatchItem(BaseModel):
    """批量导入中的单条变更，字段口径与单条提交完全一致。"""

    change_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="变更标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="变更所属的已登记版本")
    effective_at: datetime = Field(description="UTC 生效时刻（必须带时区）")
    allowed_uses: list[str] = Field(description="该次变更后允许的用途全集（按全集覆盖）")

    _check_time = field_validator("effective_at")(_ensure_aware)

    @field_validator("allowed_uses")
    @classmethod
    def _uses(cls, values: list[str]) -> list[str]:
        return _normalise_uses(values)


class LicenseChangeBatchRequest(BaseModel):
    """一次批量许可变更导入：唯一批次标识 + 非空有序变更列表。"""

    batch_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="唯一批次标识；省略时自动生成 UUID（自动生成标识无法用于重试幂等）",
    )
    changes: list[LicenseChangeBatchItem] = Field(
        min_length=1, description="非空、按请求顺序逐条应用的变更列表"
    )


# -- version-slot releases -------------------------------------------------


class ReleaseSlotItem(BaseModel):
    """一次槽位发布中的单个槽位：非空槽名、已登记目标版本、预期修订号。"""

    slot: str = Field(
        min_length=1,
        description=(
            "版本槽名（去首尾空格后非空且在本次发布内不可重复）。"
            "槽名会出现在查询接口的路径参数中（/slots/by-name/{slot} 等），"
            "建议使用字母、数字、-、_ 与点，避免包含 /"
        ),
    )
    version_id: str = Field(min_length=1, description="该槽本次指向的已登记版本")
    expected_revision: int = Field(
        ge=0,
        description="该槽预期修订号：首次发布必须为 0；此后必须等于该槽上次发布修订号 + 1",
    )

    @field_validator("slot")
    @classmethod
    def _slot(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("slot 不能为空白字符串")
        return value


class ReleasePublishRequest(BaseModel):
    """一次统一发布：唯一发布标识 + 带时区生效时刻 + 非空槽位清单。"""

    release_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="唯一发布标识；省略时自动生成 UUID（自动生成标识无法用于重试幂等）",
    )
    effective_at: datetime = Field(description="本次发布的带时区生效时刻（服务统一换算为 UTC）")
    slots: list[ReleaseSlotItem] = Field(min_length=1, description="非空槽位清单（按请求顺序冻结）")

    _check_time = field_validator("effective_at")(_ensure_aware)

    @field_validator("slots")
    @classmethod
    def _unique_slots(cls, items: list[ReleaseSlotItem]) -> list[ReleaseSlotItem]:
        seen: set[str] = set()
        for item in items:
            if item.slot in seen:
                raise ValueError(f"槽名在同一次发布中重复：{item.slot}")
            seen.add(item.slot)
        return items


class ReleaseSlotResult(BaseModel):
    """不可变发布记录中的单个槽位结果。"""

    position: int = Field(description="该槽在请求清单中的从 0 起序号")
    slot: str
    version_id: str
    expected_revision: int = Field(description="客户端提交并校验通过的预期修订号")
    revision: int = Field(description="该槽本次发布得到的修订号（首次为 0，之后严格 +1）")


class ReleaseResult(BaseModel):
    """发布首次结果的冻结内容（不含顶层 replayed 标记）。"""

    release_id: str = Field(description="唯一发布标识")
    effective_at: datetime = Field(description="换算为 UTC 后的生效时刻")
    created_at: datetime = Field(description="首次发布时间（服务按 UTC 生成）")
    slot_count: int = Field(ge=1, description="本次发布的槽位数")
    slots: list[ReleaseSlotResult] = Field(description="按请求顺序冻结的各槽结果")


class ReleasePublishResponse(BaseModel):
    replayed: bool = Field(
        description="相同发布标识与规范化后相同内容重试时为 true，返回首次冻结结果且不再推进修订"
    )
    release: ReleaseResult


class SlotResolution(BaseModel):
    """按槽名与带时区时刻解析出的当时最近已发布目标版本。"""

    slot: str
    at: datetime = Field(description="换算为 UTC 后的查询时刻")
    version_id: str = Field(description="当时最近发布所指向的目标版本")
    release_id: str = Field(description="该解析命中的发布标识")
    revision: int = Field(ge=0, description="命中发布给该槽的修订号（0 起）")
    effective_at: datetime = Field(description="命中发布的生效时刻（UTC）")


class SlotHead(BaseModel):
    """某槽当前最新一次发布（修订指针）。"""

    slot: str
    revision: int
    release_id: str
    version_id: str
    effective_at: datetime
    updated_at: datetime


class SlotPermissionResponse(BaseModel):
    """先按槽+时刻解析历史目标版本，再按现有规则判定该用途的复合结果。"""

    slot: str
    use: str
    at: datetime
    resolution: SlotResolution = Field(description="按槽与时刻解析出的当时目标版本")
    version_id: str = Field(description="解析所得目标版本（与 resolution.version_id 一致）")
    allowed: bool
    closure: list[str] = Field(description="解析版本自身及全部祖先（去重、广度优先）")
    basis: list[BasisEntry] = Field(default_factory=list)
    blockers: list[BlockerEntry] = Field(default_factory=list)
    allowed_uses_intersection: list[str] = Field(default_factory=list)


# -- delivery admissions ---------------------------------------------------


class DeliveryAdmissionEntryRequest(BaseModel):
    """交付清单中的单条：引用已登记版本或版本槽，并声明唯一用途。"""

    kind: Literal["version", "slot"] = Field(
        description="引用类型：version=已登记版本；slot=按发布历史随窗口切换解析目标的版本槽"
    )
    ref: str = Field(
        min_length=1,
        description=(
            "版本标识或槽名（去首尾空格后非空）；kind=version 时必须已登记，"
            "kind=slot 时须在窗口起点前（含起点）已发布，否则该条失败"
        ),
    )
    use: str = Field(min_length=1, description="该条交付的拟使用用途（去首尾空格后判定与留痕）")

    @field_validator("ref", "use")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空白字符串")
        return value


class DeliveryAdmissionRequest(BaseModel):
    """一次交付准入：唯一交付号 + 带时区半开窗口 + 非空有序条目清单。"""

    delivery_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="唯一交付号；省略时自动生成 UUID（自动生成标识无法用于重试幂等）",
    )
    start: datetime = Field(description="使用窗口起点（含，必须带时区，服务换算为 UTC）")
    end: datetime = Field(description="使用窗口终点（不含，必须带时区，服务换算为 UTC）")
    entries: list[DeliveryAdmissionEntryRequest] = Field(
        min_length=1, description="非空、按请求顺序冻结的交付条目"
    )

    _check_start = field_validator("start")(_ensure_aware)
    _check_end = field_validator("end")(_ensure_aware)

    @field_validator("delivery_id")
    @classmethod
    def _delivery_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("delivery_id 不能为空白字符串")
        return value


class AdmissionBlockerEntry(BaseModel):
    """准入分段内的一条阻断原因（含槽未发布）。"""

    version_id: str | None = Field(
        default=None, description="阻断版本；槽尚未发布时为 null"
    )
    reason: Literal[
        "no_valid_license", "use_not_allowed", "slot_not_published"
    ]
    change_id: str | None = None
    effective_at: datetime | None = None
    allowed_uses: list[str] = Field(default_factory=list)


class AdmissionResolution(BaseModel):
    """条目在某分段起点解析到的目标。

    版本引用恒指向该版本；槽引用携带当时命中的发布标识、修订号与该发布
    生效时刻。窗口起点前槽尚未发布时，入口级解析为“无目标”指针。
    """

    kind: Literal["version", "slot"]
    ref: str = Field(description="提交的版本标识或槽名（去空格）")
    version_id: str | None = Field(
        default=None, description="解析目标版本；该分段槽未发布时为 null"
    )
    release_id: str | None = Field(
        default=None, description="槽命中的发布标识；版本引用/未发布时为 null"
    )
    revision: int | None = Field(
        default=None, description="槽命中发布的修订号；版本引用/未发布时为 null"
    )
    effective_at: datetime = Field(
        description="解析指针的生效时刻（UTC；版本引用为窗口起点）"
    )


class AdmissionSegment(BaseModel):
    """准入判定的一个连续半开分段：解析版本恒定，许可判定恒定。"""

    start: datetime = Field(description="分段起点（含，UTC）")
    end: datetime = Field(description="分段终点（不含，UTC）")
    allowed: bool = Field(description="该分段内该用途是否始终允许")
    resolution: AdmissionResolution = Field(description="该分段解析到的目标")
    version_id: str | None = Field(
        default=None, description="被判定版本；槽未发布分段为 null"
    )
    closure: list[str] = Field(
        default_factory=list, description="被判定版本自身及全部祖先（槽未发布分段为空）"
    )
    basis: list[BasisEntry] = Field(
        default_factory=list, description="该分段各版本所依据的最近有效变更"
    )
    blockers: list[AdmissionBlockerEntry] = Field(
        default_factory=list, description="该分段全部阻断原因（允许分段为空）"
    )
    allowed_uses_intersection: list[str] = Field(default_factory=list)


class AdmissionEntryResult(BaseModel):
    """单条交付条目的全窗口分段判定（顺序与请求一致）。"""

    position: int = Field(description="该条在请求清单中的从 0 起序号")
    kind: Literal["version", "slot"]
    ref: str
    use: str = Field(description="去首尾空格后的用途")
    resolution: AdmissionResolution = Field(
        description="窗口起点处的解析指针（槽在起点未发布时为无目标指针）"
    )
    segments: list[AdmissionSegment] = Field(
        description="覆盖全窗口、连续无重叠的分段；随槽发布切换解析版本，并按许可变更细分"
    )


class AdmissionRecord(BaseModel):
    """首次固化的不可变交付准入记录（不含顶层 replayed 标记）。"""

    delivery_id: str
    created_at: datetime = Field(description="准入固化时间（服务按 UTC 生成）")
    start: datetime = Field(description="换算为 UTC 后的窗口起点（含）")
    end: datetime = Field(description="换算为 UTC 后的窗口终点（不含）")
    entry_count: int = Field(ge=1)
    entries: list[AdmissionEntryResult] = Field(description="按请求顺序冻结的各条分段判定")


class DeliveryAdmissionResponse(BaseModel):
    replayed: bool = Field(
        description="同交付号与规范化后相同的有序输入重试时为 true，返回首次冻结记录"
    )
    admission: AdmissionRecord


class AdmissionFailure(BaseModel):
    """整单被拒时，单条未通过条目的首个失败时段与阻断原因。"""

    position: int
    kind: Literal["version", "slot"]
    ref: str
    use: str
    failure_start: datetime = Field(description="首个失败时段起点（含，UTC）")
    failure_end: datetime = Field(description="首个失败时段终点（不含，UTC）")
    reason: Literal[
        "no_valid_license", "use_not_allowed", "slot_not_published"
    ] = Field(description="首个失败时段的首要阻断原因代码")
    blockers: list[AdmissionBlockerEntry] = Field(
        description="首个失败时段内的全部阻断版本/槽与原因"
    )


class AdmissionDeniedResponse(BaseModel):
    """HTTP 403：清单未在全窗口获准；整单不留任何准入记录。"""

    error: str = Field(default="admission_denied")
    message: str
    details: dict[str, str | None] = Field(default_factory=dict)
    failures: list[AdmissionFailure] = Field(
        description="每条未通过条目的首个失败时段与阻断原因（顺序与请求一致）"
    )


# -- responses -------------------------------------------------------------


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: str
    created_at: datetime
    note: str
    parent_ids: list[str]
    changes: list["LicenseChangeResponse"]


class LicenseChangeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    change_id: str
    version_id: str
    effective_at: datetime
    allowed_uses: list[str]
    content_hash: str
    created_at: datetime
    revocation: "RevocationReceipt | None" = Field(
        default=None, description="该变更已作废时的作废回执；未作废时为 null"
    )


class ChangeRevocationRequest(BaseModel):
    change_id: str = Field(min_length=1, description="待作废的原许可变更标识")
    revocation_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="唯一作废标识；省略时自动生成 UUID"
    )
    reason: str = Field(min_length=1, max_length=1024, description="作废原因")

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason 不能为空白字符串")
        return value


class RevocationReceipt(BaseModel):
    """作废回执：原标识、作废标识、原因与登记时间，可按作废标识追溯。"""

    revocation_id: str
    change_id: str
    reason: str
    registered_at: datetime


class RevocationResponse(BaseModel):
    replayed: bool = Field(description="同一作废标识与内容重试时为 true，返回原回执")
    receipt: RevocationReceipt


class BasisEntry(BaseModel):
    version_id: str
    change_id: str
    effective_at: datetime
    allowed_uses: list[str]


class BlockerEntry(BaseModel):
    version_id: str
    reason: Literal["no_valid_license", "use_not_allowed"]
    change_id: str | None = None
    effective_at: datetime | None = None
    allowed_uses: list[str] = Field(default_factory=list)


class PermissionResponse(BaseModel):
    version_id: str
    use: str
    at: datetime
    allowed: bool
    closure: list[str] = Field(description="自身及全部祖先（去重、广度优先）")
    basis: list[BasisEntry] = Field(default_factory=list, description="各版本在该时刻所依据的最近变更")
    blockers: list[BlockerEntry] = Field(default_factory=list)
    allowed_uses_intersection: list[str] = Field(default_factory=list)


class PermissionSegment(BaseModel):
    """One continuous slice of a permission timeline.

    Within ``[start, end)`` the evaluation is constant: ``basis`` and
    ``blockers`` are exactly what the single-instant check would report at
    any instant inside the slice.
    """

    start: datetime = Field(description="片段起点（含）")
    end: datetime = Field(description="片段终点（不含）")
    allowed: bool
    basis: list[BasisEntry] = Field(default_factory=list, description="片段内各版本所依据的最近变更")
    blockers: list[BlockerEntry] = Field(default_factory=list, description="片段内全部阻断版本与原因")
    allowed_uses_intersection: list[str] = Field(default_factory=list)


class PermissionTimelineResponse(BaseModel):
    version_id: str
    use: str
    start: datetime = Field(description="区间起点（含）")
    end: datetime = Field(description="区间终点（不含）")
    closure: list[str] = Field(description="自身及全部祖先（去重、广度优先）")
    segments: list[PermissionSegment] = Field(
        description=(
            "半开区间 [start, end) 切出的连续、无重叠片段；"
            "仅在判定依据或阻断信息变化时分段，相邻相同片段已合并"
        )
    )


class CreateResponse(BaseModel):
    """Response wrapper that reports whether a submission was replayed."""

    replayed: bool
    change: LicenseChangeResponse | None = None
    version: VersionResponse | None = None


class LicenseChangeBatchChangeResult(BaseModel):
    """批次内单条变更的应用结果（按请求顺序返回）。"""

    index: int = Field(description="该条在请求列表中的从 0 起序号")
    replayed: bool = Field(
        description="该变更标识与内容此前已存在时为 true（本次为重放，不新增事件）"
    )
    change: LicenseChangeResponse
    new_event_count: int = Field(
        ge=0, description="该条应用时为所有受影响订阅新增的翻转事件总数；重放为 0"
    )


class LicenseChangeBatchResult(BaseModel):
    """批次首次结果的冻结内容（不含顶层 replayed 标记）。"""

    batch_id: str = Field(description="唯一批次标识")
    created_at: datetime = Field(description="批次首次提交时间（服务按 UTC 生成）")
    change_count: int = Field(ge=1, description="批次内变更条数")
    new_event_count: int = Field(
        ge=0, description="整个批次为所有受影响订阅新增的翻转事件总数"
    )
    changes: list[LicenseChangeBatchChangeResult] = Field(
        description="按请求顺序逐条返回的变更结果"
    )


class LicenseChangeBatchResponse(BaseModel):
    replayed: bool = Field(
        description="同批次标识与规范化后完全相同的列表重试时为 true，返回首次冻结结果且不重放事件"
    )
    batch: LicenseChangeBatchResult


# -- permission vouchers ---------------------------------------------------


class VoucherIssueRequest(BaseModel):
    voucher_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="唯一凭证标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="已登记的数据集版本")
    use: str = Field(min_length=1, description="拟申请的用途（去首尾空格后判定与留痕）")
    at: datetime = Field(description="带时区的判定时刻，服务统一换算为 UTC 归一化")

    _check_time = field_validator("at")(_ensure_aware)

    @field_validator("use")
    @classmethod
    def _use(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("use 不能为空白字符串")
        return value


class VoucherDecision(BaseModel):
    """一次用途判定的不可变凭证：首次保存时的完整判定快照。"""

    voucher_id: str = Field(description="唯一凭证标识")
    version_id: str
    use: str
    at: datetime = Field(description="换算为 UTC 后的判定时刻")
    registered_at: datetime = Field(description="凭证登记时间（服务按 UTC 生成）")
    allowed: bool
    closure: list[str] = Field(description="首次判定时的自身及全部祖先（去重、广度优先）")
    basis: list[BasisEntry] = Field(
        default_factory=list, description="首次判定时各版本所依据的最近变更"
    )
    blockers: list[BlockerEntry] = Field(
        default_factory=list, description="首次判定时的全部阻断版本与原因（允许时为空）"
    )
    allowed_uses_intersection: list[str] = Field(default_factory=list)


class VoucherResponse(BaseModel):
    replayed: bool = Field(description="同标识与同输入重试时为 true，返回首次保存的原凭证")
    voucher: VoucherDecision


# -- signed voucher proof packages -----------------------------------------


class ProofBasisEntry(BaseModel):
    """首次判定快照中某版本所依据的变更（签名字段，UTC 文本时间）。"""

    version_id: str
    change_id: str
    effective_at: str
    allowed_uses: list[str]


class ProofBlockerEntry(BaseModel):
    """首次判定快照中的一条阻断原因（签名字段，UTC 文本时间）。"""

    version_id: str
    reason: Literal["no_valid_license", "use_not_allowed"]
    change_id: str | None = None
    effective_at: str | None = None
    allowed_uses: list[str] = Field(default_factory=list)


class ProofPayload(BaseModel):
    """签名覆盖的首次判定完整快照；任一字段被篡改都无法通过验签。"""

    format_version: int = Field(description="证明包/载荷格式版本（签名字段）")
    voucher_id: str = Field(description="凭证标识（签名字段）")
    version_id: str = Field(description="判定版本（签名字段）")
    use: str = Field(description="归一化后的用途（签名字段）")
    at: str = Field(description="判定时刻，UTC 文本 YYYY-MM-DDTHH:MM:SS.ffffffZ（签名字段）")
    registered_at: str = Field(description="凭证登记时刻（首次判定服务生成，签名字段）")
    allowed: bool = Field(description="首次判定结果：允许/拒绝（签名字段）")
    closure: list[str] = Field(description="首次判定的祖先闭包（顺序敏感，签名字段）")
    basis: list[ProofBasisEntry] = Field(description="首次判定的全部依据（顺序敏感，签名字段）")
    blockers: list[ProofBlockerEntry] = Field(description="首次判定的全部阻断原因（顺序敏感，签名字段）")
    allowed_uses_intersection: list[str] = Field(
        description="首次判定的用途交集（顺序敏感，签名字段）"
    )


class ProofSignature(BaseModel):
    algorithm: str = Field(description="签名算法：Ed25519（RFC 8032）")
    jws_alg: str = Field(description="JWS 头 alg：EdDSA（RFC 8037）")
    key_id: str = Field(description="签发所用密钥标识，可据此取回对应公钥")
    protected_header: dict[str, str]
    protected: str = Field(description="BASE64URL(UTF8(保护头 JSON))")
    payload: str = Field(description="BASE64URL(规范 JSON 载荷)，与 payload 重编码逐字节一致")
    signature: str = Field(description="BASE64URL(Ed25519 签名)")
    compact_serialization: str = Field(
        description="JWS 紧凑序列化：protected.payload.signature，可直接离线验签"
    )
    public_key_pem: str = Field(description="签发公钥（SPKI PEM），便于完全离线核验")
    public_key_jwk: dict[str, str] = Field(description="签发公钥（RFC 8037 OKP JWK）")


class ProofEncodingRules(BaseModel):
    format: str
    jws_compact_serialization: str
    protected_header_json: str
    payload_encoding: str
    base64url: str
    signing_input: str
    signature_scheme: str
    timestamp_format: str
    covered_fields: list[str] = Field(
        description="签名覆盖的全部字段；任一字段被篡改都无法通过验签"
    )


class ProofVerification(BaseModel):
    encoding: ProofEncodingRules = Field(description="明确的验签编码规则（离线审计方照此重算）")
    public_key_url: str = Field(description="按密钥标识取公钥的路径模板")
    public_keys_url: str = Field(description="列出全部保留公钥（含轮换前旧公钥）的路径")


class ProofPackage(BaseModel):
    """交给离线审计方的固定证明包；重复/并发导出返回同一内容。"""

    format_version: int
    voucher_id: str
    payload: ProofPayload
    signature: ProofSignature
    verification: ProofVerification
    exported_at: str = Field(
        description="首次导出固化时间（UTC 文本）；不属于签名载荷，旧包不会因此重签"
    )


class ProofExportResponse(BaseModel):
    replayed: bool = Field(description="重复/并发导出时为 true，返回已固化的同一证明包")
    proof: ProofPackage


# -- signing keys -----------------------------------------------------------


class PublicKeyResponse(BaseModel):
    key_id: str
    algorithm: str = Field(description="Ed25519（RFC 8032）")
    jws_alg: str = Field(description="EdDSA（RFC 8037）")
    curve: str
    created_at: str = Field(description="密钥生成时间（UTC 文本）")
    active: bool = Field(description="是否为当前活跃签发密钥")
    public_key_pem: str = Field(description="SPKI PEM 公钥")
    public_key_jwk: dict[str, str] = Field(description="RFC 8037 OKP JWK 公钥")


class PublicKeyListResponse(BaseModel):
    active_key_id: str | None = None
    keys: list[PublicKeyResponse]


class KeyRotationResponse(BaseModel):
    replayed: bool = Field(default=False, description="始终为 false：每次轮换都生成全新密钥")
    previous_key_id: str | None = Field(description="轮换前的活跃密钥标识（旧证明包仍用其验签）")
    key: PublicKeyResponse


# -- change preview --------------------------------------------------------


class PreviewChangeRequest(BaseModel):
    change_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="变更标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="预演变更所属的已登记版本")
    effective_at: datetime = Field(description="UTC 生效时刻（必须带时区）")
    allowed_uses: list[str] = Field(description="假设提交的变更允许用途全集（按全集覆盖）")
    use: str = Field(min_length=1, description="待判定的用途")
    at: datetime = Field(description="带时区的查询时刻")

    _check_effective_time = field_validator("effective_at")(_ensure_aware)
    _check_query_time = field_validator("at")(_ensure_aware)

    @field_validator("allowed_uses")
    @classmethod
    def _uses(cls, values: list[str]) -> list[str]:
        return _normalise_uses(values)

    @field_validator("use")
    @classmethod
    def _use(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("use 不能为空字符串")
        return value


class PreviewChangeStub(BaseModel):
    change_id: str
    version_id: str
    effective_at: datetime
    allowed_uses: list[str]


class PreviewQuery(BaseModel):
    use: str
    at: datetime


class ImpactedVersion(BaseModel):
    version_id: str
    closure: list[str] = Field(description="判定该版本时用到的自身及全部祖先（去重、广度优先）")
    before_allowed: bool = Field(description="现状（假设提交前）的允许状态")
    after_allowed: bool = Field(description="假设该变更提交后的允许状态")
    before_blockers: list[BlockerEntry] = Field(default_factory=list, description="提交前全部阻断版本与原因")
    after_blockers: list[BlockerEntry] = Field(default_factory=list, description="提交后全部阻断版本与原因")
    before_basis: list[BasisEntry] = Field(default_factory=list)
    after_basis: list[BasisEntry] = Field(default_factory=list)
    before_allowed_uses_intersection: list[str] = Field(default_factory=list)
    after_allowed_uses_intersection: list[str] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    replayed: bool = Field(description="相同标识与内容已存在时为 true，预演结果等同于现状（无影响）")
    change: PreviewChangeStub
    query: PreviewQuery
    scope: list[str] = Field(description="目标版本及其全部后代（去重、广度优先）；预演仅在该集合内计算")
    impacted_versions: list[ImpactedVersion] = Field(
        default_factory=list,
        description="允许状态发生变化的版本（含前后判定与阻断原因）；查询早于生效时刻或被更晚变更覆盖时为空",
    )


class ErrorDetail(BaseModel):
    error: str
    message: str
    details: dict[str, str | None] = Field(default_factory=dict)


# -- permission result subscriptions --------------------------------------


class SubscriptionCreateRequest(BaseModel):
    subscription_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="唯一订阅标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="关注的已登记数据集版本")
    use: str = Field(min_length=1, description="关注的用途（去首尾空格后判定与留痕）")
    at: datetime = Field(description="带时区的固定判定时刻，服务统一换算为 UTC 归一化")

    _check_time = field_validator("at")(_ensure_aware)

    @field_validator("use")
    @classmethod
    def _use(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("use 不能为空白字符串")
        return value


class SubscriptionBaseline(BaseModel):
    """建立订阅时原子固化的当时允许状态基线（含阻断原因）。"""

    allowed: bool = Field(description="建立当时的允许状态（之后许可变化不改变基线）")
    blockers: list[BlockerEntry] = Field(
        default_factory=list, description="建立当时的全部阻断版本与原因（允许时为空）"
    )


class SubscriptionResponse(BaseModel):
    subscription_id: str = Field(description="唯一订阅标识")
    version_id: str
    use: str = Field(description="去首尾空格后的关注用途")
    at: datetime = Field(description="换算为 UTC 后的固定判定时刻")
    created_at: datetime = Field(description="订阅建立时间（服务按 UTC 生成）")
    baseline_allowed: bool = Field(description="建立当时原子固化的允许状态基线")
    baseline_blockers: list[BlockerEntry] = Field(
        default_factory=list, description="建立当时的全部阻断版本与原因（允许时为空）"
    )


class SubscriptionCreateResponse(BaseModel):
    replayed: bool = Field(description="同标识与同输入重试时为 true，返回首次建立的原订阅")
    subscription: SubscriptionResponse


class SubscriptionEvent(BaseModel):
    """一次允许状态翻转的持久事件：序号、触发操作、前后状态与阻断原因。"""

    seq: int = Field(description="该订阅内从 1 起严格递增的事件序号")
    trigger_kind: Literal["license_change", "revocation"] = Field(
        description="触发本次翻转的操作类型：许可变更或作废"
    )
    trigger_id: str = Field(
        description="触发操作的标识（变更标识或作废标识），可回溯到原操作"
    )
    change_id: str = Field(description="本次触发涉及的原许可变更标识")
    effective_at: datetime | None = Field(
        description="触发涉及变更的生效时刻（UTC；作废事件记录原变更的生效时刻）"
    )
    before_allowed: bool = Field(description="翻转前的允许状态")
    after_allowed: bool = Field(description="翻转后的允许状态")
    before_blockers: list[BlockerEntry] = Field(
        default_factory=list, description="翻转前的全部阻断版本与原因"
    )
    after_blockers: list[BlockerEntry] = Field(
        default_factory=list, description="翻转后的全部阻断版本与原因"
    )
    created_at: datetime = Field(description="事件追加时间（服务按 UTC 生成）")


class SubscriptionEventPage(BaseModel):
    subscription_id: str
    after_seq: int = Field(description="本次读取所用的序号游标（严格大于该序号的事件才返回）")
    limit: int = Field(description="本页最多返回的事件条数")
    events: list[SubscriptionEvent] = Field(
        description="按序号稳定升序排列的翻转事件页（无翻转时为空数组）"
    )
    next_cursor: int = Field(
        description="下一游标（本页最后一条事件的序号；空页回显 after_seq）"
    )
    has_more: bool = Field(description="是否还有未读事件（用 next_cursor 继续翻页）")

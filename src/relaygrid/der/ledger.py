"""分布式电源接入台账 (DER access ledger).

记录分布式电源的馈线归属、逆变器模式、防孤岛定值、出口限制、调试试验与退役版本。
配置变更须先校验逆变器能力，再经独立审批后方可生效；台账按版本保存，可重建任意时点的有效设置；
退役资源不再进入复电计划。

The ledger keeps a versioned history of every DER's grid-access configuration.
A change must pass a capability check (校验能力) and an independent approval (审批)
before it becomes effective. Retired resources are excluded from restoration.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from relaygrid.der.inverters import InverterCapability
from relaygrid.der.islanding import Resource


class InverterMode(str, Enum):
    GRID_FOLLOWING = "grid_following"  # 跟网型
    GRID_FORMING = "grid_forming"      # 构网型


class ConfigState(str, Enum):
    DRAFT = "draft"        # 草稿
    CHECKED = "checked"    # 已校验能力
    APPROVED = "approved"  # 已审批
    ACTIVE = "active"      # 现行有效
    RETIRED = "retired"    # 已退役


@dataclass(frozen=True)
class AntiIslandingSettings:
    """防孤岛定值 (anti-islanding protection settings)."""

    v_max_pu: float       # 过电压定值 (pu)
    v_min_pu: float       # 欠电压定值 (pu)
    f_max_hz: float       # 过频率定值 (Hz)
    f_min_hz: float       # 欠频率定值 (Hz)
    trip_time_s: float    # 跳闸时间 (s)


@dataclass(frozen=True)
class ExportLimit:
    """出口限制 (export limit)."""

    max_export_kw: float          # 最大出口功率 (kW)
    export_allowed: bool = True   # 是否允许出口


@dataclass(frozen=True)
class CommissioningTest:
    """调试试验记录 (commissioning test record)."""

    test_id: str
    test_type: str        # e.g. anti_islanding / export_limit / ride_through
    performed_at: float
    result: str           # passed / failed
    tester: str
    note: str = ""


@dataclass(frozen=True)
class RetirementRecord:
    """退役记录 (decommissioning record)."""

    resource_id: str
    retired_at: float
    reason: str
    retired_by: str


@dataclass(frozen=True)
class DERConfig:
    """某一版本的接入配置 (a versioned access configuration)."""

    version: int
    state: ConfigState
    feeder_id: str                       # 馈线归属
    inverter_mode: InverterMode          # 逆变器模式
    anti_islanding: AntiIslandingSettings  # 防孤岛定值
    export_limit: ExportLimit            # 出口限制
    capability: InverterCapability       # 校验所依据的逆变器能力
    effective_from: float | None = None
    effective_to: float | None = None
    author: str = ""
    checker: str | None = None
    approver: str | None = None
    tests: tuple[CommissioningTest, ...] = ()
    note: str = ""


def config_violations(config: DERConfig, capability: InverterCapability) -> list[str]:
    """校验配置是否在逆变器能力范围内，返回违规项 (capability + settings validation)."""
    violations: list[str] = []
    exp = config.export_limit
    if exp.max_export_kw < 0:
        violations.append("export_limit_negative")
    if not exp.export_allowed and exp.max_export_kw != 0:
        violations.append("export_disabled_but_limit_positive")
    if exp.max_export_kw > capability.kw_available + 1e-9:
        violations.append("export_exceeds_available_kw")
    if exp.max_export_kw > capability.kva + 1e-9:
        violations.append("export_exceeds_nameplate_kva")
    if config.inverter_mode == InverterMode.GRID_FORMING and not capability.grid_forming_capable:
        violations.append("inverter_not_grid_forming_capable")
    ai = config.anti_islanding
    if ai.v_max_pu <= ai.v_min_pu:
        violations.append("invalid_voltage_window")
    if ai.f_max_hz <= ai.f_min_hz:
        violations.append("invalid_frequency_window")
    if ai.trip_time_s <= 0:
        violations.append("invalid_trip_time")
    if ai.v_min_pu <= 0 or ai.f_min_hz <= 0:
        violations.append("invalid_protection_threshold")
    return violations


class DERLedger:
    """分布式电源接入台账 (DER access ledger)."""

    def __init__(self) -> None:
        self._configs: dict[str, list[DERConfig]] = {}
        self._retirements: dict[str, RetirementRecord] = {}

    # ---- 接入登记 (registration) -----------------------------------------

    def register(
        self,
        resource_id: str,
        feeder_id: str,
        inverter_mode: InverterMode,
        anti_islanding: AntiIslandingSettings,
        export_limit: ExportLimit,
        capability: InverterCapability,
        at: float,
        author: str = "",
    ) -> DERConfig:
        """登记分布式电源，生成现行有效版本 (version 1, active)."""
        if resource_id in self._configs:
            raise ValueError(f"resource {resource_id} already registered")
        cfg = DERConfig(
            version=1,
            state=ConfigState.ACTIVE,
            feeder_id=feeder_id,
            inverter_mode=inverter_mode,
            anti_islanding=anti_islanding,
            export_limit=export_limit,
            capability=capability,
            effective_from=at,
            author=author,
        )
        violations = config_violations(cfg, capability)
        if violations:
            raise ValueError(f"capability violations: {violations}")
        self._configs[resource_id] = [cfg]
        return cfg

    # ---- 配置变更工作流 (change workflow) --------------------------------

    def propose_change(
        self,
        resource_id: str,
        *,
        feeder_id: str | None = None,
        inverter_mode: InverterMode | None = None,
        anti_islanding: AntiIslandingSettings | None = None,
        export_limit: ExportLimit | None = None,
        author: str,
        note: str = "",
    ) -> DERConfig:
        """基于现行配置起草新版本 (draft)."""
        self._require_registered(resource_id)
        if resource_id in self._retirements:
            raise ValueError(f"resource {resource_id} is retired")
        current = self._current_config(resource_id)
        if current is None:
            raise ValueError("no active config to change from")
        if self._pending(resource_id) is not None:
            raise ValueError("a pending change already exists")
        draft = replace(
            current,
            version=current.version + 1,
            state=ConfigState.DRAFT,
            feeder_id=feeder_id if feeder_id is not None else current.feeder_id,
            inverter_mode=inverter_mode if inverter_mode is not None else current.inverter_mode,
            anti_islanding=anti_islanding if anti_islanding is not None else current.anti_islanding,
            export_limit=export_limit if export_limit is not None else current.export_limit,
            effective_from=None,
            effective_to=None,
            author=author,
            checker=None,
            approver=None,
            tests=(),
            note=note,
        )
        self._configs[resource_id].append(draft)
        return draft

    def check_capability(self, resource_id: str, checker: str) -> DERConfig:
        """校验能力：通过则由 DRAFT 转为 CHECKED (technical capability gate)."""
        draft = self._pending(resource_id)
        if draft is None or draft.state != ConfigState.DRAFT:
            raise ValueError("no draft pending capability check")
        if checker == draft.author:
            raise ValueError("independent checker required")
        violations = config_violations(draft, draft.capability)
        if violations:
            raise ValueError(f"capability violations: {violations}")
        updated = replace(draft, state=ConfigState.CHECKED, checker=checker)
        self._replace(resource_id, draft, updated)
        return updated

    def approve(self, resource_id: str, approver: str) -> DERConfig:
        """审批：CHECKED 转为 APPROVED，审批人须独立于起草人与校验人 (approval gate)."""
        draft = self._pending(resource_id)
        if draft is None or draft.state != ConfigState.CHECKED:
            raise ValueError("config not checked")
        if approver in {draft.author, draft.checker}:
            raise ValueError("independent approver required")
        updated = replace(draft, state=ConfigState.APPROVED, approver=approver)
        self._replace(resource_id, draft, updated)
        return updated

    def activate(self, resource_id: str, at: float) -> DERConfig:
        """使已审批版本生效，同时闭合旧版本 (activate, closes previous version)."""
        self._require_registered(resource_id)
        if resource_id in self._retirements:
            raise ValueError(f"resource {resource_id} is retired")
        draft = self._pending(resource_id)
        if draft is None or draft.state != ConfigState.APPROVED:
            raise ValueError("config not approved")
        current = self._current_config(resource_id)
        if current is not None and at < current.effective_from:
            raise ValueError("activation time before current effective window")
        if current is not None:
            self._replace(
                resource_id,
                current,
                replace(current, effective_to=at),
            )
        updated = replace(draft, state=ConfigState.ACTIVE, effective_from=at)
        self._replace(resource_id, draft, updated)
        return updated

    def retire(self, resource_id: str, at: float, reason: str, retired_by: str) -> RetirementRecord:
        """退役：闭合现行版本并记录退役 (decommissioning, records retirement)."""
        self._require_registered(resource_id)
        if resource_id in self._retirements:
            raise ValueError(f"resource {resource_id} already retired")
        if self._pending(resource_id) is not None:
            raise ValueError("cannot retire with a pending change")
        current = self._current_config(resource_id)
        if current is not None:
            self._replace(
                resource_id,
                current,
                replace(current, state=ConfigState.RETIRED, effective_to=at),
            )
        rec = RetirementRecord(resource_id, at, reason, retired_by)
        self._retirements[resource_id] = rec
        return rec

    # ---- 调试试验 (commissioning tests) ----------------------------------

    def add_test(self, resource_id: str, test: CommissioningTest) -> DERConfig:
        """向待生效版本附加调试试验记录 (attach a commissioning test to a pending version)."""
        draft = self._pending(resource_id)
        if draft is None:
            raise ValueError("no pending version to attach test to")
        updated = replace(draft, tests=draft.tests + (test,))
        self._replace(resource_id, draft, updated)
        return updated

    # ---- 时态重建 (temporal reconstruction) ------------------------------

    def settings_at(self, resource_id: str, ts: float) -> DERConfig | None:
        """重建资源在 ts 时点的有效设置 (effective settings at ts).

        退役资源在退役时点之后返回 None。
        """
        self._require_registered(resource_id)
        rec = self._retirements.get(resource_id)
        if rec is not None and ts >= rec.retired_at:
            return None
        for cfg in self._configs[resource_id]:
            if cfg.effective_from is None:
                continue
            if cfg.effective_from <= ts and (cfg.effective_to is None or ts < cfg.effective_to):
                return cfg
        return None

    def history(self, resource_id: str) -> list[DERConfig]:
        """返回全部版本 (all versions)."""
        self._require_registered(resource_id)
        return list(self._configs[resource_id])

    def current_config(self, resource_id: str) -> DERConfig | None:
        """返回现行有效版本 (current active config)."""
        self._require_registered(resource_id)
        return self._current_config(resource_id)

    def get_version(self, resource_id: str, version: int) -> DERConfig:
        self._require_registered(resource_id)
        for cfg in self._configs[resource_id]:
            if cfg.version == version:
                return cfg
        raise KeyError(version)

    def retirement(self, resource_id: str) -> RetirementRecord | None:
        return self._retirements.get(resource_id)

    def is_retired(self, resource_id: str, at: float) -> bool:
        rec = self._retirements.get(resource_id)
        return rec is not None and at >= rec.retired_at

    # ---- 复电计划接入 (restoration integration) --------------------------

    def active_resource_ids(self, at: float) -> list[str]:
        """返回 at 时点未退役的资源 (non-retired resource ids)."""
        return [rid for rid in self._configs if not self.is_retired(rid, at)]

    def resources(self, at: float) -> tuple[Resource, ...]:
        """生成复电计划可用的资源，已剔除退役资源 (resources for restoration, retired excluded).

        available_kw 取有效出口限制；grid_forming 取逆变器模式。
        """
        out: list[Resource] = []
        for rid in self.active_resource_ids(at):
            cfg = self.settings_at(rid, at)
            if cfg is None:
                continue
            export = cfg.export_limit.max_export_kw if cfg.export_limit.export_allowed else 0.0
            out.append(
                Resource(
                    resource_id=rid,
                    available_kw=max(0.0, export),
                    min_kw=0.0,
                    grid_forming=(cfg.inverter_mode == InverterMode.GRID_FORMING),
                    spinning_reserve_kw=0.0,
                )
            )
        return tuple(out)

    def assert_restoration_eligible(self, resource_id: str, at: float) -> None:
        """守卫：退役资源不得进入复电计划 (guard: retired resources cannot enter restoration)."""
        self._require_registered(resource_id)
        if self.is_retired(resource_id, at):
            raise ValueError(
                f"resource {resource_id} is retired at {at} and cannot enter restoration"
            )

    # ---- 内部工具 (internals) --------------------------------------------

    def _require_registered(self, resource_id: str) -> None:
        if resource_id not in self._configs:
            raise KeyError(resource_id)

    def _current_config(self, resource_id: str) -> DERConfig | None:
        for cfg in reversed(self._configs[resource_id]):
            if cfg.state == ConfigState.ACTIVE and cfg.effective_to is None:
                return cfg
        return None

    def _pending(self, resource_id: str) -> DERConfig | None:
        for cfg in reversed(self._configs[resource_id]):
            if cfg.state in {ConfigState.DRAFT, ConfigState.CHECKED, ConfigState.APPROVED}:
                return cfg
        return None

    def _replace(self, resource_id: str, old: DERConfig, new: DERConfig) -> None:
        items = self._configs[resource_id]
        idx = items.index(old)
        items[idx] = new

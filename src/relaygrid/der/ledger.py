"""Distributed-resource interconnection ledger.

Records feeder ownership, inverter mode, anti-islanding settings, export
limits, commissioning tests and the retirement revision of every distributed
resource.  Every settings change is capability-checked against the inverter
profile and must be approved by an authority covering the affected scope;
approved versions form non-overlapping effective intervals, so the settings
in force at any point in time can be reconstructed.  Retired resources are
barred from restoration plans.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from typing import Any

INVERTER_GRID_FORMING = "grid_forming"
INVERTER_GRID_FOLLOWING = "grid_following"
INVERTER_MODES = (INVERTER_GRID_FORMING, INVERTER_GRID_FOLLOWING)

STATE_REGISTERED = "registered"
STATE_COMMISSIONED = "commissioned"
STATE_RETIRED = "retired"

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Approval scopes.  An approver must cover every scope touched by a change.
SCOPE_FEEDER = "feeder"
SCOPE_INVERTER = "inverter"
SCOPE_PROTECTION = "protection"
SCOPE_EXPORT = "export"

_UNSET = object()


@dataclass(frozen=True)
class AntiIslandingSettings:
    under_voltage_pu: float
    over_voltage_pu: float
    under_freq_hz: float
    over_freq_hz: float
    trip_time_s: float
    reconnect_delay_s: float


@dataclass(frozen=True)
class ExportLimit:
    max_export_kw: float
    fixed_power_factor: float | None = None


@dataclass(frozen=True)
class CommissioningTest:
    test_id: str
    performed_at: float
    passed: bool
    checks: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class InverterProfile:
    """Capability envelope advertised by an inverter make/model."""
    profile_id: str
    supported_modes: tuple[str, ...]
    grid_forming_capable: bool
    nameplate_kw: float
    export_capability_kw: float
    voltage_envelope_pu: tuple[float, float]
    frequency_envelope_hz: tuple[float, float]
    trip_time_range_s: tuple[float, float]
    min_reconnect_delay_s: float
    anti_islanding_relay: bool = True


@dataclass(frozen=True)
class ConfigVersion:
    version: int
    resource_id: str
    inverter_mode: str
    feeder_id: str
    connection_node: str
    anti_islanding: AntiIslandingSettings
    export_limit: ExportLimit
    status: str
    proposed_at: float
    proposed_by: str
    reason: str
    effective_at: float | None = None
    superseded_at: float | None = None
    approved_by: str | None = None
    affected_scopes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DERRecord:
    resource_id: str
    profile_id: str
    technology: str
    nameplate_kw: float
    nameplate_kvar: float
    inverter_mode: str
    feeder_id: str
    connection_node: str
    anti_islanding: AntiIslandingSettings
    export_limit: ExportLimit
    state: str
    registered_at: float
    commissioned_at: float | None = None
    retired_at: float | None = None
    retire_reason: str = ""
    tests: tuple[CommissioningTest, ...] = ()
    version: int = 1


def validate_settings(profile: InverterProfile, inverter_mode: str,
                      anti: AntiIslandingSettings, limit: ExportLimit,
                      feeder_id: str, known_feeders: set[str] | None = None
                      ) -> tuple[str, ...]:
    """Return the capability violations (empty tuple means compliant)."""
    reasons: list[str] = []
    if inverter_mode not in INVERTER_MODES:
        reasons.append("invalid_inverter_mode")
    if inverter_mode not in profile.supported_modes:
        reasons.append("unsupported_inverter_mode")
    if inverter_mode == INVERTER_GRID_FORMING and not profile.grid_forming_capable:
        reasons.append("grid_forming_not_capable")
    if not profile.anti_islanding_relay:
        reasons.append("missing_anti_islanding_relay")
    vlo, vhi = profile.voltage_envelope_pu
    if not (vlo <= anti.under_voltage_pu < anti.over_voltage_pu <= vhi):
        reasons.append("voltage_envelope_violation")
    flo, fhi = profile.frequency_envelope_hz
    if not (flo <= anti.under_freq_hz < anti.over_freq_hz <= fhi):
        reasons.append("frequency_envelope_violation")
    tlo, thi = profile.trip_time_range_s
    if not (tlo <= anti.trip_time_s <= thi):
        reasons.append("trip_time_out_of_range")
    if anti.reconnect_delay_s < profile.min_reconnect_delay_s:
        reasons.append("reconnect_delay_too_short")
    if limit.max_export_kw < 0:
        reasons.append("negative_export_limit")
    if limit.max_export_kw > profile.export_capability_kw + 1e-9:
        reasons.append("export_exceeds_capability")
    if limit.max_export_kw > profile.nameplate_kw + 1e-9:
        reasons.append("export_exceeds_nameplate")
    if limit.fixed_power_factor is not None and not (0.0 < abs(limit.fixed_power_factor) <= 1.0):
        reasons.append("invalid_power_factor")
    if known_feeders and feeder_id not in known_feeders:
        reasons.append("unknown_feeder")
    return tuple(reasons)


class InterconnectionLedger:
    def __init__(self, feeders=(), approvers: dict[str, frozenset[str]] | None = None):
        self.feeders = set(feeders)
        self.approvers: dict[str, frozenset[str]] = dict(approvers or {})
        self.profiles: dict[str, InverterProfile] = {}
        self.records: dict[str, DERRecord] = {}
        self.versions: dict[str, list[ConfigVersion]] = {}

    # ---- reference data -------------------------------------------------
    def add_profile(self, profile: InverterProfile) -> None:
        if profile.profile_id in self.profiles:
            raise ValueError("duplicate profile")
        self.profiles[profile.profile_id] = profile

    def add_approver(self, approver: str, scopes) -> None:
        self.approvers[approver] = frozenset(scopes)

    # ---- registration / lifecycle --------------------------------------
    def register(self, resource_id: str, *, profile_id: str, technology: str,
                 nameplate_kw: float, nameplate_kvar: float, inverter_mode: str,
                 feeder_id: str, connection_node: str,
                 anti_islanding: AntiIslandingSettings, export_limit: ExportLimit,
                 registered_at: float, accepted_by: str) -> DERRecord:
        if resource_id in self.records:
            raise ValueError("duplicate resource")
        profile = self._profile(profile_id)
        violations = validate_settings(profile, inverter_mode, anti_islanding,
                                       export_limit, feeder_id, self.feeders)
        if violations:
            raise ValueError(f"capability violations: {','.join(violations)}")
        record = DERRecord(resource_id, profile_id, technology, nameplate_kw,
                           nameplate_kvar, inverter_mode, feeder_id,
                           connection_node, anti_islanding, export_limit,
                           STATE_REGISTERED, registered_at)
        # Version 1 is the interconnection-agreement baseline, accepted at
        # registration; later changes go through propose/approve.
        baseline = ConfigVersion(
            1, resource_id, inverter_mode, feeder_id, connection_node,
            anti_islanding, export_limit, STATUS_APPROVED, registered_at,
            accepted_by, "interconnection_agreement",
            effective_at=registered_at, approved_by=accepted_by)
        self.records[resource_id] = record
        self.versions[resource_id] = [baseline]
        return record

    def add_test(self, resource_id: str, test: CommissioningTest) -> None:
        record = self._record(resource_id)
        if record.state == STATE_RETIRED:
            raise ValueError("resource retired")
        if any(t.test_id == test.test_id for t in record.tests):
            raise ValueError("duplicate test")
        self.records[resource_id] = replace(record, tests=record.tests + (test,))

    def commission(self, resource_id: str, commissioned_at: float) -> DERRecord:
        record = self._record(resource_id)
        if record.state != STATE_REGISTERED:
            raise ValueError(f"cannot commission from {record.state}")
        if not any(t.passed for t in record.tests):
            raise ValueError("no_passing_commissioning_test")
        if commissioned_at < record.registered_at:
            raise ValueError("commission_before_registration")
        record = replace(record, state=STATE_COMMISSIONED,
                         commissioned_at=commissioned_at)
        self.records[resource_id] = record
        return record

    def retire(self, resource_id: str, retired_at: float, reason: str = "") -> DERRecord:
        record = self._record(resource_id)
        if record.state == STATE_RETIRED:
            raise ValueError("already retired")
        if record.commissioned_at is not None and retired_at < record.commissioned_at:
            raise ValueError("retire_before_commission")
        versions = self.versions[resource_id]
        # Retirement terminates every still-open change request.
        for i, v in enumerate(versions):
            if v.status == STATUS_PROPOSED:
                versions[i] = replace(v, status=STATUS_REJECTED,
                                      approved_by="system:retirement")
            elif v.status == STATUS_APPROVED and v.superseded_at is None:
                versions[i] = replace(v, superseded_at=retired_at)
        record = replace(record, state=STATE_RETIRED, retired_at=retired_at,
                         retire_reason=reason)
        self.records[resource_id] = record
        return record

    # ---- configuration changes -----------------------------------------
    def propose_change(self, resource_id: str, *, at: float, actor: str,
                       reason: str, inverter_mode: Any = _UNSET,
                       feeder_id: Any = _UNSET, connection_node: Any = _UNSET,
                       anti_islanding: Any = _UNSET,
                       export_limit: Any = _UNSET) -> ConfigVersion:
        record = self._record(resource_id)
        if record.state == STATE_RETIRED:
            raise ValueError("resource retired")
        pending = [v for v in self.versions[resource_id]
                   if v.status == STATUS_PROPOSED]
        if pending:
            raise ValueError("pending_change_exists")

        mode = record.inverter_mode if inverter_mode is _UNSET else inverter_mode
        fid = record.feeder_id if feeder_id is _UNSET else feeder_id
        node = record.connection_node if connection_node is _UNSET else connection_node
        anti = record.anti_islanding if anti_islanding is _UNSET else anti_islanding
        limit = record.export_limit if export_limit is _UNSET else export_limit

        violations = validate_settings(self._profile(record.profile_id), mode,
                                       anti, limit, fid, self.feeders)
        if violations:
            raise ValueError(f"capability violations: {','.join(violations)}")

        scopes: set[str] = set()
        if mode != record.inverter_mode:
            scopes.add(SCOPE_INVERTER)
        if fid != record.feeder_id or node != record.connection_node:
            scopes.add(SCOPE_FEEDER)
        if anti != record.anti_islanding:
            scopes.add(SCOPE_PROTECTION)
        if limit != record.export_limit:
            scopes.add(SCOPE_EXPORT)
        if not scopes:
            raise ValueError("no_effective_change")

        version = ConfigVersion(
            max(v.version for v in self.versions[resource_id]) + 1,
            resource_id, mode, fid, node, anti, limit, STATUS_PROPOSED, at,
            actor, reason, affected_scopes=frozenset(scopes))
        self.versions[resource_id].append(version)
        return version

    def approve_change(self, resource_id: str, version: int, *,
                       approver: str, at: float) -> ConfigVersion:
        record = self._record(resource_id)
        if record.state == STATE_RETIRED:
            raise ValueError("resource retired")
        versions = self.versions[resource_id]
        candidate = next((v for v in versions if v.version == version), None)
        if candidate is None or candidate.status != STATUS_PROPOSED:
            raise ValueError("no such proposed change")
        if candidate.version != max(v.version for v in versions):
            raise ValueError("superseded proposal")
        current = next((v for v in versions
                        if v.status == STATUS_APPROVED and v.superseded_at is None), None)
        if current is not None and at < current.effective_at:
            raise ValueError("effective_before_current_version")
        if approver == candidate.proposed_by:
            raise ValueError("self_approval_forbidden")
        granted = self.approvers.get(approver)
        if granted is None:
            raise ValueError("unknown_approver")
        if not candidate.affected_scopes <= granted:
            raise ValueError("approver_scope_insufficient")

        for i, v in enumerate(versions):
            if v.status == STATUS_APPROVED and v.superseded_at is None:
                versions[i] = replace(v, superseded_at=at)
        approved = replace(candidate, status=STATUS_APPROVED, effective_at=at,
                           approved_by=approver)
        versions[candidate.version - 1] = approved
        self.records[resource_id] = replace(
            record, inverter_mode=approved.inverter_mode,
            feeder_id=approved.feeder_id, connection_node=approved.connection_node,
            anti_islanding=approved.anti_islanding, export_limit=approved.export_limit,
            version=approved.version)
        return approved

    def reject_change(self, resource_id: str, version: int, *,
                      rejected_by: str) -> ConfigVersion:
        versions = self.versions[resource_id]
        candidate = next((v for v in versions if v.version == version), None)
        if candidate is None or candidate.status != STATUS_PROPOSED:
            raise ValueError("no such proposed change")
        rejected = replace(candidate, status=STATUS_REJECTED, approved_by=rejected_by)
        versions[version - 1] = rejected
        return rejected

    # ---- point-in-time reconstruction ----------------------------------
    def settings_at(self, resource_id: str, at: float) -> ConfigVersion | None:
        """Approved version in force at ``at`` (None if retired/not yet live)."""
        for v in self.versions.get(resource_id, ()):
            if v.status != STATUS_APPROVED or v.effective_at is None:
                continue
            end = v.superseded_at if v.superseded_at is not None else float("inf")
            if v.effective_at <= at < end:
                return v
        return None

    def history(self, resource_id: str) -> tuple[ConfigVersion, ...]:
        return tuple(self.versions.get(resource_id, ()))

    # ---- restoration gating --------------------------------------------
    def eligible_for_restoration(self, resource_id: str,
                                 at: float | None = None) -> bool:
        record = self.records.get(resource_id)
        if record is None:
            return False
        if at is None:
            return record.state == STATE_COMMISSIONED
        if record.state == STATE_RETIRED and at >= record.retired_at:
            return False
        if record.commissioned_at is None or at < record.commissioned_at:
            return False
        return self.settings_at(resource_id, at) is not None

    def restoration_candidates(self, resource_ids, at: float | None = None
                               ) -> list[str]:
        return [r for r in resource_ids if self.eligible_for_restoration(r, at)]

    def islanding_resource(self, resource_id: str, available_kw: float,
                           min_kw: float = 0.0):
        """Project a ledger record onto the islanding ``Resource`` model."""
        from relaygrid.der.islanding import Resource
        record = self._record(resource_id)
        return Resource(
            resource_id, available_kw, min_kw,
            grid_forming=record.inverter_mode == INVERTER_GRID_FORMING)

    # ---- helpers --------------------------------------------------------
    def _profile(self, profile_id: str) -> InverterProfile:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise ValueError("unknown capability profile")
        return profile

    def _record(self, resource_id: str) -> DERRecord:
        record = self.records.get(resource_id)
        if record is None:
            raise ValueError("unknown resource")
        return record

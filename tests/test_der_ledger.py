import pytest

from relaygrid.der.ledger import (
    AntiIslandingSettings, CommissioningTest, ConfigVersion, ExportLimit,
    InterconnectionLedger, InverterProfile, STATE_COMMISSIONED, STATE_RETIRED,
    STATUS_APPROVED, STATUS_PROPOSED, STATUS_REJECTED, INVERTER_GRID_FORMING,
    INVERTER_GRID_FOLLOWING, SCOPE_EXPORT, SCOPE_INVERTER, SCOPE_PROTECTION,
    validate_settings,
)
from relaygrid.der.islanding import Resource
from relaygrid.services.restoration_service import RestorationService, RestorationRequest
from relaygrid.loads.priority import LoadBlock

PV_PROFILE = InverterProfile(
    profile_id="inv-pv-100",
    supported_modes=(INVERTER_GRID_FOLLOWING,),
    grid_forming_capable=False,
    nameplate_kw=100.0,
    export_capability_kw=80.0,
    voltage_envelope_pu=(0.5, 1.2),
    frequency_envelope_hz=(45.0, 55.0),
    trip_time_range_s=(0.05, 2.0),
    min_reconnect_delay_s=60.0,
)

BESS_PROFILE = InverterProfile(
    profile_id="inv-bess-100",
    supported_modes=(INVERTER_GRID_FOLLOWING, INVERTER_GRID_FORMING),
    grid_forming_capable=True,
    nameplate_kw=100.0,
    export_capability_kw=100.0,
    voltage_envelope_pu=(0.5, 1.2),
    frequency_envelope_hz=(45.0, 55.0),
    trip_time_range_s=(0.05, 2.0),
    min_reconnect_delay_s=60.0,
)

STANDARD_AI = AntiIslandingSettings(0.85, 1.10, 49.0, 51.0, 0.16, 300.0)


def build_ledger():
    ledger = InterconnectionLedger(feeders={"F1", "F2"})
    ledger.add_profile(PV_PROFILE)
    ledger.add_profile(BESS_PROFILE)
    ledger.add_approver("ops-mgr", frozenset({SCOPE_INVERTER, SCOPE_EXPORT, SCOPE_PROTECTION}))
    ledger.add_approver("protection-eng", frozenset({SCOPE_PROTECTION}))
    ledger.add_approver("dispatcher", frozenset())
    return ledger


def register(ledger, rid="pv-1", profile_id="inv-pv-100", mode=INVERTER_GRID_FOLLOWING):
    return ledger.register(
        rid, profile_id=profile_id, technology="solar_pv",
        nameplate_kw=100.0, nameplate_kvar=50.0, inverter_mode=mode,
        feeder_id="F1", connection_node="B-12",
        anti_islanding=STANDARD_AI, export_limit=ExportLimit(60.0, 1.0),
        registered_at=1000.0, accepted_by="ops-mgr")


def test_registration_creates_effective_baseline():
    ledger = build_ledger()
    record = register(ledger)
    assert record.state == "registered"
    settings = ledger.settings_at("pv-1", 1000.0)
    assert settings.version == 1 and settings.status == STATUS_APPROVED
    assert settings.feeder_id == "F1" and settings.inverter_mode == INVERTER_GRID_FOLLOWING
    assert settings.anti_islanding.trip_time_s == 0.16
    assert settings.export_limit.max_export_kw == 60.0


def test_registration_rejected_when_beyond_capability():
    ledger = build_ledger()
    with pytest.raises(ValueError, match="capability violations"):
        ledger.register(
            "pv-1", profile_id="inv-pv-100", technology="solar_pv",
            nameplate_kw=100.0, nameplate_kvar=50.0,
            inverter_mode=INVERTER_GRID_FORMING,  # PV unit cannot grid-form
            feeder_id="F1", connection_node="B-12",
            anti_islanding=STANDARD_AI, export_limit=ExportLimit(60.0),
            registered_at=1000.0, accepted_by="ops-mgr")
    with pytest.raises(ValueError, match="capability violations"):
        ledger.register(
            "pv-2", profile_id="inv-pv-100", technology="solar_pv",
            nameplate_kw=100.0, nameplate_kvar=50.0,
            inverter_mode=INVERTER_GRID_FOLLOWING,
            feeder_id="F9", connection_node="B-12",  # unknown feeder
            anti_islanding=STANDARD_AI, export_limit=ExportLimit(95.0),  # > 80 export
            registered_at=1000.0, accepted_by="ops-mgr")


def test_export_cannot_exceed_inverter_capability():
    violations = validate_settings(
        PV_PROFILE, INVERTER_GRID_FOLLOWING, STANDARD_AI,
        ExportLimit(90.0), "F1")
    assert "export_exceeds_capability" in violations


def test_change_requires_approval_and_is_not_effective_until_approved():
    ledger = build_ledger()
    register(ledger)
    proposal = ledger.propose_change(
        "pv-1", at=2000.0, actor="planner", reason="curtailment order",
        export_limit=ExportLimit(40.0, 1.0))
    assert proposal.status == STATUS_PROPOSED
    # Still the old limit before approval.
    assert ledger.settings_at("pv-1", 2001.0).export_limit.max_export_kw == 60.0
    assert ledger.records["pv-1"].export_limit.max_export_kw == 60.0
    # Self approval is forbidden.
    with pytest.raises(ValueError, match="self_approval"):
        ledger.approve_change("pv-1", proposal.version, approver="planner", at=2100.0)
    # A dispatcher with no scopes cannot approve.
    with pytest.raises(ValueError, match="scope_insufficient"):
        ledger.approve_change("pv-1", proposal.version, approver="dispatcher", at=2100.0)
    approved = ledger.approve_change("pv-1", proposal.version,
                                     approver="ops-mgr", at=2100.0)
    assert approved.effective_at == 2100.0 and approved.approved_by == "ops-mgr"
    assert ledger.records["pv-1"].version == 2


def test_change_exceeding_capability_cannot_be_proposed():
    ledger = build_ledger()
    register(ledger)
    with pytest.raises(ValueError, match="capability violations"):
        ledger.propose_change("pv-1", at=2000.0, actor="planner",
                              reason="bad", export_limit=ExportLimit(99.0))
    with pytest.raises(ValueError, match="capability violations"):
        ledger.propose_change("pv-1", at=2000.0, actor="planner",
                              reason="bad", inverter_mode=INVERTER_GRID_FORMING)


def test_scope_separation_protection_only_approver():
    ledger = build_ledger()
    register(ledger)
    proposal = ledger.propose_change(
        "pv-1", at=2000.0, actor="planner", reason="tighter trip",
        anti_islanding=AntiIslandingSettings(0.88, 1.09, 49.2, 50.8, 0.10, 300.0))
    assert SCOPE_PROTECTION in proposal.affected_scopes
    ledger.approve_change("pv-1", proposal.version,
                          approver="protection-eng", at=2100.0)
    assert ledger.records["pv-1"].anti_islanding.trip_time_s == 0.10


def test_rejected_change_leaves_settings_untouched():
    ledger = build_ledger()
    register(ledger)
    proposal = ledger.propose_change(
        "pv-1", at=2000.0, actor="planner", reason="x",
        export_limit=ExportLimit(10.0))
    ledger.reject_change("pv-1", proposal.version, rejected_by="ops-mgr")
    assert ledger.settings_at("pv-1", 5000.0).export_limit.max_export_kw == 60.0
    # A rejected proposal does not block a new one.
    again = ledger.propose_change("pv-1", at=5100.0, actor="planner",
                                  reason="y", export_limit=ExportLimit(20.0))
    assert again.version == 3 and again.status == STATUS_PROPOSED


def test_point_in_time_reconstruction():
    ledger = build_ledger()
    register(ledger)
    v2 = ledger.propose_change("pv-1", at=2000.0, actor="planner",
                               reason="a", export_limit=ExportLimit(40.0))
    ledger.approve_change("pv-1", v2.version, approver="ops-mgr", at=3000.0)
    v3 = ledger.propose_change("pv-1", at=4000.0, actor="planner",
                               reason="b", export_limit=ExportLimit(20.0))
    ledger.approve_change("pv-1", v3.version, approver="ops-mgr", at=5000.0)

    assert ledger.settings_at("pv-1", 2999.0).version == 1
    assert ledger.settings_at("pv-1", 3000.0).version == 2
    assert ledger.settings_at("pv-1", 4999.0).version == 2
    assert ledger.settings_at("pv-1", 5000.0).version == 3
    assert ledger.settings_at("pv-1", 999.0) is None  # before interconnection
    current = ledger.settings_at("pv-1", 99999.0)
    assert current.version == 3 and current.export_limit.max_export_kw == 20.0
    assert [h.version for h in ledger.history("pv-1")] == [1, 2, 3]


def test_commissioning_requires_passing_test():
    ledger = build_ledger()
    register(ledger)
    with pytest.raises(ValueError, match="no_passing_commissioning_test"):
        ledger.commission("pv-1", 1500.0)
    ledger.add_test("pv-1", CommissioningTest("t1", 1400.0, False, ("trip",)))
    with pytest.raises(ValueError, match="no_passing_commissioning_test"):
        ledger.commission("pv-1", 1500.0)
    ledger.add_test("pv-1", CommissioningTest("t2", 1450.0, True,
                                              ("trip", "reconnect", "export")))
    record = ledger.commission("pv-1", 1500.0)
    assert record.state == STATE_COMMISSIONED
    assert [t.test_id for t in record.tests] == ["t1", "t2"]


def test_retired_resource_excluded_from_restoration_at_and_after_retirement():
    ledger = build_ledger()
    register(ledger, rid="pv-old")
    ledger.add_test("pv-old", CommissioningTest("t", 1400.0, True))
    ledger.commission("pv-old", 1500.0)
    register(ledger, rid="pv-new")
    ledger.add_test("pv-new", CommissioningTest("t", 1400.0, True))
    ledger.commission("pv-new", 1500.0)

    assert ledger.eligible_for_restoration("pv-old", at=5000.0)
    ledger.retire("pv-old", retired_at=6000.0, reason="decommissioned")
    assert not ledger.eligible_for_restoration("pv-old")
    assert ledger.eligible_for_restoration("pv-old", at=5999.0)
    assert not ledger.eligible_for_restoration("pv-old", at=6000.0)
    assert ledger.eligible_for_restoration("pv-new", at=6000.0)
    assert ledger.restoration_candidates(["pv-old", "pv-new"], at=6000.0) == ["pv-new"]


def test_retired_resource_cannot_be_reconfigured():
    ledger = build_ledger()
    register(ledger)
    ledger.add_test("pv-1", CommissioningTest("t", 1400.0, True))
    ledger.commission("pv-1", 1500.0)
    ledger.retire("pv-1", 6000.0)
    with pytest.raises(ValueError, match="retired"):
        ledger.propose_change("pv-1", at=7000.0, actor="planner",
                              reason="late", export_limit=ExportLimit(10.0))
    with pytest.raises(ValueError, match="retired"):
        ledger.add_test("pv-1", CommissioningTest("t2", 7000.0, True))


def test_retirement_closes_effective_interval():
    ledger = build_ledger()
    register(ledger)
    ledger.retire("pv-1", 6000.0, reason="removed")
    assert ledger.settings_at("pv-1", 5999.0).version == 1
    assert ledger.settings_at("pv-1", 6000.0) is None
    assert ledger.records["pv-1"].state == STATE_RETIRED


def test_retirement_rejects_pending_change():
    ledger = build_ledger()
    register(ledger)
    proposal = ledger.propose_change("pv-1", at=5000.0, actor="planner",
                                     reason="late", export_limit=ExportLimit(10.0))
    ledger.retire("pv-1", 6000.0)
    history = {v.version: v for v in ledger.history("pv-1")}
    assert history[proposal.version].status == STATUS_REJECTED
    assert ledger.settings_at("pv-1", 5999.0).version == 1


def test_cannot_approve_change_effective_before_current_version():
    ledger = build_ledger()
    register(ledger)
    v2 = ledger.propose_change("pv-1", at=2000.0, actor="planner",
                               reason="a", export_limit=ExportLimit(40.0))
    ledger.approve_change("pv-1", v2.version, approver="ops-mgr", at=3000.0)
    v3 = ledger.propose_change("pv-1", at=4000.0, actor="planner",
                               reason="b", export_limit=ExportLimit(20.0))
    with pytest.raises(ValueError, match="effective_before_current"):
        ledger.approve_change("pv-1", v3.version, approver="ops-mgr", at=2500.0)


def test_feeder_reassignment_requires_feeder_scope():
    ledger = build_ledger()
    register(ledger)
    proposal = ledger.propose_change("pv-1", at=2000.0, actor="planner",
                                     reason="moved", feeder_id="F2",
                                     connection_node="B-88")
    # ops-mgr does not hold the feeder scope.
    with pytest.raises(ValueError, match="scope_insufficient"):
        ledger.approve_change("pv-1", proposal.version,
                              approver="ops-mgr", at=2100.0)
    ledger.add_approver("planner-approver",
                        frozenset({SCOPE_INVERTER, SCOPE_EXPORT,
                                   SCOPE_PROTECTION, "feeder"}))
    ledger.approve_change("pv-1", proposal.version,
                          approver="planner-approver", at=2100.0)
    assert ledger.settings_at("pv-1", 2100.0).feeder_id == "F2"


def test_grid_forming_mode_for_bess():
    ledger = build_ledger()
    register(ledger, rid="bess-1", profile_id="inv-bess-100",
             mode=INVERTER_GRID_FOLLOWING)
    proposal = ledger.propose_change(
        "bess-1", at=2000.0, actor="planner", reason="island support",
        inverter_mode=INVERTER_GRID_FORMING)
    ledger.approve_change("bess-1", proposal.version,
                          approver="ops-mgr", at=2100.0)
    assert ledger.settings_at("bess-1", 2200.0).inverter_mode == INVERTER_GRID_FORMING


def test_restoration_service_drops_retired_resource():
    ledger = build_ledger()
    register(ledger, rid="pv-1")
    ledger.add_test("pv-1", CommissioningTest("t", 1400.0, True))
    ledger.commission("pv-1", 1500.0)
    active = Resource("pv-1", 80.0, grid_forming=False)
    ghost = Resource("pv-1", 80.0)  # same id, ledger state governs

    request = RestorationRequest(
        capacity_kw=200.0, load_blocks=(LoadBlock("b1", 100.0, 5),),
        switching_candidates=(), resources=(active,),
        ledger=ledger, plan_at=5000.0)
    svc = RestorationService()
    assert svc.plan(request)["island"].has_grid_former is False

    ledger.retire("pv-1", 6000.0)
    retired_request = RestorationRequest(
        capacity_kw=200.0, load_blocks=(LoadBlock("b1", 100.0, 5),),
        switching_candidates=(), resources=(ghost,),
        ledger=ledger, plan_at=7000.0)
    assert svc.plan(retired_request)["island"] is None
    assert svc.excluded_resources(retired_request) == ["pv-1"]
    # Without a ledger the service behaves as before (backward compatible).
    plain = RestorationRequest(200.0, (LoadBlock("b1", 100.0, 5),), (), (ghost,))
    assert svc.plan(plain)["island"] is not None

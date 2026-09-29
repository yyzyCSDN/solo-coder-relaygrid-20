"""分布式电源接入台账测试 (DER access ledger tests)."""
import pytest

from relaygrid.der.inverters import InverterCapability
from relaygrid.der.islanding import assess_island
from relaygrid.der.ledger import (
    AntiIslandingSettings,
    CommissioningTest,
    ConfigState,
    DERLedger,
    ExportLimit,
    InverterMode,
)


def make_capability(kva=500.0, kw_available=480.0, kvar_limit=300.0, grid_forming_capable=False):
    return InverterCapability(kva, kw_available, kvar_limit, grid_forming_capable)


def make_anti_islanding():
    return AntiIslandingSettings(
        v_max_pu=1.10, v_min_pu=0.88, f_max_hz=50.5, f_min_hz=49.5, trip_time_s=0.2
    )


def make_export(kw=400.0, allowed=True):
    return ExportLimit(kw, allowed)


def test_register_creates_active_version():
    ledger = DERLedger()
    cap = make_capability()
    cfg = ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    assert cfg.version == 1
    assert cfg.state == ConfigState.ACTIVE
    assert ledger.current_config("pv1").feeder_id == "feeder_a"


def test_change_workflow_check_approve_activate():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    draft = ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    assert draft.state == ConfigState.DRAFT
    ledger.check_capability("pv1", checker="checker1")
    approved = ledger.approve("pv1", approver="approver1")
    assert approved.state == ConfigState.APPROVED
    active = ledger.activate("pv1", at=100.0)
    assert active.state == ConfigState.ACTIVE
    assert active.effective_from == 100.0
    old = ledger.get_version("pv1", 1)
    assert old.effective_to == 100.0
    assert ledger.current_config("pv1").version == 2


def test_settings_at_reconstructs_effective_settings():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(400.0), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    ledger.check_capability("pv1", "checker1")
    ledger.approve("pv1", "approver1")
    ledger.activate("pv1", at=100.0)
    assert ledger.settings_at("pv1", 50.0).export_limit.max_export_kw == 400.0
    assert ledger.settings_at("pv1", 100.0).export_limit.max_export_kw == 300.0
    assert ledger.settings_at("pv1", 150.0).export_limit.max_export_kw == 300.0


def test_multiple_changes_temporal_reconstruction():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(400.0), cap, at=0.0, author="eng",
    )
    for kw, at, c, a in [(300.0, 100.0, "c1", "a1"), (250.0, 200.0, "c2", "a2")]:
        ledger.propose_change("pv1", export_limit=make_export(kw), author="eng")
        ledger.check_capability("pv1", c)
        ledger.approve("pv1", a)
        ledger.activate("pv1", at=at)
    assert ledger.settings_at("pv1", 50.0).export_limit.max_export_kw == 400.0
    assert ledger.settings_at("pv1", 150.0).export_limit.max_export_kw == 300.0
    assert ledger.settings_at("pv1", 250.0).export_limit.max_export_kw == 250.0


def test_feeder_attribution_change():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", feeder_id="feeder_b", author="eng")
    ledger.check_capability("pv1", "c1")
    ledger.approve("pv1", "a1")
    ledger.activate("pv1", at=100.0)
    assert ledger.settings_at("pv1", 50.0).feeder_id == "feeder_a"
    assert ledger.settings_at("pv1", 150.0).feeder_id == "feeder_b"


def test_capability_violation_export_exceeds_available():
    ledger = DERLedger()
    cap = make_capability(kva=500.0, kw_available=480.0)
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(400.0), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(600.0), author="eng")
    with pytest.raises(ValueError):
        ledger.check_capability("pv1", "checker1")


def test_grid_forming_requires_capable_inverter():
    ledger = DERLedger()
    cap = make_capability(grid_forming_capable=False)
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", inverter_mode=InverterMode.GRID_FORMING, author="eng")
    with pytest.raises(ValueError):
        ledger.check_capability("pv1", "checker1")


def test_checker_and_approver_must_be_independent():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    with pytest.raises(ValueError):
        ledger.check_capability("pv1", checker="eng")
    ledger.check_capability("pv1", "checker1")
    with pytest.raises(ValueError):
        ledger.approve("pv1", approver="eng")
    with pytest.raises(ValueError):
        ledger.approve("pv1", approver="checker1")


def test_cannot_propose_change_with_pending_change():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    with pytest.raises(ValueError):
        ledger.propose_change("pv1", export_limit=make_export(200.0), author="eng2")


def test_retirement_records_and_excludes_from_restoration():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(400.0), cap, at=0.0, author="eng",
    )
    ledger.register(
        "pv2", "feeder_a", InverterMode.GRID_FORMING,
        make_anti_islanding(), make_export(200.0),
        make_capability(grid_forming_capable=True), at=0.0, author="eng",
    )
    rec = ledger.retire("pv1", at=100.0, reason="end_of_life", retired_by="eng")
    assert rec.retired_at == 100.0
    assert ledger.retirement("pv1").reason == "end_of_life"
    assert ledger.settings_at("pv1", 150.0) is None
    res_before = ledger.resources(at=50.0)
    assert "pv1" in [r.resource_id for r in res_before]
    res_after = ledger.resources(at=150.0)
    ids = [r.resource_id for r in res_after]
    assert "pv1" not in ids
    assert "pv2" in ids


def test_retired_resource_cannot_enter_restoration():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.assert_restoration_eligible("pv1", at=50.0)
    ledger.retire("pv1", at=100.0, reason="end_of_life", retired_by="eng")
    with pytest.raises(ValueError):
        ledger.assert_restoration_eligible("pv1", at=150.0)


def test_cannot_retire_with_pending_change():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    with pytest.raises(ValueError):
        ledger.retire("pv1", at=100.0, reason="end_of_life", retired_by="eng")


def test_commissioning_test_attached_to_version():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    test = CommissioningTest(
        "t1", "anti_islanding", performed_at=50.0, result="passed", tester="tech"
    )
    ledger.add_test("pv1", test)
    ledger.check_capability("pv1", "checker1")
    ledger.approve("pv1", "approver1")
    active = ledger.activate("pv1", at=100.0)
    assert len(active.tests) == 1
    assert active.tests[0].test_id == "t1"


def test_resources_build_restoration_resources_and_integrate_with_islanding():
    ledger = DERLedger()
    cap = make_capability(grid_forming_capable=True)
    ledger.register(
        "bat1", "feeder_a", InverterMode.GRID_FORMING,
        make_anti_islanding(), make_export(200.0), cap, at=0.0, author="eng",
    )
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(100.0), cap, at=0.0, author="eng",
    )
    ledger.retire("pv1", at=100.0, reason="end_of_life", retired_by="eng")
    res = ledger.resources(at=150.0)
    ids = [r.resource_id for r in res]
    assert "pv1" not in ids
    assert "bat1" in ids
    assessment = assess_island(150.0, list(res))
    assert assessment.feasible


def test_export_disabled_means_zero_available():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), ExportLimit(0.0, False), cap, at=0.0, author="eng",
    )
    res = ledger.resources(at=50.0)
    assert res[0].available_kw == 0.0


def test_history_returns_all_versions():
    ledger = DERLedger()
    cap = make_capability()
    ledger.register(
        "pv1", "feeder_a", InverterMode.GRID_FOLLOWING,
        make_anti_islanding(), make_export(), cap, at=0.0, author="eng",
    )
    ledger.propose_change("pv1", export_limit=make_export(300.0), author="eng")
    ledger.check_capability("pv1", "c1")
    ledger.approve("pv1", "a1")
    ledger.activate("pv1", at=100.0)
    hist = ledger.history("pv1")
    assert [c.version for c in hist] == [1, 2]

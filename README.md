# RelayGrid

RelayGrid is a deterministic Python toolkit for distribution-grid operations.
It covers feeder topology, protection, voltage assessment, DER islanding,
switching safety, outage accounting, crew logistics, asset thermal limits,
telemetry quality, storage dispatch, and restoration sequencing.

The package is self-contained and uses only the Python standard library.

## Install and run

```bash
python -m pip install -e ".[test]"
relaygrid demo
python -m pytest -q
```

The demo builds a four-bus feeder with two open ties, reports its energized
state, safely closes the sectionalizing branch, and emits a JSON summary.

## Public API

The top-level package exports the feeder data model and `GridSimulator`:

```python
from relaygrid import Branch, Bus, FeederSnapshot, GridSimulator, SwitchState
```

Domain modules remain independently importable so applications can combine
topology, protection, telemetry, restoration, reliability, planning, and field
operations without depending on a web framework or database.

## Distributed-resource interconnection ledger

`relaygrid.der.ledger.InterconnectionLedger` tracks every distributed
resource from registration through commissioning to retirement:

- feeder ownership and connection node, inverter mode (grid-following /
  grid-forming), anti-islanding voltage/frequency/trip/reconnect settings,
  export limits, and commissioning tests;
- configuration changes are checked against the inverter capability profile
  (supported modes, voltage/frequency envelopes, trip-time range, export
  capability, feeder registry) and require an approver whose scopes cover
  every affected domain — self-approval is rejected;
- approved versions form non-overlapping effective intervals, so
  `ledger.settings_at(resource_id, t)` reconstructs exactly the settings in
  force at any point in time;
- `retire()` closes the current interval and, together with
  `eligible_for_restoration()`, bars the resource from restoration plans at
  and after retirement. Passing `ledger=...` and `plan_at=...` on
  `RestorationRequest` makes `RestorationService` filter resources
  automatically; `excluded_resources()` reports what was dropped.

## License

RelayGrid is released under the MIT License.

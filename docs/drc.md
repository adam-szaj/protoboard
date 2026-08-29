# DRC — Design Rule Check

```python
class DRCEngine:
    def check(self, project: Project) -> list[DRCIssue]: ...
    def check_with_graph(self, project: Project,
                        graph: ConnectivityGraph) -> list[DRCIssue]: ...

class DRCIssue:
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    locations: list[Point]
    affected_nets: list[UUID]
    affected_pins: list[PinRef]
    affected_wires: list[UUID]
    affected_components: list[UUID]
    axis_labels: tuple[str, str] = ("X", "Y")
```

## Implemented checks

| Code | Severity | Description |
|---|---|---|
| `OVERLAP` | error | Two components share a pad and at least one conflicting package pin is not bendable. |
| `SHORT` | error | Pads of different nets are electrically connected. |
| `FLOATING_PIN` | warning | A placed component has a pin with no `PinRef` in any net. |
| `BARE_WIRE_CROSS` | error | Two bare wires of different nets cross at the same point. |
| `BARE_WIRE_INTERIOR_CROSS` | error | A bare wire's *interior* (mid-segment) crosses a different-net bare wire. |
| `MISSING_NET` | error | A net from the schematic isn't fully realised on the board. |
| `OFF_PAD_JOINT` | error | A `JointKnot` sits at a position with no underlying pad. |
| `OFF_BOARD_PIN` | error | A component pin lands off the board outline. |
| `GROUP_INVALID_EXPORT` | error | `G.exported_pins` contains a pin that is neither a direct member of `G` nor re-exported by a direct child group. |
| `GROUP_MISSING_EXPORT` | warning | A net crosses the group boundary but no pin on either side is exposed via the export chain. |
| `GROUP_BOGUS_EXPORT` | warning | An exported pin belongs to a net that is internal to the group (doesn't reach outside). |
| `WIRE_PIN_OFF_PLATED_THROUGH` | error | A `WirePinKnot` sits on a non-plated-through pad. |
| `HALF_ORPHAN_WIRE` | warning | A wire endpoint is a free `JointKnot` (no pin, no mate, no `WirePinKnot`) — the wire has no deliberate terminal. Rule 9 from [pad-pin-wire-junction-semantics.md](pad-pin-wire-junction-semantics.md). |

## Performance

`DRCEngine.check_with_graph()` accepts an already-built `ConnectivityGraph`
instead of building one from scratch. `session.connectivity_graph()` returns
a cached graph that is invalidated whenever the layout changes
(`session.invalidate_graph()`), so a repeated DRC pass on an unchanged
layout costs ~1.5 ms instead of ~7.8 ms.

Empty stripboard nodes (a strip with no pins attached) are ignored by DRC —
they do not produce `FLOATING_PIN`.

See [performance.md](performance.md) for the history of the DRC speed-ups.

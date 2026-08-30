# SPDX-License-Identifier: MIT
"""Tests for command recording + headless replay."""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import CommandStack, MacroCommand
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    AddWireWithNetsCommand,
    MoveComponentCommand,
    PlaceComponentCommand,
    RemoveWireCommand,
    SetComponentPackageVariantCommand,
    SetWireSideCommand,
)
from protoboard.domain.commands.net_cmds import (
    CreateNetCommand,
    RenameNetCommand,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.recording import (
    CommandRecorder,
    deserialize_command,
    load_recording,
    replay_commands,
    serialize_command,
)


def _empty_board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(),
            ),
            rows=4,
            cols=4,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


class _State:
    """Minimal state satisfying both ComponentState and SchematicState."""

    def __init__(self) -> None:
        self.board = _empty_board()
        self.layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
        self.schematic = Schematic(nets={})
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        from protoboard.domain.components.definition import ComponentDefinition

        self.components: dict[str, ComponentDefinition] = {}


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerializationRoundtrip:
    def test_place_component(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(1.0, 2.0),
            rotation=90,
            side="top",
        )
        inst = ComponentInstance(
            id=comp_id,
            definition_ref="R",
            refdes="R1",
            value="10k",
        )
        cmd = PlaceComponentCommand(
            component_id=comp_id,
            placement=pl,
            instance=inst,
        )
        d = serialize_command(cmd)
        assert d["type"] == "place_component"
        restored = deserialize_command(d)
        assert isinstance(restored, PlaceComponentCommand)
        assert restored.component_id == comp_id
        assert restored.placement.position == Point(1.0, 2.0)
        assert restored.placement.rotation == 90
        assert restored.instance.refdes == "R1"

    def test_move_component(self) -> None:
        comp_id = uuid.uuid4()
        old = ComponentPlacement(
            component_id=comp_id, position=Point(0, 0), rotation=0, side="top"
        )
        new = ComponentPlacement(
            component_id=comp_id, position=Point(2, 1), rotation=0, side="top"
        )
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new,
            old_placement=old,
            skip_conflict_check=True,
        )
        d = serialize_command(cmd)
        restored = deserialize_command(d)
        assert isinstance(restored, MoveComponentCommand)
        assert restored.new_placement.position == Point(2, 1)
        assert restored.skip_conflict_check is True

    def test_set_package_variant(self) -> None:
        # Regression: the body-variant picker fires this command; without a
        # registered serializer the recorder skipped it (recorder.skip_unknown).
        comp_id = uuid.uuid4()
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant="SMD-0603/typ",
        )
        d = serialize_command(cmd)
        assert d["type"] == "set_package_variant"
        restored = deserialize_command(d)
        assert isinstance(restored, SetComponentPackageVariantCommand)
        assert restored.component_id == comp_id
        assert restored.new_variant == "SMD-0603/typ"

    def test_set_package_variant_none_clears(self) -> None:
        comp_id = uuid.uuid4()
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id,
            new_variant=None,
        )
        restored = deserialize_command(serialize_command(cmd))
        assert isinstance(restored, SetComponentPackageVariantCommand)
        assert restored.new_variant is None

    def test_wire(self) -> None:
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0), Point(1, 1)],
            insulated=True,
            side="top",
        )
        cmd = AddWireCommand(wire=wire)
        d = serialize_command(cmd)
        restored = deserialize_command(d)
        assert isinstance(restored, AddWireCommand)
        assert restored.wire.id == wire.id
        assert restored.wire.path == wire.path

    def test_add_wire_with_nets_downgrades_to_add_wire(self) -> None:
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 1)],
            insulated=False,
            side="bottom",
        )
        cmd = AddWireWithNetsCommand(wire=wire)
        d = serialize_command(cmd)
        # Net mutations are tracked separately; replay re-adds the wire only.
        restored = deserialize_command(d)
        assert isinstance(restored, AddWireCommand)
        assert restored.wire.id == wire.id

    def test_macro(self) -> None:
        wire1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0)],
            insulated=True,
            side="top",
        )
        wire2 = Wire(
            id=uuid.uuid4(),
            path=[Point(1, 0), Point(1, 1)],
            insulated=False,
            side="bottom",
        )
        macro = MacroCommand(
            [AddWireCommand(wire=wire1), AddWireCommand(wire=wire2)],
            description="Two wires",
        )
        d = serialize_command(macro)
        restored = deserialize_command(d)
        assert isinstance(restored, MacroCommand)
        assert restored.description == "Two wires"

    def test_net_commands(self) -> None:
        net_id = uuid.uuid4()
        cmd = CreateNetCommand(name="N1", net_id=net_id)
        restored = deserialize_command(serialize_command(cmd))
        assert isinstance(restored, CreateNetCommand)
        assert restored.net_id == net_id

        rename = RenameNetCommand(net_id=net_id, new_name="VCC")
        rrestored = deserialize_command(serialize_command(rename))
        assert isinstance(rrestored, RenameNetCommand)
        assert rrestored.new_name == "VCC"


# ---------------------------------------------------------------------------
# Registry totality — every concrete Command must be serializable
# ---------------------------------------------------------------------------


def _all_concrete_command_classes() -> set[type]:
    """Every concrete production Command subclass, discovered recursively.

    Imports every module under ``domain.commands`` first so all subclasses are
    registered on ``Command.__subclasses__()``.  Restricted to classes defined
    in the ``protoboard.domain.commands`` package — throwaway ``Command``
    subclasses that other test modules define (``_UnknownCmd``, ``_IncrCmd`` …)
    also live on ``__subclasses__()`` once their module has imported, and must
    not count as production gaps (this test's result would otherwise depend on
    collection order).
    """
    import importlib
    import pkgutil

    import protoboard.domain.commands as cmd_pkg
    from protoboard.domain.commands.base import Command

    for mod in pkgutil.iter_modules(cmd_pkg.__path__):
        importlib.import_module(f"protoboard.domain.commands.{mod.name}")

    def walk(cls: type) -> set[type]:
        out: set[type] = set()
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("protoboard.domain.commands"):
                out.add(sub)
            out |= walk(sub)
        return out

    return walk(Command)


class TestSerializationTotality:
    """Every concrete Command a user action can put on the stack must be
    registered for serialization — otherwise the recorder silently skips it
    (``test_unknown_command_is_skipped``) and headless replay diverges from
    the original session.

    New commands that reach ``CommandStack.execute`` (directly or as a
    ``MacroCommand`` member — the macro serializer recurses) MUST either be
    registered in ``serialization.py`` or added to ``_INTENTIONALLY_UNREGISTERED``
    below with a one-line reason.  A bare new command fails this test.
    """

    # Concrete Command subclasses that are deliberately NOT serialized, each
    # with the reason it can never reach the recorder as a stack command.
    _INTENTIONALLY_UNREGISTERED: dict[str, str] = {
        # Abstract intermediate bases — never instantiated directly; their
        # concrete subclasses are registered.  (Not caught by inspect.isabstract
        # because they don't declare abstractmethods, only override contracts.)
        "ConditionalCommand": "abstract base; subclasses are registered",
        "GroupMembershipCommand": "abstract base; subclasses are registered",
        # Dead legacy command — superseded by the drag pipeline
        # (MoveDragSetCommand); no constructor call exists anywhere in src/.
        "TranslateAnchoredWiresCommand": "dead code; never constructed",
    }

    def test_every_concrete_command_is_registered_or_excused(self) -> None:
        from protoboard.services.recording.serialization import _TYPE_TO_NAME

        registered = {t.__name__ for t in _TYPE_TO_NAME}
        excused = set(self._INTENTIONALLY_UNREGISTERED)
        concrete = {c.__name__ for c in _all_concrete_command_classes()}

        missing = sorted(concrete - registered - excused)
        assert missing == [], (
            "Concrete Command subclasses with no serializer and no documented "
            f"exclusion: {missing}. Register them in serialization.py, or add "
            "them to _INTENTIONALLY_UNREGISTERED with a reason."
        )

    def test_exclusions_are_still_real_unregistered_classes(self) -> None:
        # Guard the guard: an excused name that becomes registered (or is
        # deleted) should be pruned from the exclusion list so it can't mask a
        # future regression.
        from protoboard.services.recording.serialization import _TYPE_TO_NAME

        registered = {t.__name__ for t in _TYPE_TO_NAME}
        concrete = {c.__name__ for c in _all_concrete_command_classes()}
        for name in self._INTENTIONALLY_UNREGISTERED:
            assert name in concrete, (
                f"{name!r} in _INTENTIONALLY_UNREGISTERED is not a concrete "
                "Command subclass any more — remove the stale exclusion."
            )
            assert name not in registered, (
                f"{name!r} is now registered — remove it from "
                "_INTENTIONALLY_UNREGISTERED."
            )


# ---------------------------------------------------------------------------
# Recorder behavior
# ---------------------------------------------------------------------------


class TestNewlyRegisteredRoundtrip:
    """Round-trip coverage for the commands registered to close the recorder
    blind spot (drag / transform / drop / fuse / wire-pin convert / group flip).
    Each was previously skipped silently, diverging replay.
    """

    def test_convert_endpoint_to_wire_pin(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )

        wid, nid = uuid.uuid4(), uuid.uuid4()
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=wid,
            endpoint="b",
            net_id=nid,
        )
        d = serialize_command(cmd)
        assert d["type"] == "convert_endpoint_to_wire_pin"
        r = deserialize_command(d)
        assert isinstance(r, ConvertEndpointToWirePinCommand)
        assert r.wire_id == wid and r.endpoint == "b" and r.net_id == nid

    def test_convert_endpoint_none_net(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )

        cmd = ConvertEndpointToWirePinCommand(
            wire_id=uuid.uuid4(),
            endpoint="a",
            net_id=None,
        )
        r = deserialize_command(serialize_command(cmd))
        assert isinstance(r, ConvertEndpointToWirePinCommand)
        assert r.net_id is None

    def test_convert_wire_pin_to_endpoint(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            ConvertWirePinToEndpointCommand,
        )

        wpid = uuid.uuid4()
        r = deserialize_command(
            serialize_command(ConvertWirePinToEndpointCommand(wire_pin_id=wpid))
        )
        assert isinstance(r, ConvertWirePinToEndpointCommand)
        assert r.wire_pin_id == wpid

    def test_drop_wires(self) -> None:
        from protoboard.domain.commands.component_cmds import DropWiresCommand

        ids = frozenset({uuid.uuid4(), uuid.uuid4()})
        r = deserialize_command(serialize_command(DropWiresCommand(wire_ids=ids)))
        assert isinstance(r, DropWiresCommand)
        assert r.wire_ids == ids

    def test_fuse_touching_wires_scoped_and_whole_layout(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
        )

        ids = frozenset({uuid.uuid4()})
        scoped = deserialize_command(
            serialize_command(FuseTouchingWiresCommand(wire_ids=ids))
        )
        assert isinstance(scoped, FuseTouchingWiresCommand)
        assert scoped.wire_ids == ids
        whole = deserialize_command(
            serialize_command(FuseTouchingWiresCommand(wire_ids=None))
        )
        assert isinstance(whole, FuseTouchingWiresCommand)
        assert whole.wire_ids is None

    def test_flip_group(self) -> None:
        from protoboard.domain.commands.group_cmds import FlipGroupCommand

        gid = uuid.uuid4()
        cmd = FlipGroupCommand(group_id=gid, axis="v", pivot=Point(3.0, 4.0))
        d = serialize_command(cmd)
        assert d["type"] == "flip_group"
        r = deserialize_command(d)
        assert isinstance(r, FlipGroupCommand)
        assert r.group_id == gid and r.axis == "v"
        assert r.pivot == Point(3.0, 4.0)

    def test_flip_group_default_pivot(self) -> None:
        from protoboard.domain.commands.group_cmds import FlipGroupCommand

        r = deserialize_command(
            serialize_command(FlipGroupCommand(group_id=uuid.uuid4(), axis="h"))
        )
        assert isinstance(r, FlipGroupCommand)
        assert r.axis == "h" and r.pivot is None

    def test_set_appearance(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            SetComponentAppearanceCommand,
        )
        from protoboard.domain.components.appearance import Appearance

        cid = uuid.uuid4()
        app = Appearance(color="#1a1a1a80", corner_radius_mm=(0.5, 0.0, 0.5, 0.0))
        cmd = SetComponentAppearanceCommand(component_id=cid, new_appearance=app)
        d = serialize_command(cmd)
        assert d["type"] == "set_appearance"
        # Serialized as the canonical style string (matches the YAML authoring).
        assert d["style"] == app.to_style_string()
        r = deserialize_command(d)
        assert isinstance(r, SetComponentAppearanceCommand)
        assert r.component_id == cid
        assert r.new_appearance == app

    def test_set_appearance_none_clears(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            SetComponentAppearanceCommand,
        )

        r = deserialize_command(
            serialize_command(
                SetComponentAppearanceCommand(
                    component_id=uuid.uuid4(),
                    new_appearance=None,
                )
            )
        )
        assert isinstance(r, SetComponentAppearanceCommand)
        assert r.new_appearance is None

    def test_move_drag_set(self) -> None:
        from protoboard.domain.commands.drag_cmds import MoveDragSetCommand
        from protoboard.geometry import Vector

        cid = uuid.uuid4()
        old = ComponentPlacement(
            component_id=cid,
            position=Point(0, 0),
            rotation=0,
            side="top",
        )
        new = ComponentPlacement(
            component_id=cid,
            position=Point(2, 3),
            rotation=0,
            side="top",
        )
        wid = uuid.uuid4()
        cmd = MoveDragSetCommand(
            members={cid: (old, new)},
            internal_wire_ids=frozenset({wid}),
            dropped_wire_ids=frozenset(),
            delta=Vector(2.0, 3.0),
        )
        d = serialize_command(cmd)
        assert d["type"] == "move_drag_set"
        r = deserialize_command(d)
        assert isinstance(r, MoveDragSetCommand)
        assert set(r.members) == {cid}
        assert r.members[cid][1].position == Point(2, 3)
        assert r.internal_wire_ids == frozenset({wid})
        assert r.delta == Vector(2.0, 3.0)

    def test_transform_drag_set(self) -> None:
        from protoboard.domain.commands.drag_cmds import TransformDragSetCommand
        from protoboard.domain.layout.wire import Fragment

        cid, wid, kid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        old = ComponentPlacement(
            component_id=cid,
            position=Point(0, 0),
            rotation=0,
            side="top",
        )
        new = ComponentPlacement(
            component_id=cid,
            position=Point(0, 0),
            rotation=90,
            side="top",
        )
        cmd = TransformDragSetCommand(
            members={cid: (old, new)},
            wire_paths={wid: ([Point(0, 0), Point(1, 0)], [Point(0, 0), Point(0, 1)])},
            wire_fragments={
                wid: (
                    [Fragment(path=[Point(0, 0), Point(1, 0)], side="top")],
                    [Fragment(path=[Point(0, 0), Point(0, 1)], side="top")],
                )
            },
            knot_positions={kid: (Point(1, 0), Point(0, 1))},
        )
        d = serialize_command(cmd)
        assert d["type"] == "transform_drag_set"
        r = deserialize_command(d)
        assert isinstance(r, TransformDragSetCommand)
        assert r.members[cid][1].rotation == 90
        assert r.wire_paths[wid][1] == [Point(0, 0), Point(0, 1)]
        assert r.wire_fragments[wid][1][0].path == [Point(0, 0), Point(0, 1)]
        assert r.knot_positions[kid] == (Point(1, 0), Point(0, 1))


class TestCommandRecorder:
    def test_records_execute_undo_redo(self, tmp_path: Path) -> None:
        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")
        state = _State()
        stack: CommandStack = CommandStack()
        stack.set_recorder(rec)

        net_id = uuid.uuid4()
        stack.execute(CreateNetCommand(name="N1", net_id=net_id), state)
        stack.execute(RenameNetCommand(net_id=net_id, new_name="VCC"), state)
        stack.undo(state)
        stack.redo(state)

        rec.stop()
        events = load_recording(tmp_path / "cmds.yaml")
        kinds = [e["kind"] for e in events]
        assert kinds == ["execute", "execute", "undo", "redo"]
        assert events[0]["command"]["type"] == "create_net"
        assert events[1]["command"]["type"] == "rename_net"

    def test_inactive_recorder_is_noop(self) -> None:
        rec = CommandRecorder()
        # Never called start() — recorder should silently ignore.
        rec.record_execute(CreateNetCommand(name="N1"))
        rec.record_undo()
        assert rec.events == []

    def test_flush_recreates_deleted_project_dir(self, tmp_path: Path) -> None:
        # Regression: the project directory can vanish while the app is open
        # (deleted / moved).  stop() -> _flush() must recreate the parent and
        # not raise FileNotFoundError — that exception used to escape the Qt
        # closeEvent and leave the app un-quittable.
        import shutil

        proj = tmp_path / "reference_big_project"
        proj.mkdir()
        target = proj / "session_commands.yaml"

        rec = CommandRecorder()
        rec.start(target)
        shutil.rmtree(proj)
        assert not proj.exists()

        rec.stop()  # must not raise

        assert target.exists()

    def test_stop_swallows_unwritable_path(self, tmp_path: Path) -> None:
        # If the target genuinely cannot be written (e.g. parent is a file,
        # so mkdir also fails), stop() still must not raise.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        target = blocker / "session_commands.yaml"

        rec = CommandRecorder()
        rec._path = target  # bypass start()'s own flush
        rec.stop()  # must not raise even though the write is impossible

    def test_unknown_command_is_skipped(self, tmp_path: Path) -> None:
        from protoboard.domain.commands.base import Command

        class _UnknownCmd(Command):  # type: ignore[type-arg]
            description = "Unknown"

            def apply(self, _state):  # type: ignore[no-untyped-def]
                pass

            def revert(self, _state):  # type: ignore[no-untyped-def]
                pass

        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")
        rec.record_execute(_UnknownCmd())
        # Should not have appended the unknown command.
        assert rec.events == []


# ---------------------------------------------------------------------------
# End-to-end replay
# ---------------------------------------------------------------------------


class TestReplay:
    def test_replay_reproduces_state(self, tmp_path: Path) -> None:
        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")

        live = _State()
        stack: CommandStack = CommandStack()
        stack.set_recorder(rec)

        # Place a component, add a wire, rename a net.
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id, position=Point(1, 1), rotation=0, side="top"
        )
        inst = ComponentInstance(
            id=comp_id, definition_ref="R", refdes="R1", value="10k"
        )
        stack.execute(
            PlaceComponentCommand(component_id=comp_id, placement=pl, instance=inst),
            live,
        )

        wire = Wire(
            id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top"
        )
        stack.execute(AddWireCommand(wire=wire), live)

        net_id = uuid.uuid4()
        stack.execute(CreateNetCommand(name="N1", net_id=net_id), live)
        stack.execute(RenameNetCommand(net_id=net_id, new_name="VCC"), live)

        rec.stop()

        # Replay onto a fresh state and verify equality of the salient fields.
        replayed = _State()
        result = replay_commands(replayed, tmp_path / "cmds.yaml", stop_on_error=True)
        assert result.skipped == []
        assert result.executed == 4

        assert comp_id in replayed.layout.placements
        assert replayed.layout.placements[comp_id].position == Point(1, 1)
        assert len(replayed.layout.wires) == 1
        assert replayed.layout.wires[0].id == wire.id
        assert net_id in replayed.schematic.nets
        assert replayed.schematic.nets[net_id].name == "VCC"

    def test_replay_honours_undo_redo(self, tmp_path: Path) -> None:
        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")

        live = _State()
        stack: CommandStack = CommandStack()
        stack.set_recorder(rec)

        wire = Wire(
            id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top"
        )
        stack.execute(AddWireCommand(wire=wire), live)
        stack.undo(live)  # remove
        # No redo — final state should have zero wires.
        rec.stop()

        replayed = _State()
        result = replay_commands(replayed, tmp_path / "cmds.yaml", stop_on_error=True)
        assert result.executed == 1
        assert result.undone == 1
        assert replayed.layout.wires == []

    def test_set_wire_side_replays(self, tmp_path: Path) -> None:
        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")

        live = _State()
        stack: CommandStack = CommandStack()
        stack.set_recorder(rec)

        wire = Wire(
            id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top"
        )
        stack.execute(AddWireCommand(wire=wire), live)
        stack.execute(SetWireSideCommand(wire_id=wire.id, side="bottom"), live)
        rec.stop()

        replayed = _State()
        replay_commands(replayed, tmp_path / "cmds.yaml", stop_on_error=True)
        assert replayed.layout.wires[0].side == "bottom"

    def test_remove_wire_replays(self, tmp_path: Path) -> None:
        rec = CommandRecorder()
        rec.start(tmp_path / "cmds.yaml")

        live = _State()
        stack: CommandStack = CommandStack()
        stack.set_recorder(rec)

        wire = Wire(
            id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top"
        )
        stack.execute(AddWireCommand(wire=wire), live)
        stack.execute(RemoveWireCommand(wire_id=wire.id), live)
        rec.stop()

        replayed = _State()
        replay_commands(replayed, tmp_path / "cmds.yaml", stop_on_error=True)
        assert replayed.layout.wires == []


# ---------------------------------------------------------------------------
# Recording lifecycle stop/start
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_restart_replaces_previous_log(self, tmp_path: Path) -> None:
        path = tmp_path / "cmds.yaml"
        rec = CommandRecorder()
        rec.start(path)
        rec.record_execute(CreateNetCommand(name="N1"))
        rec.stop()
        assert len(load_recording(path)) == 1

        rec.start(path)
        # Should start fresh — no events from previous session.
        assert rec.events == []
        rec.record_execute(CreateNetCommand(name="N2"))
        rec.stop()
        events = load_recording(path)
        assert len(events) == 1
        assert events[0]["command"]["name"] == "N2"

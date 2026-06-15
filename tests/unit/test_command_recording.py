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
    SetWireSideCommand,
)
from protoboard.domain.commands.net_cmds import (
    CreateNetCommand,
    RenameNetCommand,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
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
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                     layers=PadLayers()),
        rows=4, cols=4, row_gap=0.0, col_gap=0.0,
    ))


class _State:
    """Minimal state satisfying both ComponentState and SchematicState."""

    def __init__(self) -> None:
        self.board = _empty_board()
        self.layout = Layout(placements={}, wires=[])
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
            component_id=comp_id, position=Point(1.0, 2.0),
            rotation=90, side="top",
        )
        inst = ComponentInstance(
            id=comp_id, definition_ref="R", refdes="R1", value="10k",
        )
        cmd = PlaceComponentCommand(
            component_id=comp_id, placement=pl, instance=inst,
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
        old = ComponentPlacement(component_id=comp_id, position=Point(0, 0), rotation=0, side="top")
        new = ComponentPlacement(component_id=comp_id, position=Point(2, 1), rotation=0, side="top")
        cmd = MoveComponentCommand(
            component_id=comp_id, new_placement=new, old_placement=old,
            skip_conflict_check=True,
        )
        d = serialize_command(cmd)
        restored = deserialize_command(d)
        assert isinstance(restored, MoveComponentCommand)
        assert restored.new_placement.position == Point(2, 1)
        assert restored.skip_conflict_check is True

    def test_wire(self) -> None:
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0), Point(1, 1)],
            insulated=True, side="top",
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
            insulated=False, side="bottom",
        )
        cmd = AddWireWithNetsCommand(wire=wire)
        d = serialize_command(cmd)
        # Net mutations are tracked separately; replay re-adds the wire only.
        restored = deserialize_command(d)
        assert isinstance(restored, AddWireCommand)
        assert restored.wire.id == wire.id

    def test_macro(self) -> None:
        wire1 = Wire(
            id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)],
            insulated=True, side="top",
        )
        wire2 = Wire(
            id=uuid.uuid4(), path=[Point(1, 0), Point(1, 1)],
            insulated=False, side="bottom",
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
# Recorder behavior
# ---------------------------------------------------------------------------


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
        pl = ComponentPlacement(component_id=comp_id, position=Point(1, 1), rotation=0, side="top")
        inst = ComponentInstance(id=comp_id, definition_ref="R", refdes="R1", value="10k")
        stack.execute(PlaceComponentCommand(component_id=comp_id, placement=pl, instance=inst), live)

        wire = Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top")
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

        wire = Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top")
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

        wire = Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top")
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

        wire = Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(1, 0)], insulated=True, side="top")
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

# SPDX-License-Identifier: MIT
from protoboard.controller.tools.armed_subtool import ArmedSubTool


class _Conforming:
    def commit_op(self) -> bool:
        return True

    def cancel_op(self) -> bool:
        return False

    def has_pending_op(self) -> bool:
        return False

    def can_commit(self) -> bool:
        return True

    def on_key(self, inp: object) -> bool:
        return False

    def can_exit(self) -> bool:
        return True


def test_conforming_object_is_armed_subtool() -> None:
    assert isinstance(_Conforming(), ArmedSubTool)


def test_missing_method_is_not_armed_subtool() -> None:
    class _Partial:
        def commit_op(self) -> bool:
            return True

    assert not isinstance(_Partial(), ArmedSubTool)

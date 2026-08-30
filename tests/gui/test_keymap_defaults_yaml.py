# SPDX-License-Identifier: MIT
import textwrap

from protoboard.gui.keymap import GlobalKeyMap, KeyMap


def test_defaults_section_loads(tmp_path, monkeypatch):
    f = tmp_path / "keybindings.yaml"
    f.write_text(textwrap.dedent("""
        defaults:
          select-tool:
            action.reroute: "W"
        overrides:
          select-tool:
            action.reroute: "Q"
    """))
    import protoboard.paths as paths

    monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", f)
    km = KeyMap(GlobalKeyMap.with_globals())
    km.load_defaults()
    km.load_overrides()
    # override wins over the default
    assert km.resolve("select-tool.action.reroute") == "Q"


def test_missing_defaults_section_is_backward_compatible(tmp_path, monkeypatch):
    f = tmp_path / "keybindings.yaml"
    # old-style file: flat, no defaults:/overrides: wrappers
    f.write_text('select-tool:\n  action.reroute: "Q"\n')
    import protoboard.paths as paths

    monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", f)
    km = KeyMap(GlobalKeyMap.with_globals())
    km.load_defaults()  # no defaults: section → no-op, no crash
    km.load_overrides()  # old flat file still read as overrides
    assert km.resolve("select-tool.action.reroute") == "Q"

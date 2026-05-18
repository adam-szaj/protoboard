# Packaging

Local build scripts that produce standalone distributions of
protoboard-designer for Linux and Windows using PyInstaller.

## Linux

```bash
./packaging/build-linux.sh
```

Output:

```
dist/protoboard-designer/                          # one-folder bundle
dist/protoboard-designer-<VERSION>-linux-x86_64.tar.gz
```

Run the bundled app:

```bash
tar xzf protoboard-designer-<VERSION>-linux-x86_64.tar.gz
./protoboard-designer/protoboard-designer
```

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```

Output:

```
dist\protoboard-designer\                          # one-folder bundle
dist\protoboard-designer-<VERSION>-windows-x86_64.zip
```

Unzip and double-click `protoboard-designer.exe`.

## What gets bundled

The PyInstaller spec (`protoboard.spec`) ships:

- The Python interpreter + Qt runtime
- The `protoboard` package and its dependencies
- `library/` — built-in component library
- `assets/` — body shapes and other read-only renderer data
- `boards/` — example board layouts
- `README.md`, `CHANGELOG.md`, `LICENSE`

The app discovers these via `protoboard.paths`, which honours the
PyInstaller `sys._MEIPASS` unpack directory at runtime.

## Versioning

Both scripts read the version string from `pyproject.toml` (the `version =
"..."` line). Bump that field before building a release.

## Smoke test

Each build script invokes the produced binary with `--help` after the
PyInstaller pass to catch obvious import / packaging breakages early.

## Notes

- The PyInstaller spec uses `console=False` so on Windows no terminal
  flashes when launching the app.
- `QT_QPA_PLATFORM=xcb` is set in `protoboard/__init__.py` so the Linux
  bundle works on the default Wayland sessions without a manual override.
- Icons are deliberately omitted in this release — to be added later.

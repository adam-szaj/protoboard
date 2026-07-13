# SPDX-License-Identifier: MIT
"""Controller layer — Qt-free edit verbs, ports, and interaction tools.

Sits between the GUI (PySide6 adapters + renderers + KeyMap) and the
Domain/Services. Nothing here may import ``protoboard.gui``; the same mechanical
rule that protects ``domain/`` protects this package.
"""

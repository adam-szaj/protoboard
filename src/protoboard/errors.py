# SPDX-License-Identifier: MIT
"""Custom exceptions for the protoboard package."""


class ProtoboardError(Exception):
    """Base class for all protoboard errors."""


class LayoutError(ProtoboardError):
    """Raised when a box tree cannot be laid out."""


class SerializationError(ProtoboardError):
    """Raised when YAML (de)serialization fails."""


class ActionRegistrationError(ProtoboardError):
    """Raised when the action-registry builder cannot wire an ActionSpec
    (unknown slot, duplicate id, missing menu parent, etc.)."""

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


class UnboundInstanceError(ProtoboardError):
    """Raised when a placed component's geometry is requested but its
    :class:`ComponentInstance` was never bound to a definition.

    A placed instance must carry its definition + package catalogue (via
    ``ComponentInstance.bind``) so it can resolve the variant footprint the
    user actually chose. Reading the *definition default* footprint instead is
    the silent-fallback bug this model exists to eliminate, so an unbound
    instance reaching a geometry path is a programming error — surfaced loudly
    rather than degraded to the wrong body.
    """

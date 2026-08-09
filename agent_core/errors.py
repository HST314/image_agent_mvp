"""Typed exceptions raised by the image agent state machine."""


class ImageAgentError(Exception):
    """Base exception for expected image agent failures."""


class ValidationBlockedError(ImageAgentError):
    """Raised when the task draft misses fields required for intake."""


class GateBlockedError(ImageAgentError):
    """Raised when a mandatory human approval gate is not satisfied."""


class StateTransitionError(ImageAgentError):
    """Raised when a workflow transition is not allowed."""

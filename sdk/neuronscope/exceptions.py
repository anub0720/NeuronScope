"""Public SDK exceptions."""


class NeuronScopeError(Exception):
    """Base error for NeuronScope SDK failures."""


class ConfigurationError(NeuronScopeError, ValueError):
    """Invalid SDK configuration or model input."""


class UnsupportedModelError(NeuronScopeError):
    """The requested model behavior is not supported by this SDK version."""


class TransportError(NeuronScopeError):
    """A permanent HTTP or backend protocol error."""


class PayloadTooLargeError(NeuronScopeError):
    """A payload exceeds the configured request size limit."""


class EventCapacityError(NeuronScopeError):
    """Unresolved collapse-event evidence reached its configured RAM limit."""

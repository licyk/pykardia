"""Runtime errors and the non-failure outcome of cooperative interruption."""

from __future__ import annotations

from typing import TypeVar

E = TypeVar("E", bound=BaseException)


class Error(Exception):
    """A shared exception handle with identity and concrete-type introspection."""

    def __init__(self, error: BaseException) -> None:
        """Retain the original exception instead of copying its value."""
        self._error: BaseException = error
        super().__init__(str(error))

    @classmethod
    def new(cls, error: BaseException) -> Error:
        """Wrap a concrete exception, preserving its identity."""
        return cls(error)

    @classmethod
    def msg(cls, message: object) -> Error:
        """Create an error containing a displayable message."""
        return cls(Exception(str(message)))

    def downcast_ref(self, error_type: type[E]) -> E | None:
        """Return the concrete exception if it has the requested type."""
        return self._error if isinstance(self._error, error_type) else None

    def is_(self, error_type: type[BaseException]) -> bool:
        """Test the wrapped exception's type."""
        return isinstance(self._error, error_type)

    def is_interrupted(self) -> bool:
        """Return whether this error represents cooperative interruption."""
        return self.is_(Interrupted)

    def as_std(self) -> BaseException:
        """Return the original Python exception."""
        return self._error

    def ptr_eq(self, other: Error) -> bool:
        """Test whether two handles refer to the same exception object."""
        return self._error is other._error

    def __str__(self) -> str:
        """Display the wrapped exception's message."""
        return str(self._error)


class Panicked(Exception):
    """An explicitly represented panic from a foreign-language callback."""

    def __init__(self, message: str) -> None:
        """Prefix the contained panic with the reference diagnostic."""
        self.message: str = message
        super().__init__(f"panicked: {message}")


class Interrupted(Exception):
    """The effect was disposed before reaching its next iteration boundary."""

    def __init__(self) -> None:
        """Use the reference runtime's diagnostic for interrupted setup."""
        super().__init__("interrupted at an iteration boundary")


class PyKardiaError(Exception):
    """Base class for invalid effect and provider-binding operations."""


class InactiveEffect(PyKardiaError):
    """An effect was created through an inactive context."""

    def __init__(self) -> None:
        """Describe why creating a resource is no longer allowed."""
        super().__init__("cannot create effect on inactive context")


class AlreadyProvided(PyKardiaError):
    """A live provider already occupies this service slot."""

    def __init__(self, key: str, by: str) -> None:
        """Identify the conflicting service and provider."""
        self.key: str = key
        self.by: str = by
        super().__init__(f'service "{key}" has been registered at <{by}>')


class NotProvided(PyKardiaError):
    """A service property was changed without a provided binding."""

    def __init__(self, key: str) -> None:
        """Identify the missing binding."""
        self.key: str = key
        super().__init__(f'cannot set property "{key}" without provide')


class ForeignBinding(PyKardiaError):
    """A different fiber owns the service property being changed."""

    def __init__(self, key: str) -> None:
        """Identify the binding whose ownership cannot be shared."""
        self.key: str = key
        super().__init__(f'cannot set property "{key}" in multiple fibers')


class AccessError(Exception):
    """Base class for undeclared, inactive or incorrectly typed service access."""


class Undeclared(AccessError):
    """The caller did not declare access to this dependency."""

    def __init__(self, key: str) -> None:
        """Identify the undeclared service."""
        self.key: str = key
        super().__init__(f'cannot get property "{key}" without inject')


class Inactive(AccessError):
    """The caller's required dependency is unavailable in its current context."""

    def __init__(self, key: str) -> None:
        """Identify the unavailable required service."""
        self.key: str = key
        super().__init__(f'cannot get required service "{key}" in inactive context')


class TypeMismatch(AccessError):
    """The resolved service does not have the requested concrete type."""

    def __init__(self, key: str, expected: str) -> None:
        """Identify the service and requested type name."""
        self.key: str = key
        self.expected: str = expected
        super().__init__(f'service "{key}" is not of type {expected}')


class AggregateError(Exception):
    """Several independent failures, kept in their original collection order."""

    def __init__(self, message: str, errors: list[BaseException]) -> None:
        """Retain the aggregate message and the original component exceptions."""
        self.message: str = message
        self.errors: list[BaseException] = list(errors)
        super().__init__(message)

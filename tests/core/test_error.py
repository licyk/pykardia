"""Concrete exception identities and diagnostics from core/src/error.rs."""

import unittest

from pykardia.core.effect import EffectCell, EffectScope
from pykardia.core.error import (
    AggregateError,
    AlreadyProvided,
    Error,
    ForeignBinding,
    Inactive,
    InactiveEffect,
    Interrupted,
    NotProvided,
    Panicked,
    TypeMismatch,
    Undeclared,
)
from pykardia.core.runtime import Runtime


class ErrorTests(unittest.IsolatedAsyncioTestCase):
    """Preserve identity when wrapping and treat wrapped interruption as non-failure."""

    async def test_shared_error_identity_and_downcast(self) -> None:
        """Two wrappers over one exception compare by identity, not message equality."""
        original = ValueError("bad")
        first, second = Error.new(original), Error.new(original)
        self.assertTrue(first.ptr_eq(second))
        self.assertFalse(first.ptr_eq(Error.new(ValueError("bad"))))
        self.assertTrue(first.is_(ValueError))
        self.assertIs(first.downcast_ref(ValueError), original)
        self.assertIsNone(first.downcast_ref(TypeError))
        self.assertIs(first.as_std(), original)
        self.assertEqual(str(first), "bad")
        self.assertEqual(str(Error.msg("message")), "message")
        aggregate = AggregateError("both", [first, second])
        self.assertEqual(aggregate.errors, [first, second])
        self.assertEqual(str(aggregate), "both")

    async def test_diagnostics_match_reference(self) -> None:
        """Match messages that cross logging, configuration and guest error boundaries."""
        cases = [
            (Panicked("boom"), "panicked: boom"),
            (InactiveEffect(), "cannot create effect on inactive context"),
            (AlreadyProvided("db", "old"), 'service "db" has been registered at <old>'),
            (NotProvided("db"), 'cannot set property "db" without provide'),
            (ForeignBinding("db"), 'cannot set property "db" in multiple fibers'),
            (Undeclared("db"), 'cannot get property "db" without inject'),
            (Inactive("db"), 'cannot get required service "db" in inactive context'),
            (TypeMismatch("db", "Database"), 'service "db" is not of type Database'),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(str(error), expected)

    async def test_wrapped_interruption_is_not_a_setup_failure(self) -> None:
        """Error::new(Interrupted) follows the same successful boundary path as Interrupted."""
        reports: list[BaseException] = []
        cleaned: list[int] = []
        cell = EffectCell(Runtime(), "interrupt")

        async def body(scope: EffectScope) -> None:
            """Register a resource and return the reference's wrapped interruption."""
            scope.defer_fn(lambda: cleaned.append(1))
            raise Error.new(Interrupted())

        cell.start(body, reports.append)
        await cell.ready()
        self.assertEqual(reports, [])
        self.assertEqual(cleaned, [])
        await cell.dispose()
        self.assertEqual(cleaned, [1])

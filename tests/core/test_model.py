"""Seeded real-runtime orchestrations checked against a declarative acyclic support set."""

import asyncio
import random
import unittest
from dataclasses import dataclass, field

import async_timeout

from pykardia import AccessError, Context, Disposer, Fiber, FiberState, JsonValue, PluginDef

KEYS: tuple[str | None, ...] = ("a", "b", None, "c", None)


@dataclass
class Token:
    """A provided token alive exactly during its provider's resource lifetime."""

    alive: bool = True


@dataclass
class World:
    """Observable writes and violations collected by the actual plugin callbacks."""

    table: dict[int, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def check(self, context: Context, keys: list[str], phase: str) -> None:
        """Assert binding reads stay inside the provider episode in setup and cleanup."""
        for key in keys:
            try:
                token = context.get_as(key, Token).arc()
            except AccessError as error:
                self.violations.append(f"{phase}: {key}: {error}")
            else:
                if not token.alive:
                    self.violations.append(f"{phase}: {key} read outside provider episode")


def definition(
    kind: int, dependencies: list[int], world: World, delays: tuple[int, int]
) -> PluginDef:
    """Create a plugin that acquires real resources and reverts its writes exactly once."""
    keys = [key for index in dependencies if (key := KEYS[index]) is not None]

    async def apply(context: Context, config: JsonValue) -> None:
        """Delay real initialization, then provide a lifetime token and acquire a table entry."""
        await asyncio.sleep(delays[0] / 1000)
        world.check(context, keys, "load")
        key = KEYS[kind]
        if key is not None:
            token = Token()
            context.provide_as(key, token)
            context.on_dispose(lambda: setattr(token, "alive", False))
        world.table[kind] = world.table.get(kind, 0) + 1

        async def cleanup() -> None:
            """Read committed dependencies and remove precisely the write this episode made."""
            await asyncio.sleep(delays[1] / 1000)
            world.check(context, keys, "cleanup")
            world.table[kind] -= 1
            if not world.table[kind]:
                del world.table[kind]

        context.defer("write", Disposer.future(cleanup))

    provided = KEYS[kind]
    return (
        PluginDef.new(apply)
        .name(f"kind{kind}")
        .inject(keys)
        .provides([provided] if provided is not None else [])
    )


async def quiesce(root: Context) -> None:
    """Observe all real transitions, including retired fibers no longer in the registry."""
    await root._root.wait_until(
        lambda state: all(not fiber.inertia for fiber in state.fibers.values())
    )


class ModelTests(unittest.IsolatedAsyncioTestCase):
    """Check ordering, recovery, support and cleanup under 256 deterministic random schedules."""

    async def test_orchestrations_reach_the_declarative_model(self) -> None:
        """Port the reference's 256-case model using seeded inputs and the production scheduler."""
        for seed in range(256):
            with self.subTest(seed=seed):
                async with async_timeout.timeout(5):
                    await self._scenario(seed)

    async def _scenario(self, seed: int) -> None:
        """Generate an acyclic graph and mutate actual plugin fibers under real delays."""
        rng = random.Random(seed)
        dependencies: list[list[int]] = []
        for kind in range(len(KEYS)):
            candidates = [index for index in range(kind) if KEYS[index] is not None]
            dependencies.append(
                sorted(rng.sample(candidates, rng.randint(0, min(2, len(candidates)))))
            )
        delays = [(rng.randrange(3), rng.randrange(3)) for _ in KEYS]
        operations = [
            (rng.choices(["toggle", "update", "yield"], [4, 1, 2])[0], rng.randrange(len(KEYS)))
            for _ in range(rng.randrange(1, 24))
        ]
        root, world = Context(), World()
        definitions = [
            definition(kind, dependencies[kind], world, delays[kind]) for kind in range(len(KEYS))
        ]
        fibers: list[Fiber | None] = [None] * len(KEYS)
        for operation, kind in operations:
            fiber = fibers[kind]
            if operation == "toggle":
                if fiber is None:
                    fibers[kind] = root.plugin_def(definitions[kind])
                else:
                    fiber.dispose()
                    fibers[kind] = None
            elif operation == "update":
                if fiber is not None:
                    fiber.update({"op": "update"})
            else:
                for _ in range(kind % 4):
                    await asyncio.sleep(0)
        await quiesce(root)
        active: set[int] = set()
        for kind in range(len(KEYS)):
            if fibers[kind] is not None and all(index in active for index in dependencies[kind]):
                active.add(kind)
        evidence = f"seed={seed}, dependencies={dependencies}, ops={operations}, delays={delays}"
        for kind, fiber in enumerate(fibers):
            if fiber is not None:
                expected = FiberState.Active if kind in active else FiberState.Pending
                self.assertEqual(fiber.state(), expected, evidence)
        self.assertEqual(world.table, dict.fromkeys(active, 1), evidence)
        self.assertEqual(world.violations, [], evidence)
        for kind, key in enumerate(KEYS):
            if key is not None:
                self.assertEqual(root.live_as(key, Token) is not None, kind in active, evidence)
        await root.fiber().dispose()
        await quiesce(root)
        self.assertEqual(world.table, {}, evidence)
        self.assertEqual(world.violations, [], evidence)
        self.assertEqual(root.registry().size(), 0, evidence)
        self.assertEqual(root.fiber().effect_count(), 0, evidence)
        self.assertTrue(all(not root.bindings(key) for key in KEYS if key is not None), evidence)

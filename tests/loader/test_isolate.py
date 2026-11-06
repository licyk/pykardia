"""Managed realm migration, retained binding identities and guarded disposal."""

from __future__ import annotations

import asyncio

from pykardia import (
    Context,
    Disposer,
    EffectHandle,
    FiberState,
    JsonValue,
    PluginDef,
    ServiceChanged,
)
from tests.loader.common import Counter, LoaderCase
from tests.loader.test_evaluate import provider


class IsolateTests(LoaderCase):
    """Exercise real service slots and committed dependency guards across realm changes."""

    async def asyncSetUp(self) -> None:
        """Register a provider and a consumer of the same coeffect name."""
        await super().asyncSetUp()
        self.consumer = Counter()
        self.catalog.register_def("foo", self.consumer.definition().inject(["bar"]))
        self.catalog.register_def("bar", provider("bar"))

    async def test_basic_isolation_relevant_and_irrelevant(self) -> None:
        """Only changes affecting the consumed key cause consumer reload or unload."""
        await self.read([{"id": "p", "name": "bar"}, {"id": "c", "name": "foo"}])
        self.assertEqual((self.consumer.applied, self.consumer.disposed), (1, 0))
        steps: list[tuple[str, JsonValue, tuple[int, int]]] = [
            ("c", {"bar": True}, (0, 1)),
            ("c", {"bar": True, "qux": True}, (0, 0)),
            ("c", {"qux": True}, (1, 0)),
            ("c", None, (0, 0)),
            ("p", {"bar": True}, (0, 1)),
            ("p", {"bar": True, "qux": True}, (0, 0)),
            ("p", {"qux": True}, (1, 0)),
            ("p", None, (0, 0)),
        ]
        for identity, isolation, delta in steps:
            before = self.consumer.applied, self.consumer.disposed
            await self.loader.update(identity, {"isolate": isolation})
            await self.loader.settled()
            self.assertEqual(
                (self.consumer.applied - before[0], self.consumer.disposed - before[1]), delta
            )

    async def test_group_private_shared_and_nested_realms(self) -> None:
        """Nested consumers inherit private realms or choose explicit shared/private overrides."""
        await self.read(
            [
                {
                    "id": "alpha",
                    "name": "group",
                    "group": True,
                    "isolate": {"bar": True},
                    "config": [{"id": "pa", "name": "bar", "config": "alpha"}],
                },
                {
                    "id": "beta",
                    "name": "group",
                    "group": True,
                    "isolate": {"bar": "beta"},
                    "config": [{"id": "pb", "name": "bar", "config": "beta"}],
                },
            ]
        )
        await self.loader.create({"id": "inherited", "name": "foo"}, parent="alpha")
        await self.loader.create(
            {"id": "shared", "name": "foo", "isolate": {"bar": "beta"}}, parent="alpha"
        )
        await self.loader.create(
            {"id": "private", "name": "foo", "isolate": {"bar": True}}, parent="alpha"
        )
        await self.loader.settled()
        self.assertEqual(self.fiber("inherited").context().view("bar"), "alpha")
        self.assertEqual(self.fiber("shared").context().view("bar"), "beta")
        self.assertEqual(self.fiber("private").state(), FiberState.Pending)
        self.assertEqual(self.consumer.applied, 2)
        await self.loader.update("alpha", {"isolate": {"bar": True, "irrelevant": True}})
        await self.loader.settled()
        self.assertEqual((self.consumer.applied, self.consumer.disposed), (2, 0))

    async def test_transfer_provider_and_consumer_between_realms(self) -> None:
        """Moving contexts into and out of an isolated group changes coeffect availability."""
        await self.read(
            [
                {"id": "g", "name": "group", "group": True, "isolate": {"bar": True}, "config": []},
                {"id": "p", "name": "bar"},
                {"id": "c", "name": "foo"},
            ]
        )
        for identity, parent, delta in (
            ("c", "g", (0, 1)),
            ("p", "g", (1, 0)),
            ("c", None, (0, 1)),
            ("p", None, (1, 0)),
        ):
            before = self.consumer.applied, self.consumer.disposed
            await self.loader.update(identity, {}, parent=parent)
            await self.loader.settled()
            self.assertEqual(
                (self.consumer.applied - before[0], self.consumer.disposed - before[1]), delta
            )

    async def test_group_switches_consumer_provider(self) -> None:
        """A group's shared-realm change reloads an existing consumer against the new provider."""
        await self.read(
            [
                {"id": "a", "name": "bar", "isolate": {"bar": "alpha"}, "config": "alpha"},
                {"id": "b", "name": "bar", "isolate": {"bar": "beta"}, "config": "beta"},
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "isolate": {"bar": "alpha"},
                    "config": [{"id": "c", "name": "foo"}],
                },
            ]
        )
        fiber = self.fiber("c")
        self.assertEqual(fiber.context().view("bar"), "alpha")
        await self.loader.update("g", {"isolate": {"bar": "beta"}})
        await self.loader.settled()
        self.assertEqual(self.fiber("c"), fiber)
        self.assertEqual(fiber.context().view("bar"), "beta")
        self.assertEqual((self.consumer.applied, self.consumer.disposed), (2, 1))

    async def test_group_migrates_provider_between_consumers(self) -> None:
        """A provider inside a moved group activates the newly matching consumer without restart."""
        await self.read(
            [
                {"id": "a", "name": "foo", "isolate": {"bar": "alpha"}},
                {"id": "b", "name": "foo", "isolate": {"bar": "beta"}},
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "isolate": {"bar": "alpha"},
                    "config": [{"id": "p", "name": "bar"}],
                },
            ]
        )
        binding = self.root.bindings("bar")[0][1]
        await self.loader.update("g", {"isolate": {"bar": "beta"}})
        await self.loader.settled()
        self.assertEqual(self.root.bindings("bar")[0][1], binding)
        self.assertEqual(self.fiber("a").state(), FiberState.Pending)
        self.assertEqual(self.fiber("b").state(), FiberState.Active)
        self.assertEqual((self.consumer.applied, self.consumer.disposed), (2, 1))

    async def test_provider_migrates_without_restart_then_reloads_in_place(self) -> None:
        """Migration retains binding identity; actual config reload creates a fresh binding."""
        await self.read([{"id": "p", "name": "bar", "config": "init"}])
        fiber = self.fiber("p")
        original = self.root.bindings("bar")[0]
        await self.loader.update("p", {"isolate": {"bar": True}})
        moved = self.root.bindings("bar")[0]
        self.assertEqual(moved[1], original[1])
        self.assertNotEqual(moved[0], original[0])
        self.assertEqual(self.fiber("p"), fiber)
        await self.loader.update("p", {"config": "reloaded"})
        await self.loader.settled()
        reloaded = self.root.bindings("bar")[0]
        self.assertEqual(reloaded[0], moved[0])
        self.assertNotEqual(reloaded[1], moved[1])
        self.assertEqual(fiber.context().view("bar"), "reloaded")

    async def test_self_access_during_reparent_before_binding_migration(self) -> None:
        """An update hook reads its own provision after reparenting and before slot migration."""
        reads: list[str] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Publish a concrete value and accept raw JSON updates without restarting."""
            ctx.provide_as("svc", "prov")

            def update(config: JsonValue) -> bool:
                """Read the actual own provision during loader's context-patching sequence."""
                reads.append(ctx.get_as("svc", str).arc())
                return True

            ctx.on_update(JsonValue, update)

        self.catalog.register_def("prov", PluginDef.new(apply))
        await self.read([{"id": "p", "name": "prov", "config": {"revision": 1}}])
        await self.loader.update("p", {"isolate": {"svc": "A"}, "config": {"revision": 2}})
        self.assertEqual(reads, ["prov"])
        self.assertEqual(self.fiber("p").context().get_as("svc", str).arc(), "prov")

    async def test_repeated_moves_preserve_other_bindings_and_collect_unused_realms(self) -> None:
        """Bindings in reused root and intermediate realms survive another provider's disposal."""
        self.register("keep")
        await self.read([{"id": "p", "name": "bar", "config": "prov"}])
        root_realm, original = self.root.bindings("bar")[0]
        await self.loader.update("p", {"isolate": {"bar": "A"}})
        at_a = self.root.bindings("bar")[0][0]
        await self.loader.create({"id": "root", "name": "bar", "config": "rootUser"})
        await self.loader.create({"id": "keep", "name": "keep", "isolate": {"bar": "A"}})
        await self.loader.settled()
        root_slot = self.root.bindings("bar")[1]
        self.assertEqual(root_slot[0], root_realm)
        await self.loader.update("p", {"isolate": {"bar": "B"}})
        at_b = self.root.bindings("bar")[0][0]
        await self.loader.create(
            {"id": "onA", "name": "bar", "isolate": {"bar": "A"}, "config": "onA"}
        )
        await self.loader.settled()
        on_a = self.root.bindings("bar")[2]
        self.assertEqual(on_a[0], at_a)
        await self.loader.update("p", {"isolate": {"bar": "C"}})
        at_c, moved_id = self.root.bindings("bar")[0]
        self.assertEqual(moved_id, original)
        self.assertEqual(len({root_realm, at_a, at_b, at_c}), 4)
        self.assertNotIn("B", self.loader._state.global_realms)
        await self.fiber("p").dispose()
        self.assertEqual(self.root.bindings("bar"), [root_slot, on_a])

    async def test_moved_provider_waits_for_consumer_cleanup(self) -> None:
        """A retained committed view remains readable until cleanup finishes in the moved realm."""
        entered, release = asyncio.Event(), asyncio.Event()
        timeline: list[str] = []

        async def provide(ctx: Context, config: JsonValue) -> None:
            """Record provider cleanup after publishing its actual service value."""
            assert isinstance(config, str)
            ctx.provide_as("svc", config)
            ctx.on_dispose(lambda: timeline.append(f"closed: {config}"))

        async def consume(ctx: Context, config: JsonValue) -> None:
            """Retain a service read until asynchronous cleanup is released."""

            async def cleanup() -> None:
                """Read the committed value after the live binding has been withdrawn."""
                entered.set()
                await release.wait()
                timeline.append(f"read: {ctx.get_as('svc', str).arc()}")

            ctx.defer("cleanup", Disposer.future(cleanup))

        self.catalog.register_def("provider", PluginDef.new(provide))
        self.catalog.register_def("consumer", PluginDef.new(consume).inject(["svc"]))
        await self.read([{"id": "p", "name": "provider", "config": "moved"}])
        original = self.root.bindings("svc")[0]
        await self.loader.update("p", {"isolate": {"svc": "B"}})
        self.assertEqual(self.root.bindings("svc")[0][1], original[1])
        await self.loader.create({"id": "other", "name": "provider", "config": "other"})
        await self.loader.create({"id": "c", "name": "consumer", "isolate": {"svc": "B"}})
        await self.loader.settled()
        other_slot = self.root.bindings("svc")[1]
        disposal = self.fiber("p").dispose()
        await entered.wait()
        self.assertEqual(timeline, [])
        release.set()
        await disposal
        await self.loader.settled()
        self.assertEqual(timeline, ["read: moved", "closed: moved"])
        self.assertEqual(self.root.bindings("svc"), [other_slot])
        self.assertEqual(self.fiber("c").state(), FiberState.Pending)

    async def test_blocked_migration_notifies_retained_realm_on_disposal(self) -> None:
        """A conflicting target leaves the original slot and its dependent cleanup intact."""
        for mode in ("fiber", "provide"):
            entered, release = asyncio.Event(), asyncio.Event()
            provisions: dict[str, EffectHandle] = {}
            starts: list[str] = []
            stops: list[str] = []

            async def provide(
                ctx: Context, config: JsonValue, provisions: dict[str, EffectHandle] = provisions
            ) -> None:
                """Capture the real provision handle for direct-disposal coverage."""
                assert isinstance(config, str)
                provisions[config] = ctx.provide_as("svc", config)

            async def consume(
                ctx: Context,
                config: JsonValue,
                starts: list[str] = starts,
                stops: list[str] = stops,
                entered: asyncio.Event = entered,
                release: asyncio.Event = release,
            ) -> None:
                """Record service selection and await actual cleanup completion."""
                starts.append(ctx.get_as("svc", str).arc())

                async def cleanup() -> None:
                    """Keep the old committed binding alive until the test opens its gate."""
                    entered.set()
                    await release.wait()
                    stops.append(ctx.get_as("svc", str).arc())

                ctx.defer("cleanup", Disposer.future(cleanup))

            self.catalog.register_def("provider", PluginDef.new(provide))
            self.catalog.register_def("consumer", PluginDef.new(consume).inject(["svc"]))
            await self.read(
                [
                    {"id": "first", "name": "provider", "config": "first", "isolate": {"svc": "A"}},
                    {
                        "id": "second",
                        "name": "provider",
                        "config": "second",
                        "isolate": {"svc": "B"},
                    },
                    {"id": "ca", "name": "consumer", "isolate": {"svc": "A"}},
                    {"id": "cb", "name": "consumer", "isolate": {"svc": "B"}},
                ]
            )
            a, b = self.root.bindings("svc")
            await self.loader.update("first", {"isolate": {"svc": "B"}})
            self.assertEqual(self.root.bindings("svc"), [a, b])
            self.assertEqual(self.fiber("first").context().get_as("svc", str).arc(), "first")
            events_a: list[ServiceChanged] = []
            events_b: list[ServiceChanged] = []
            listener_a = self.root.isolate_in("svc", a[0]).on(
                ServiceChanged,
                lambda event, events=events_a: events.append(event) if event.key == "svc" else None,
            )
            listener_b = self.root.isolate_in("svc", b[0]).on(
                ServiceChanged,
                lambda event, events=events_b: events.append(event) if event.key == "svc" else None,
            )
            disposal = (
                self.fiber("first").dispose() if mode == "fiber" else provisions["first"].dispose()
            )
            await entered.wait()
            release.set()
            await disposal
            await self.loader.settled()
            self.assertEqual(stops, ["first"])
            self.assertEqual(self.fiber("ca").state(), FiberState.Pending)
            self.assertEqual(self.fiber("cb").state(), FiberState.Active)
            self.assertEqual(self.root.bindings("svc"), [b])
            self.assertTrue(events_a)
            self.assertEqual(events_b, [])
            await self.loader.create(
                {
                    "id": "replacement",
                    "name": "provider",
                    "config": "replacement",
                    "isolate": {"svc": "A"},
                }
            )
            await self.loader.settled()
            self.assertEqual(starts, ["first", "second", "replacement"])
            self.assertEqual(events_b, [])
            await listener_a.dispose()
            await listener_b.dispose()
            await self.read([])

    async def test_nested_isolation_shields_descendants(self) -> None:
        """An inner explicit realm prevents outer isolation changes from reloading its consumers."""
        await self.read(
            [
                {
                    "id": "outer",
                    "name": "group",
                    "group": True,
                    "config": [
                        {
                            "id": "inner",
                            "name": "group",
                            "group": True,
                            "isolate": {"bar": "custom"},
                            "config": [
                                {"id": "p", "name": "bar", "config": "custom"},
                                {"id": "b", "name": "foo"},
                            ],
                        },
                    ],
                },
                {"id": "a", "name": "foo", "isolate": {"bar": "custom"}},
            ]
        )
        self.assertEqual(self.fiber("a").context().view("bar"), "custom")
        self.assertEqual(self.fiber("b").context().view("bar"), "custom")
        isolations: list[JsonValue] = [{"bar": "custom"}, {}]
        for isolation in isolations:
            await self.loader.update("outer", {"isolate": isolation})
            await self.loader.settled()
        self.assertEqual((self.consumer.applied, self.consumer.disposed), (2, 0))

    async def test_committed_parent_and_pinned_child_views_survive_move(self) -> None:
        """A parent and a re-isolated child both read their old view while unloading."""
        loads: list[str] = []
        unloads: list[str] = []

        async def parent(ctx: Context, config: JsonValue) -> None:
            """Create a child pinned to this episode's admitted realm."""
            realm = ctx.realm("bar")
            assert realm is not None

            async def child(child_ctx: Context) -> None:
                """Observe actual committed values during child apply and cleanup."""
                loads.append(f"child:{child_ctx.view('bar')}")
                child_ctx.on_dispose(lambda: unloads.append(f"child:{child_ctx.view('bar')}"))

            ctx.isolate_in("bar", realm).inject(["bar"], child)
            ctx.on_dispose(lambda: unloads.append(f"parent:{ctx.view('bar')}"))

        self.catalog.register_def("parent", PluginDef.new(parent).inject(["bar"]))
        await self.read(
            [
                {"id": "a", "name": "bar", "isolate": {"bar": "a"}, "config": "a"},
                {"id": "b", "name": "bar", "isolate": {"bar": "b"}, "config": "b"},
                {"id": "parent", "name": "parent", "isolate": {"bar": "a"}},
            ]
        )
        while loads != ["child:a"]:
            await asyncio.sleep(0)
        await self.loader.update("parent", {"isolate": {"bar": "b"}})
        await self.loader.settled()
        while len(loads) < 2:
            await asyncio.sleep(0)
        self.assertEqual(loads, ["child:a", "child:b"])
        self.assertCountEqual(unloads, ["child:a", "parent:a"])
        self.assertEqual(self.root.log_buffer(), [])

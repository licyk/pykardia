"""Count actual service-view reads and verify cached loader admission decisions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pykardia import Context, Disposer, FiberState, JsonValue, PluginDef
from tests.loader.common import LoaderCase
from tests.loader.test_evaluate import provider, settings, worker


@dataclass
class Probe:
    """A mutable service view that records each actual serialization request."""

    enabled: bool = True
    reads: int = 0

    def serialize(self) -> JsonValue:
        """Read the current enabled flag and count one JSON view access."""
        self.reads += 1
        return {"enabled": self.enabled}


class VerdictTests(LoaderCase):
    """Observe production expression evaluation instead of counting calls to a mock evaluator."""

    async def asyncSetUp(self) -> None:
        """Provide both ordinary settings and an observable custom serializer."""
        await super().asyncSetUp()
        self.worker = self.register("worker")
        self.probe = Probe()
        self.catalog.register_def("settings", provider("settings"))
        self.catalog.register_def("env", provider("env"))

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Publish the real probe object with a typed view serializer."""
            ctx.provide_serialized_as("settings", self.probe, Probe.serialize)

        self.catalog.register_def("probe", PluginDef.new(apply))

    async def test_verdict_cached_across_reads_and_mutations(self) -> None:
        """Neither reading verdicts nor mutating a service re-evaluates disabled expressions."""
        for group in (False, True):
            self.probe = Probe()
            owner: dict[str, JsonValue] = {
                "id": "owner",
                "name": "group" if group else "worker",
                "group": group,
                "inject": ["settings"],
                "disabled": {"__jsExpr": "!settings.enabled"},
            }
            if group:
                owner["config"] = [
                    {"id": "child", "name": "worker"},
                    {"id": "sibling", "name": "worker"},
                ]
            await self.read([{"id": "settings", "name": "probe"}, owner])
            entry = self.loader.resolve("child" if group else "owner")
            self.assertEqual(self.probe.reads, 1)
            self.probe.enabled = False
            for _ in range(3):
                self.assertFalse(entry.is_disabled())
            self.assertEqual(self.probe.reads, 1)
            self.assertEqual(self.fiber("child" if group else "owner").state(), FiberState.Active)
            await self.loader.update("owner", {"config": owner.get("config")})
            await self.loader.settled()
            self.assertTrue(entry.is_disabled())
            inner = entry.inner_fiber()
            self.assertTrue(inner is None or inner.is_removed())
            if group:
                self.assertEqual(self.fiber("owner").state(), FiberState.Active)
            await self.read([])

    async def test_failed_verdict_reads_and_each_changed_expression_once(self) -> None:
        """A failing disabled expression is cached and each explicit direction change reads once."""
        await self.read(
            [
                {"id": "settings", "name": "probe"},
                worker({"inject": ["settings"], "disabled": {"__jsExpr": "!settings.enabled"}}),
            ]
        )
        self.assertEqual(self.probe.reads, 1)
        await self.loader.update("worker", {"disabled": {"__jsExpr": "settings.missing.value"}})
        await self.loader.settled()
        entry = self.loader.resolve("worker")
        for _ in range(3):
            self.assertTrue(entry.is_disabled())
        self.assertEqual(self.probe.reads, 2)
        await self.loader.update("worker", {"disabled": {"__jsExpr": "!settings.enabled"}})
        await self.loader.settled()
        self.assertEqual(self.probe.reads, 3)
        self.assertEqual(self.worker.applied, 2)

    async def test_verdict_invalidated_before_asynchronous_unload(self) -> None:
        """The cached verdict becomes undecided before a dependent's cleanup starts."""
        entered, release = asyncio.Event(), asyncio.Event()
        during: list[bool] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Capture the verdict in the synchronous prefix of a real async inverse."""

            async def finish() -> None:
                """Finish cleanup only after the observation gate opens."""
                await release.wait()

            def cleanup() -> asyncio.Future[None]:
                """Observe admission before returning an owned asynchronous cleanup task."""
                during.append(self.loader.resolve("held").is_disabled())
                entered.set()
                return asyncio.create_task(finish())

            ctx.defer("held", Disposer.future(cleanup))

        self.catalog.register_def("held", PluginDef.new(apply))
        await self.read(
            [
                settings({"enabled": True}),
                {
                    "id": "held",
                    "name": "held",
                    "inject": ["settings"],
                    "disabled": {"__jsExpr": "!settings.enabled"},
                },
            ]
        )
        entry = self.loader.resolve("held")
        outer = entry.outer_fiber()
        self.loader.remove("settings")
        await entered.wait()
        self.assertTrue(entry.is_disabled())
        self.assertEqual(during, [True])
        self.assertEqual(entry.outer_fiber(), outer)
        release.set()
        await self.loader.settled()
        await self.loader.update("held", {"config": {"value": 1}})
        self.assertEqual(entry.outer_fiber(), outer)
        await self.loader.create(settings({"enabled": True}))
        await self.loader.settled()
        self.assertFalse(entry.is_disabled())

    async def test_disabled_group_dependency_loss_and_recovery(self) -> None:
        """A structural group stays structurally enabled while its cached verdict is undecided."""
        await self.read(
            [
                settings({"enabled": False}),
                {
                    "id": "owner",
                    "name": "group",
                    "group": True,
                    "inject": ["settings"],
                    "disabled": {"__jsExpr": "!settings.enabled"},
                    "config": [{"id": "child", "name": "worker"}],
                },
            ]
        )
        owner = self.loader.resolve("owner")
        self.loader.remove("settings")
        await self.loader.settled()
        self.assertFalse(owner.is_disabled())
        await self.loader.create(settings({"enabled": True}))
        await self.loader.settled()
        self.assertFalse(self.loader.resolve("child").is_disabled())
        self.assertEqual(self.worker.applied, 1)

    async def test_move_reevaluates_in_new_parent_context(self) -> None:
        """Moving entries changes disabled and inject-metadata evaluation to the new parent."""
        for field in ("disabled", "inject"):
            extra: dict[str, JsonValue] = (
                {"disabled": {"__jsExpr": "!env.enabled"}}
                if field == "disabled"
                else {
                    "inject": {"settings": {"namespace": {"__jsExpr": "env.namespace"}}},
                    "disabled": {"__jsExpr": "false"},
                }
            )
            await self.read(
                [
                    settings({}),
                    {
                        "id": "a",
                        "name": "group",
                        "group": True,
                        "inject": ["env"],
                        "isolate": {"env": "a"},
                        "config": [worker(extra)],
                    },
                    {
                        "id": "b",
                        "name": "group",
                        "group": True,
                        "inject": ["env"],
                        "isolate": {"env": "b"},
                        "config": [],
                    },
                    {
                        "id": "env-a",
                        "name": "env",
                        "isolate": {"env": "a"},
                        "config": {"enabled": True, "namespace": "a"},
                    },
                    {
                        "id": "env-b",
                        "name": "env",
                        "isolate": {"env": "b"},
                        "config": {"enabled": False, "namespace": "b"},
                    },
                ]
            )
            entry = self.loader.resolve("worker")
            self.assertEqual(self.fiber("worker").state(), FiberState.Active)
            await self.loader.update("worker", {}, parent="b")
            await self.loader.settled()
            if field == "disabled":
                self.assertTrue(entry.is_disabled())
                self.assertTrue(self.fiber("worker").is_removed())
            else:
                outer = entry.outer_fiber()
                assert outer is not None
                self.assertEqual(outer.inject().meta("settings"), {"namespace": "b"})
                self.assertEqual(self.fiber("worker").state(), FiberState.Active)
            await self.read([])

    async def test_realm_change_reevaluates_verdict(self) -> None:
        """A consumer moved to a disabled settings realm retains its written expression."""
        await self.read(
            [
                {
                    "id": "a",
                    "name": "settings",
                    "isolate": {"settings": "a"},
                    "config": {"enabled": True},
                },
                {
                    "id": "b",
                    "name": "settings",
                    "isolate": {"settings": "b"},
                    "config": {"enabled": False},
                },
                worker(
                    {
                        "inject": ["settings"],
                        "isolate": {"settings": "a"},
                        "disabled": {"__jsExpr": "!settings.enabled"},
                    }
                ),
            ]
        )
        entry = self.loader.resolve("worker")
        self.assertFalse(entry.is_disabled())
        await self.loader.update("worker", {"isolate": {"settings": "b"}})
        await self.loader.settled()
        self.assertTrue(entry.is_disabled())
        self.assertTrue(self.fiber("worker").is_removed())

    async def test_failing_inject_retries_on_refresh(self) -> None:
        """Refreshing a failed metadata expression retries it without creating fibers."""
        await self.read([worker({"inject": {"missing": {"__jsExpr": "doesNotExist.value"}}})])
        before = len(self.root.log_buffer())
        await self.loader.update("worker", {})
        await self.loader.settled()
        self.assertGreater(len(self.root.log_buffer()), before)
        self.assertTrue(self.loader.resolve("worker").is_disabled())
        self.assertIsNone(self.loader.resolve("worker").inner_fiber())

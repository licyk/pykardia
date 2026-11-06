"""Expression evaluation sites, cached verdicts and reactive entry recovery."""

from __future__ import annotations

from pykardia import Context, FiberState, Inject, JsonValue, PluginDef
from tests.loader.common import Counter, LoaderCase


def provider(name: str) -> PluginDef:
    """Provide each loaded JSON config as the named service's actual view."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Publish the config from this provider episode."""
        ctx.provide_as(name, config)

    return PluginDef.new(apply)


def settings(config: JsonValue) -> JsonValue:
    """Build the shared settings provider entry."""
    return {"id": "settings", "name": "settings", "config": config}


def worker(extra: dict[str, JsonValue]) -> JsonValue:
    """Add scenario options to the common worker entry."""
    return {"id": "worker", "name": "worker", **extra}


class EvaluateTests(LoaderCase):
    """Evaluate actual expressions under their respective loader-generated contexts."""

    async def asyncSetUp(self) -> None:
        """Install JSON providers and an observed worker in the real catalog."""
        await super().asyncSetUp()
        self.catalog.register_def("settings", provider("settings"))
        self.catalog.register_def("env", provider("env"))
        self.worker = self.register("worker")

    async def test_disabled_expressions_and_outer_parentage(self) -> None:
        """False expression objects enable workers; injected entries nest inner under outer."""
        await self.read(
            [
                settings({}),
                {"id": "a", "name": "worker", "disabled": {"__jsExpr": "false"}},
                {"id": "b", "name": "worker", "disabled": {"__jsExpr": "true"}},
                worker({"inject": ["settings"]}),
            ]
        )
        self.assertFalse(self.loader.resolve("a").is_disabled())
        self.assertTrue(self.loader.resolve("b").is_disabled())
        self.assertIsNone(self.loader.resolve("a").outer_fiber())
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        self.assertEqual(self.fiber("worker").parent(), outer)
        self.assertEqual(list(outer.inject().keys()), ["settings"])
        self.assertTrue(self.fiber("worker").inject().is_empty())
        self.assertEqual(self.worker.applied, 2)

    async def test_waits_for_inject_and_recovers_disabled(self) -> None:
        """Missing dependencies delay verdicts; later provider changes reevaluate them."""
        await self.read(
            [worker({"inject": ["settings"], "disabled": {"__jsExpr": "!settings.enabled"}})]
        )
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        self.assertEqual(outer.state(), FiberState.Pending)
        self.assertTrue(entry.is_disabled())
        await self.loader.create(settings({"enabled": False}))
        await self.loader.settled()
        self.assertEqual(outer.state(), FiberState.Active)
        self.assertIsNone(entry.inner_fiber())
        await self.loader.update("settings", {"config": {"enabled": True}})
        await self.loader.settled()
        self.assertFalse(entry.is_disabled())
        self.assertEqual(self.worker.applied, 1)

    async def test_failing_disabled_and_intercept_recovery(self) -> None:
        """Invalid expressions suppress application and a corrected intercept reactivates it."""
        await self.read(
            [
                settings({"proxy": "p1"}),
                worker(
                    {"inject": ["settings"], "disabled": {"__jsExpr": "settings.missing.value"}}
                ),
            ]
        )
        entry = self.loader.resolve("worker")
        self.assertIsNone(entry.inner_fiber())
        self.assertTrue(
            any(
                "cannot evaluate `settings.missing.value`" in message.text
                for message in self.root.log_buffer()
            )
        )
        await self.loader.update(
            "worker", {"disabled": None, "intercept": {"http": {"__jsExpr": "settings.proxy"}}}
        )
        await self.loader.settled()
        inner = self.fiber("worker")
        self.assertEqual(inner.context().intercepts("http"), ["p1"])
        await self.loader.update(
            "worker", {"intercept": {"http": {"__jsExpr": "settings.missing.value"}}}
        )
        await self.loader.settled()
        self.assertTrue(inner.is_removed())
        await self.loader.update("worker", {"config": {"tag": "v2"}})
        await self.loader.settled()
        self.assertEqual(self.worker.applied, 1)
        await self.loader.update("worker", {"intercept": {"http": {"__jsExpr": "settings.proxy"}}})
        await self.loader.settled()
        self.assertFalse(entry.is_disabled())
        self.assertEqual(self.worker.applied, 2)

    async def test_inject_values_use_parent_context(self) -> None:
        """Injection metadata can read a parent's dependency while preserving expression source."""
        inject: JsonValue = {"settings": {"namespace": {"__jsExpr": "env.namespace"}}}
        await self.read(
            [
                {"id": "env", "name": "env", "config": {"namespace": "ns"}},
                settings({}),
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "inject": ["env"],
                    "config": [worker({"inject": inject})],
                },
            ]
        )
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        self.assertEqual(outer.inject().meta("settings"), {"namespace": "ns"})
        options = entry.options()
        assert isinstance(options, dict)
        self.assertEqual(options["inject"], inject)
        self.assertEqual(self.worker.applied, 1)

    async def test_inject_cannot_read_sibling_or_own_declaration(self) -> None:
        """The entry's not-yet-established injection does not authorize its own metadata reads."""
        await self.read(
            [
                settings({"namespace": "ns"}),
                worker({"inject": {"settings": True, "other": {"__jsExpr": "settings.namespace"}}}),
            ]
        )
        entry = self.loader.resolve("worker")
        self.assertIsNone(entry.outer_fiber())
        self.assertIsNone(entry.inner_fiber())
        self.assertEqual(self.worker.applied, 0)

    async def test_intercept_updates_in_place_on_inner_only(self) -> None:
        """An interception change swaps the inner overlay without restarting the plugin."""
        await self.read(
            [
                settings({"proxy": "p1"}),
                worker(
                    {
                        "inject": ["settings"],
                        "intercept": {"http": {"proxy": {"__jsExpr": "settings.proxy"}}},
                    }
                ),
            ]
        )
        entry = self.loader.resolve("worker")
        inner, outer = self.fiber("worker"), entry.outer_fiber()
        assert outer is not None
        self.assertEqual(outer.context().intercepts("http"), [])
        self.assertEqual(inner.context().intercepts("http"), [{"proxy": "p1"}])
        await self.loader.update(
            "worker", {"intercept": {"http": {"proxy": {"__jsExpr": "settings.proxy + '!'"}}}}
        )
        self.assertEqual(inner.context().intercepts("http"), [{"proxy": "p1!"}])
        await self.loader.update("worker", {"intercept": None})
        self.assertEqual(inner.context().intercepts("http"), [])
        self.assertEqual(self.worker.applied, 1)

    async def test_dependency_loss_preserves_source_and_outer(self) -> None:
        """Loss retires the inner fiber without writing a literal disabled flag."""
        expression: JsonValue = {"__jsExpr": "!settings.enabled"}
        await self.read(
            [settings({"enabled": True}), worker({"inject": ["settings"], "disabled": expression})]
        )
        entry = self.loader.resolve("worker")
        inner, outer = self.fiber("worker"), entry.outer_fiber()
        self.loader.remove("settings")
        await self.loader.settled()
        self.assertTrue(inner.is_removed())
        self.assertEqual(entry.outer_fiber(), outer)
        self.assertTrue(entry.is_disabled())
        options = entry.options()
        assert isinstance(options, dict)
        self.assertEqual(options["disabled"], expression)
        await self.loader.create(settings({"enabled": True}))
        await self.loader.settled()
        self.assertEqual(self.fiber("worker").state(), FiberState.Active)
        self.assertEqual(self.worker.applied, 2)

    async def test_verdict_updates_reuse_outer_and_restart_inner(self) -> None:
        """Flipping an expression suppresses and later reactivates the same entry holder."""
        await self.read(
            [
                settings({"enabled": True}),
                worker({"inject": ["settings"], "disabled": {"__jsExpr": "!settings.enabled"}}),
            ]
        )
        entry = self.loader.resolve("worker")
        outer, inner = entry.outer_fiber(), self.fiber("worker")
        await self.loader.update("worker", {"disabled": {"__jsExpr": "settings.enabled"}})
        await self.loader.settled()
        self.assertTrue(inner.is_removed())
        self.assertEqual(entry.outer_fiber(), outer)
        await self.loader.update("worker", {"disabled": {"__jsExpr": "!settings.enabled"}})
        await self.loader.update("worker", {"disabled": {"__jsExpr": "settings.enabled && false"}})
        await self.loader.settled()
        self.assertEqual(self.worker.applied, 2)

    async def test_group_propagates_verdict_without_disabling_itself(self) -> None:
        """A structural group remains active and controls only its descendants' admission."""
        await self.read(
            [
                settings({"enabled": False}),
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "inject": ["settings"],
                    "disabled": {"__jsExpr": "!settings.enabled"},
                    "config": [worker({})],
                },
            ]
        )
        self.assertEqual(self.fiber("g").state(), FiberState.Active)
        self.assertIsNone(self.loader.resolve("worker").inner_fiber())
        await self.loader.update("g", {"disabled": {"__jsExpr": "false"}})
        await self.loader.settled()
        self.assertEqual(self.fiber("worker").state(), FiberState.Active)

    async def test_config_uses_plugin_inject_on_every_reload(self) -> None:
        """A plugin's own dependency grants config reads while source expressions stay written."""
        own = Counter()
        self.catalog.register_def("own", own.definition().inject(["settings"]))
        expression: JsonValue = {"value": {"__jsExpr": "settings.value"}}
        await self.read(
            [settings({"value": 1}), {"id": "own", "name": "own", "config": expression}]
        )
        self.assertEqual(own.configs, [{"value": 1}])
        await self.loader.update("settings", {"config": {"value": 2}})
        await self.loader.settled()
        self.assertEqual(own.configs, [{"value": 1}, {"value": 2}])
        self.assertEqual(self.fiber("own").config(), expression)

    async def test_config_cannot_read_undeclared_sibling(self) -> None:
        """Config interpolation fails the inner fiber before invoking the plugin."""
        await self.read(
            [settings({"value": 1}), worker({"config": {"value": {"__jsExpr": "settings.value"}}})]
        )
        self.assertEqual(self.fiber("worker").state(), FiberState.Failed)
        self.assertEqual(self.worker.applied, 0)

    async def test_disabled_precedes_plugin_dependencies(self) -> None:
        """A disabled entry does not create its plugin's independently pending inner fiber."""
        self.catalog.register_def("needy", self.worker.definition().inject(["http"]))
        await self.read(
            [
                settings({"enabled": False}),
                {
                    "id": "needy",
                    "name": "needy",
                    "inject": ["settings"],
                    "disabled": {"__jsExpr": "!settings.enabled"},
                },
            ]
        )
        entry = self.loader.resolve("needy")
        self.assertIsNone(entry.inner_fiber())
        await self.loader.update("settings", {"config": {"enabled": True}})
        await self.loader.settled()
        self.assertEqual(self.fiber("needy").state(), FiberState.Pending)

    async def test_outer_metadata_does_not_replace_plugin_declaration(self) -> None:
        """Entry and plugin injection metadata remain attached to their respective fibers."""
        self.catalog.register_def(
            "own", self.worker.definition().inject(Inject().with_meta("settings", {"own": 1}))
        )
        await self.read(
            [
                settings({}),
                {"id": "own", "name": "own", "inject": {"settings": {"namespace": "ns"}}},
            ]
        )
        outer = self.loader.resolve("own").outer_fiber()
        assert outer is not None
        self.assertEqual(outer.inject().meta("settings"), {"namespace": "ns"})
        self.assertEqual(self.fiber("own").inject().meta("settings"), {"own": 1})

    async def test_rebuild_when_inject_changes(self) -> None:
        """Adding and clearing entry injection retires the obsolete outer and inner episodes."""
        await self.read(
            [settings({}), {"id": "env", "name": "env"}, worker({"inject": ["settings"]})]
        )
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        await self.loader.update("worker", {"inject": ["settings", "env"]})
        await self.loader.settled()
        self.assertTrue(outer.is_removed())
        new_outer = entry.outer_fiber()
        assert new_outer is not None
        self.assertEqual(list(new_outer.inject().keys()), ["settings", "env"])
        await self.loader.update("worker", {"inject": None})
        await self.loader.settled()
        self.assertIsNone(entry.outer_fiber())
        self.assertEqual(self.worker.applied, 3)
        self.assertEqual(self.backend.changes[-1].options, {"id": "worker", "name": "worker"})

    async def test_self_dispose_retires_outer_but_remove_does_not_write_disabled(self) -> None:
        """Self-disposal is persisted, while loader removal retires both fibers without a flag."""
        await self.read([settings({}), worker({"inject": ["settings"]})])
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        await self.fiber("worker").dispose()
        await self.loader.settled()
        self.assertTrue(outer.is_removed())
        self.assertIsNone(entry.outer_fiber())
        options = entry.options()
        assert isinstance(options, dict)
        self.assertIs(options["disabled"], True)
        await self.loader.update("worker", {"disabled": None})
        await self.loader.settled()
        inner, outer = self.fiber("worker"), entry.outer_fiber()
        assert outer is not None
        self.loader.remove("worker")
        await self.loader.settled()
        self.assertTrue(inner.is_removed() and outer.is_removed())
        self.assertIsNone(self.backend.changes[-1].options)

    async def test_no_intercept_evaluation_while_disabled(self) -> None:
        """A disabled entry does not attempt a failing interception expression."""
        await self.read(
            [worker({"disabled": True, "intercept": {"http": {"__jsExpr": "missing.value"}}})]
        )
        self.assertEqual(self.worker.applied, 0)
        self.assertFalse(any("missing.value" in message.text for message in self.root.log_buffer()))
        await self.loader.update("worker", {"disabled": {"__jsExpr": "true"}})
        await self.loader.settled()
        self.assertFalse(any("missing.value" in message.text for message in self.root.log_buffer()))

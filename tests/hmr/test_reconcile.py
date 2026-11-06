"""Source reconcile.rs transaction and cache recovery acceptance."""

import asyncio
from pathlib import Path

import async_timeout

from pykardia import Context, FiberState, JsonValue, PluginDef
from pykardia.hmr import Exports, Hmr, HmrExt, Imports, ModuleCode
from tests.hmr.common import APP, HmrCase, Query, answering, dependent, plugin, throws, value


class ReconcileTests(HmrCase):
    """Check the running plugins alongside real cache and transaction outcomes."""

    async def test_replaces_an_entry_plugin_in_place(self) -> None:
        """Replacing a module retires the old episode while preserving the entry and fiber."""
        disposed: list[bool] = []

        async def cleanup(ctx: Context) -> None:
            """Record cleanup from the real old plugin episode."""
            ctx.on_dispose(lambda: disposed.append(True))

        id = await self.load(
            "entry", ModuleCode.new([], lambda _: Exports.plugin(answering("entry", "v1", cleanup)))
        )
        entry = self.loader.resolve("entry")
        fiber = entry.inner_fiber()
        self.source.codes[id] = plugin("entry", "v2")
        result = await self.reconcile(id)
        self.assertEqual(result.settled_fibers, [fiber])
        self.assertEqual(result.import_failures, [])
        self.assertIs(entry.inner_fiber(), fiber)
        self.assertFalse(entry.is_disabled())
        self.assertEqual(disposed, [True])
        self.assertEqual(self.query("entry"), "v2")

    async def test_imports_a_module_again_after_it_failed_in_an_aborted_attempt(self) -> None:
        """A newly introduced failed dependency retries when only its importer changes again."""
        id = await self.load("entry", plugin("entry", "v1"))
        self.source.codes[f"{APP}/dep"] = throws("added v1")
        self.source.codes[id] = plugin("entry", "v2", [f"{APP}/dep"])
        self.assertEqual(len((await self.reconcile(id)).import_failures), 1)
        self.source.codes[f"{APP}/dep"] = value(None)
        self.assertEqual((await self.reconcile(id)).import_failures, [])
        self.assertEqual(self.query("entry"), "v2")

    async def test_replaces_the_plugins_below_a_directory_root(self) -> None:
        """A directory root matches whole path segments and reaches importing plugins."""
        self.source.codes[f"{APP}/root/dep"] = value("v1")
        self.source.codes[f"{APP}/root-sibling/dep"] = value("v1")
        await self.load("root", dependent("root", f"{APP}/root/dep"))
        await self.load("sibling", dependent("sibling", f"{APP}/root-sibling/dep"))
        self.source.codes[f"{APP}/root/dep"] = value("v2")
        self.source.codes[f"{APP}/root-sibling/dep"] = value("v2")
        result = await self.reconcile(f"{APP}/root/")
        self.assertEqual(len(result.settled_fibers), 1)
        self.assertEqual((self.query("root"), self.query("sibling")), ("v2", "v1"))

    async def test_rejects_a_relative_path_as_a_root(self) -> None:
        """A relative root fails before any module cache or plugin changes."""
        id = await self.load("relative", plugin("relative", "v1"))
        self.source.codes[id] = plugin("relative", "v2")
        with self.assertRaisesRegex(ValueError, "expected an absolute path"):
            await self.reconcile("./relative")
        self.assertEqual(self.query("relative"), "v1")

    async def test_rejects_with_the_error_of_the_callback(self) -> None:
        """Callback failures propagate intact and leave running code untouched."""
        id = await self.load("callback", plugin("callback", "v1"))
        self.source.codes[id] = plugin("callback", "v2")

        async def fail() -> list[Path]:
            """Raise from inside the actual transaction callback."""
            raise ValueError("callback failed")

        with self.assertRaisesRegex(ValueError, "^callback failed$"):
            await self.hmr.reconcile(fail)
        self.assertEqual(self.query("callback"), "v1")

    async def test_runs_the_callback_inside_the_transaction(self) -> None:
        """A queued callback waits for the first callback and same-task recursion is rejected."""
        order: list[str] = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def empty() -> list[Path]:
            """Supply no roots to the attempted nested transaction."""
            return []

        async def first() -> list[Path]:
            """Reject nesting before holding the queue until the test releases it."""
            with self.assertRaisesRegex(ValueError, "cannot start a transaction"):
                await self.hmr.reconcile(empty)
            entered.set()
            await release.wait()
            order.append("callback")
            return []

        async def second() -> list[Path]:
            """Record entry to the second callback after the queue grants its turn."""
            order.append("second")
            return []

        async with async_timeout.timeout(5):
            a = asyncio.create_task(self.hmr.reconcile(first))
            await entered.wait()
            b = asyncio.create_task(self.hmr.reconcile(second))
            await asyncio.sleep(0.02)
            self.assertEqual(order, [])
            release.set()
            await asyncio.gather(a, b)
        self.assertEqual(order, ["callback", "second"])

    async def test_replaces_a_plugin_that_no_entry_manages(self) -> None:
        """A directly instantiated module plugin still participates in registry replacement."""
        self.source.codes[f"{APP}/direct"] = plugin("direct", "v1")
        definition = self.graph.import_(f"{APP}/direct").plugin_def
        assert definition is not None
        fiber = self.root.plugin_def(definition)
        await fiber.settled()
        self.source.codes[f"{APP}/direct"] = plugin("direct", "v2")
        result = await self.reconcile(f"{APP}/direct")
        self.assertEqual(result.settled_fibers, [fiber])
        self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(self.query("direct"), "v2")

    async def test_refreshes_a_disabled_plugin_for_when_it_is_enabled_again(self) -> None:
        """Eviction while disabled makes the next enable load the latest dependency."""
        self.source.codes[f"{APP}/dep"] = value("v1")
        await self.load("disabled", dependent("disabled", f"{APP}/dep"))
        await self.loader.update("disabled", {"disabled": True})
        self.source.codes[f"{APP}/dep"] = value("v2")
        await self.reconcile(f"{APP}/dep")
        await self.loader.update("disabled", {"disabled": False})
        await self.loader.settled()
        self.assertEqual(self.query("disabled"), "v2")

    async def test_refreshes_a_module_that_no_plugin_depends_on_for_its_next_import(self) -> None:
        """Unowned changed modules are evicted and reevaluate lazily at their next import."""
        self.source.codes[f"{APP}/data"] = value("v1")
        self.assertEqual(self.graph.import_(f"{APP}/data").json_value, "v1")
        self.source.codes[f"{APP}/data"] = value("v2")
        result = await self.reconcile(f"{APP}/data")
        self.assertEqual(result.settled_fibers, [])
        self.assertFalse(self.graph.is_loaded(f"{APP}/data"))
        self.assertEqual(self.graph.import_(f"{APP}/data").json_value, "v2")

    async def test_does_not_apply_a_unit_whose_module_fails_to_import(self) -> None:
        """A failed module restores its cache and leaves its old plugin running for retry."""
        id = await self.load("abort", plugin("abort", "v1"))
        original = self.graph.exports(id)
        self.source.codes[id] = throws("BROKEN")
        result = await self.reconcile(id)
        self.assertEqual(result.settled_fibers, [])
        self.assertEqual(len(result.import_failures), 1)
        self.assertEqual(result.import_failures[0].modules, [id])
        self.assertEqual(self.graph.exports(id), original)
        self.assertEqual(self.query("abort"), "v1")
        self.source.codes[id] = plugin("abort", "v2")
        await self.reconcile(id)
        self.assertEqual(self.query("abort"), "v2")

    async def test_does_not_apply_a_unit_whose_module_does_not_export_a_plugin(self) -> None:
        """A non-plugin replacement leaves the original listener and fiber intact."""
        id = await self.load("invalid", plugin("invalid", "v1"))
        fiber = self.loader.resolve("invalid").inner_fiber()
        self.source.codes[id] = value("not a plugin")
        result = await self.reconcile(id)
        self.assertEqual(len(result.import_failures), 1)
        self.assertEqual(
            str(result.import_failures[0].error), f"invalid plugin at {id}, expect a plugin export"
        )
        self.assertIs(self.loader.resolve("invalid").inner_fiber(), fiber)
        self.assertEqual(self.query("invalid"), "v1")

    async def test_applies_a_unit_when_another_unit_fails_to_import(self) -> None:
        """Sharing only an unchanged dependency does not couple two replacement units."""
        self.source.codes[f"{APP}/common"] = value("common")
        broken = await self.load("broken", plugin("broken", "v1", [f"{APP}/common"]))
        healthy = await self.load("healthy", plugin("healthy", "v1", [f"{APP}/common"]))
        self.source.codes[broken] = throws("BROKEN", [f"{APP}/common"])
        self.source.codes[healthy] = plugin("healthy", "v2", [f"{APP}/common"])
        result = await self.reconcile(broken, healthy)
        self.assertEqual(len(result.settled_fibers), 1)
        self.assertEqual(len(result.import_failures), 1)
        self.assertEqual(result.import_failures[0].modules, [broken])
        self.assertEqual((self.query("broken"), self.query("healthy")), ("v1", "v2"))
        self.source.codes[broken] = plugin("broken", "v2", [f"{APP}/common"])
        self.assertEqual((await self.reconcile(broken)).import_failures, [])
        self.assertEqual(self.query("broken"), "v2")

    async def test_aborts_plugins_that_share_a_changed_module_together(self) -> None:
        """One failure aborts both plugins that share an evicted dependency."""
        self.source.codes[f"{APP}/dep"] = value("v1")
        left = await self.load("left", dependent("left", f"{APP}/dep"))
        right = await self.load("right", dependent("right", f"{APP}/dep"))
        self.source.codes[f"{APP}/dep"] = value("v2")
        self.source.codes[left] = throws("left failed", [f"{APP}/dep"])
        result = await self.reconcile(f"{APP}/dep", left)
        self.assertEqual(result.settled_fibers, [])
        self.assertEqual(len(result.import_failures), 1)
        self.assertEqual(result.import_failures[0].modules, sorted([f"{APP}/dep", left, right]))
        self.assertEqual((self.query("left"), self.query("right")), ("v1", "v1"))

    async def test_aborts_a_plugin_that_newly_imports_a_changed_module_of_a_failed_unit(
        self,
    ) -> None:
        """New dependency edges join an otherwise independent replacement to a failed unit."""
        self.source.codes[f"{APP}/dep"] = value("v1")
        owner = await self.load("owner", dependent("owner", f"{APP}/dep"))
        adopter = await self.load("adopter", plugin("adopter", "v1"))
        self.source.codes[f"{APP}/dep"] = value("v2")
        self.source.codes[owner] = throws("owner failed", [f"{APP}/dep"])
        self.source.codes[adopter] = dependent("adopter", f"{APP}/dep")
        result = await self.reconcile(f"{APP}/dep", owner, adopter)
        self.assertEqual(result.settled_fibers, [])
        self.assertEqual(len(result.import_failures), 1)
        self.assertIn(adopter, result.import_failures[0].modules)
        self.assertEqual((self.query("owner"), self.query("adopter")), ("v1", "v1"))

    async def test_restores_a_changed_module_outside_every_plugin_with_the_failed_unit_it_imports(
        self,
    ) -> None:
        """Unowned old dependents are restored when they reach only an aborted unit."""
        self.source.codes[f"{APP}/dep"] = value("v1")
        self.source.codes[f"{APP}/outside"] = ModuleCode.new(
            [f"{APP}/dep"], lambda imports: Exports.value(imports.value(f"{APP}/dep"))
        )
        self.graph.import_(f"{APP}/outside")
        owner = await self.load("owner", dependent("owner", f"{APP}/dep"))
        self.source.codes[f"{APP}/dep"] = value("v2")
        self.source.codes[f"{APP}/outside"] = value("changed")
        self.source.codes[owner] = throws("owner failed", [f"{APP}/dep"])
        result = await self.reconcile(f"{APP}/dep", f"{APP}/outside", owner)
        self.assertEqual(len(result.import_failures), 1)
        self.assertIn(f"{APP}/outside", result.import_failures[0].modules)
        self.assertEqual(self.graph.import_(f"{APP}/outside").json_value, "v1")

    async def test_leaves_a_fiber_failed_when_the_new_plugin_fails_to_apply(self) -> None:
        """Stage-three apply errors keep the committed version failed and allow later retry."""
        id = await self.load("failed", plugin("failed", "v1"))

        async def fail(ctx: Context) -> None:
            """Fail during actual application after the module has successfully evaluated."""
            raise ValueError("apply failed")

        self.source.codes[id] = ModuleCode.new(
            [], lambda _: Exports.plugin(answering("failed", "v2", fail))
        )
        result = await self.reconcile(id)
        self.assertEqual(result.import_failures, [])
        self.assertEqual(len(result.settled_fibers), 1)
        fiber = result.settled_fibers[0]
        self.assertEqual(fiber.state(), FiberState.Failed)
        with self.assertRaisesRegex(ValueError, "apply failed"):
            await fiber.settled()
        self.source.codes[id] = plugin("failed", "v3")
        self.assertEqual((await self.reconcile(id)).settled_fibers, [fiber])
        self.assertEqual(self.query("failed"), "v3")

    async def test_skips_a_changed_host_module_below_a_root_and_applies_the_other_changes(
        self,
    ) -> None:
        """Host marks suppress only their own reload while neighboring plugins update."""
        host = await self.load("host/host", plugin("host", "v1"))
        other = await self.load("host/other", plugin("other", "v1"))
        self.graph.mark_host([host])
        self.source.codes[host], self.source.codes[other] = (
            plugin("host", "v2"),
            plugin("other", "v2"),
        )
        result = await self.reconcile(f"{APP}/host")
        self.assertEqual(result.skipped_host_modules, [host])
        self.assertEqual((self.query("host"), self.query("other")), ("v1", "v2"))

    async def test_rejects_a_transaction_started_inside_another_one(self) -> None:
        """Reloaded plugin callers cannot nest a transaction even from a different asyncio task."""
        outcomes: list[str] = []

        async def roots() -> list[Path]:
            """Supply an empty nested transaction root list."""
            return []

        async def nested(ctx: Context) -> None:
            """Attempt actual caller-attributed reconciliation during application."""
            try:
                await HmrExt(ctx.get(Hmr)).reconcile(roots)
                outcomes.append("resolved")
            except ValueError as error:
                outcomes.append(str(error))

        def code() -> ModuleCode:
            """Build a fresh plugin that declares access to the HMR service."""
            return ModuleCode.new(
                [], lambda _: Exports.plugin(answering("nested", None, nested).inject(["hmr"]))
            )

        id = await self.load("nested", code())
        self.assertEqual(outcomes, ["resolved"])
        self.source.codes[id] = code()
        result = await self.reconcile(id)
        self.assertEqual(
            outcomes, ["resolved", "cannot start a transaction inside another transaction"]
        )
        await HmrExt(result.settled_fibers[0].context().get(Hmr)).reconcile(roots)

    async def test_replaces_a_service(self) -> None:
        """Service consumers resolve the provision from the newly applied plugin episode."""

        def service(version: str) -> ModuleCode:
            """Build an actual provider plugin for the requested version."""

            async def provide(ctx: Context) -> None:
                """Publish the version using the real context provision API."""
                ctx.provide_as("reconcileService", version)

            return ModuleCode.new([], lambda _: Exports.plugin(answering("service", None, provide)))

        id = await self.load("service", service("v1"))
        self.assertEqual(self.root.get_as("reconcileService", str).arc(), "v1")
        self.source.codes[id] = service("v2")
        await self.reconcile(id)
        self.assertEqual(self.root.get_as("reconcileService", str).arc(), "v2")

    async def test_replaces_a_plugin_when_one_of_its_dependencies_changes(self) -> None:
        """Sharing an unchanged dependency does not reload an unaffected sibling."""
        self.source.codes[f"{APP}/dep"] = value("v1")
        self.source.codes[f"{APP}/common"] = value("common")
        await self.load(
            "dependent",
            ModuleCode.new(
                [f"{APP}/dep", f"{APP}/common"],
                lambda imports: Exports.plugin(answering("dependent", imports.value(f"{APP}/dep"))),
            ),
        )
        applies: list[bool] = []

        async def counted(ctx: Context) -> None:
            """Count actual sibling episodes to detect an unnecessary reload."""
            applies.append(True)

        await self.load(
            "sibling",
            ModuleCode.new(
                [f"{APP}/common"], lambda _: Exports.plugin(answering("sibling", None, counted))
            ),
        )
        self.source.codes[f"{APP}/dep"] = value("v2")
        self.assertEqual(len((await self.reconcile(f"{APP}/dep")).settled_fibers), 1)
        self.assertEqual(self.query("dependent"), "v2")
        self.assertEqual(applies, [True])

    async def test_replaces_the_inner_fiber_of_an_entry_with_an_injection_scope_in_place(
        self,
    ) -> None:
        """Entry injection remains in its outer fiber while HMR replaces the inner definition."""
        self.root.provide_as("reconcileScope", None)
        self.source.codes[f"{APP}/scoped"] = plugin("scoped", "v1")
        await self.loader.create(
            {"id": "scoped", "name": f"{APP}/scoped", "inject": ["reconcileScope"]}
        )
        await self.loader.settled()
        entry = self.loader.resolve("scoped")
        outer, inner = entry.outer_fiber(), entry.inner_fiber()
        self.source.codes[f"{APP}/scoped"] = plugin("scoped", "v2")
        result = await self.reconcile(f"{APP}/scoped")
        self.assertEqual(result.settled_fibers, [inner])
        self.assertIs(entry.outer_fiber(), outer)
        self.assertIs(entry.inner_fiber(), inner)
        self.assertEqual(self.query("scoped"), "v2")

    async def test_keeps_the_metadata_of_an_entry_that_re_exports_the_changed_plugin(self) -> None:
        """The outermost exporter supplies dependency and validation metadata after replacement."""
        self.root.provide_as("reconcileGate", None)

        def implementation(version: str) -> ModuleCode:
            """Build a plugin that reports the actual validated configuration."""

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Expose both module version and decoded configuration through its listener."""
                ctx.on(
                    Query,
                    lambda q: (
                        {"version": version, "config": config} if q.name == "reexport" else None
                    ),
                )

            return ModuleCode.new([], lambda _: Exports.plugin(PluginDef.new(apply)))

        def export(imports: Imports) -> Exports:
            """Decorate the same plugin identity at the outermost module boundary."""
            exported = imports.get(f"{APP}/implementation")
            assert exported is not None and exported.plugin_def is not None

            def validate(config: JsonValue) -> JsonValue:
                """Add the source test's marker through real configuration validation."""
                assert isinstance(config, dict)
                return {**config, "validated": True}

            return Exports.plugin(
                exported.plugin_def.inject(["reconcileGate"]).validate_with(validate)
            )

        self.source.codes[f"{APP}/implementation"] = implementation("v1")
        self.source.codes[f"{APP}/exporter"] = ModuleCode.new([f"{APP}/implementation"], export)
        original = self.graph.import_(f"{APP}/exporter").plugin_def
        assert original is not None
        await self.root.plugin_def(original, {}).settled()
        self.source.codes[f"{APP}/implementation"] = implementation("v2")
        result = await self.reconcile(f"{APP}/implementation")
        self.assertEqual(len(result.settled_fibers), 1)
        self.assertEqual(
            [str(key) for key in result.settled_fibers[0].inject().keys()], ["reconcileGate"]
        )
        self.assertEqual(self.query("reexport"), {"version": "v2", "config": {"validated": True}})
        refreshed = self.graph.import_(f"{APP}/exporter").plugin_def
        assert refreshed is not None
        self.assertNotEqual(refreshed.key(), original.key())
        self.assertEqual(self.root.registry().get(refreshed), result.settled_fibers[0].runtime())

    async def test_imports_a_module_again_after_its_evaluation_failed_for_a_reason_outside_its_file(
        self,
    ) -> None:
        """An empty reconcile does not prevent a previously failing evaluation from retrying."""
        ready = False

        def evaluate(imports: Imports) -> Exports:
            """Read a real external readiness condition during source evaluation."""
            if not ready:
                raise ValueError("not ready")
            return Exports.value("ready")

        self.source.codes[f"{APP}/transient"] = ModuleCode.new([], evaluate)
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.graph.import_(f"{APP}/transient")
        ready = True
        await self.reconcile()
        self.assertEqual(self.graph.import_(f"{APP}/transient").json_value, "ready")

    async def test_keeps_a_module_that_is_still_being_imported(self) -> None:
        """An ongoing source evaluation on another thread survives an empty transaction."""
        import threading

        entered, release = threading.Event(), threading.Event()
        evaluations: list[bool] = []

        def evaluate(imports: Imports) -> Exports:
            """Pause actual module evaluation on the source test's thread boundary."""
            entered.set()
            if not release.wait(5):
                raise TimeoutError("source gate timed out")
            evaluations.append(True)
            return Exports.value(len(evaluations))

        self.source.codes[f"{APP}/pending"] = ModuleCode.new([], evaluate)
        first = asyncio.create_task(asyncio.to_thread(self.graph.import_, f"{APP}/pending"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            await self.reconcile()
        finally:
            release.set()
        self.assertEqual(await first, self.graph.import_(f"{APP}/pending"))
        self.assertEqual(evaluations, [True])

    async def test_keeps_the_job_of_a_module_when_its_job_of_another_type_fails(self) -> None:
        """A new shared module survives its first importer's failure if another unit uses it."""
        failing = await self.load("failing", plugin("failing", "v1"))
        importer = await self.load("importer", plugin("slot", "v1"))
        count: list[bool] = []

        def counted(imports: Imports) -> Exports:
            """Return a different value for every real evaluation of the new module."""
            count.append(True)
            return Exports.value(len(count))

        self.source.codes[f"{APP}/shared"] = ModuleCode.new([], counted)
        self.source.codes[failing] = throws("failing failed", [f"{APP}/shared"])
        self.source.codes[importer] = dependent("slot", f"{APP}/shared")
        result = await self.reconcile(failing, importer)
        self.assertEqual((len(result.settled_fibers), len(result.import_failures)), (1, 1))
        await self.reconcile()
        self.assertEqual(self.query("slot"), self.graph.import_(f"{APP}/shared").json_value)
        self.assertEqual(len(count), 1)

    async def test_follows_the_dependencies_of_a_module_job_when_its_job_of_another_type_fails_to_link(  # noqa: E501
        self,
    ) -> None:
        """New intermediates linked to an aborted old dependency are removed before reuse."""
        self.source.codes[f"{APP}/core"] = value("v1")
        owner = await self.load("owner", dependent("owner", f"{APP}/core"))
        failing = await self.load("failing", plugin("failing", "v1"))
        importer = await self.load("importer", plugin("edge", "v1"))
        self.source.codes[f"{APP}/edge"] = ModuleCode.new(
            [f"{APP}/core"], lambda imports: Exports.value(imports.value(f"{APP}/core"))
        )
        self.source.codes[f"{APP}/core"] = value("v2")
        self.source.codes[owner] = throws("owner failed", [f"{APP}/core"])
        self.source.codes[failing] = throws("failing failed", [f"{APP}/edge"])
        self.source.codes[importer] = dependent("edge", f"{APP}/edge")
        result = await self.reconcile(f"{APP}/core", owner, failing, importer)
        self.assertEqual(result.settled_fibers, [])
        self.assertEqual(self.query("edge"), "v1")
        self.assertFalse(self.graph.is_loaded(f"{APP}/edge"))
        self.assertEqual(self.graph.import_(f"{APP}/edge").json_value, "v1")

    async def test_replaces_a_plugin_exported_from_a_data_url(self) -> None:
        """A non-path exporter is refreshed through its changed path dependency."""
        self.source.codes[f"{APP}/implementation"] = plugin("data", "v1")
        url = "data:text/rust,pub use data_impl::*"

        def export(imports: Imports) -> Exports:
            """Forward the real dependency export without changing definition identity."""
            exported = imports.get(f"{APP}/implementation")
            assert exported is not None
            return exported

        self.source.codes[url] = ModuleCode.new([f"{APP}/implementation"], export)
        definition = self.graph.import_(url).plugin_def
        assert definition is not None
        await self.root.plugin_def(definition).settled()
        self.source.codes[f"{APP}/implementation"] = plugin("data", "v2")
        self.assertEqual(len((await self.reconcile(f"{APP}/implementation")).settled_fibers), 1)
        self.assertEqual(self.query("data"), "v2")

    async def test_shares_a_module_first_loaded_by_a_failed_unit_with_a_unit_that_imports_it_dynamically(  # noqa: E501
        self,
    ) -> None:
        """An unrecorded dynamic import can retain a new module loaded by a failed attempt."""
        failing = await self.load("failing", plugin("failing", "v1"))
        importer = await self.load("importer", plugin("importer", "v1"))
        count: list[bool] = []

        def counted(imports: Imports) -> Exports:
            """Expose real evaluation count as the shared module value."""
            count.append(True)
            return Exports.value(len(count))

        self.source.codes[f"{APP}/shared"] = ModuleCode.new([], counted)
        self.source.codes[failing] = throws("failing failed", [f"{APP}/shared"])
        self.source.codes[importer] = ModuleCode.new(
            [],
            lambda _: Exports.plugin(
                answering("importer", self.graph.import_(f"{APP}/shared").json_value)
            ),
        )
        result = await self.reconcile(failing, importer)
        self.assertEqual((len(result.settled_fibers), len(result.import_failures)), (1, 1))
        self.assertEqual(self.query("importer"), self.graph.import_(f"{APP}/shared").json_value)
        self.assertEqual(len(count), 1)

    async def test_replaces_a_plugin_when_a_module_inside_a_package_changes(self) -> None:
        """A transitive package-internal dependency invalidates its importing plugin."""
        self.source.codes[f"{APP}/package/lib"] = value("v1")
        self.source.codes[f"{APP}/package/index"] = ModuleCode.new(
            [f"{APP}/package/lib"],
            lambda imports: Exports.value(imports.value(f"{APP}/package/lib")),
        )
        await self.load("package", dependent("package", f"{APP}/package/index"))
        self.source.codes[f"{APP}/package/lib"] = value("v2")
        self.assertEqual(len((await self.reconcile(f"{APP}/package/lib")).settled_fibers), 1)
        self.assertEqual(self.query("package"), "v2")

    async def test_runs_transactions_one_at_a_time(self) -> None:
        """A later transaction cannot apply its plugin until the first has settled."""
        slow = await self.load("slow", plugin("slow", "v1"))
        fast = await self.load("fast", plugin("fast", "v1"))
        order: list[str] = []

        async def delay(ctx: Context) -> None:
            """Keep the first real plugin application pending before marking completion."""
            await asyncio.sleep(0.1)
            order.append("slow done")

        async def applied(ctx: Context) -> None:
            """Record entry into the second plugin's actual application."""
            order.append("fast applied")

        self.source.codes[slow] = ModuleCode.new(
            [], lambda _: Exports.plugin(answering("slow", "v2", delay))
        )
        self.source.codes[fast] = ModuleCode.new(
            [], lambda _: Exports.plugin(answering("fast", "v2", applied))
        )
        first, second = await asyncio.gather(self.reconcile(slow), self.reconcile(fast))
        self.assertEqual((len(first.settled_fibers), len(second.settled_fibers)), (1, 1))
        self.assertEqual(order, ["slow done", "fast applied"])

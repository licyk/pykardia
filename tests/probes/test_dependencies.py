"""Verify real parser, filesystem notification and WASM embedding capabilities."""

import tempfile
import threading
import unittest
from pathlib import Path

import wasmtime
import yaml
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from yaml.events import AliasEvent, ScalarEvent

from pykardia.core.runtime import Action, Runtime


class DependencyTests(unittest.TestCase):
    """Exercise dependency APIs required by the reference implementation."""

    def test_yaml_retains_tags_anchors_styles_and_positions(self) -> None:
        """Read syntax events without applying PyYAML's default value schema."""
        text = 'name: &anchor !!js "服务.enabled"\ncopy: *anchor\nflag: yes\n'
        events = list(yaml.parse(text))
        value = next(event for event in events if isinstance(event, ScalarEvent) and event.anchor)
        self.assertEqual(value.value, "服务.enabled")
        self.assertEqual(value.tag, "tag:yaml.org,2002:js")
        self.assertEqual(value.style, '"')
        self.assertEqual(value.start_mark.line, 0)
        self.assertEqual(value.start_mark.column, 6)
        alias = next(event for event in events if isinstance(event, AliasEvent))
        self.assertEqual(alias.anchor, "anchor")
        self.assertEqual(alias.start_mark.line, 1)
        scalars = [event.value for event in events if isinstance(event, ScalarEvent)]
        self.assertEqual(scalars[-1], "yes")

    def test_watch_survives_atomic_file_replacement(self) -> None:
        """Observe a rename target and subsequent writes through its directory."""
        changed = threading.Event()

        class Handler(FileSystemEventHandler):
            """Signal changes to the desired filename on the actual observer thread."""

            def on_any_event(self, event: FileSystemEvent) -> None:
                """Accept actual file creation, modification or move notifications."""
                paths = (str(event.src_path), str(event.dest_path))
                if event.event_type in {"created", "modified", "moved"} and any(
                    path.endswith("config.yml") for path in paths
                ):
                    changed.set()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text("old", encoding="utf-8")
            observer = Observer()
            observer.schedule(Handler(), directory, recursive=True)
            observer.start()
            try:
                staged = Path(directory) / "staged"
                staged.write_text("new", encoding="utf-8")
                staged.replace(path)
                self.assertTrue(changed.wait(5), "no atomic replacement notification")
                changed.clear()
                path.write_text("again", encoding="utf-8")
                self.assertTrue(changed.wait(5), "watch lost after rename")
            finally:
                observer.stop()
                observer.join(5)
            self.assertFalse(observer.is_alive())

    def test_wasm_memory_callback_and_instance_isolation(self) -> None:
        """Run a core WAT module with the cordis import namespace and separate stores."""
        engine = wasmtime.Engine()
        module = wasmtime.Module(
            engine,
            '(module (import "cordis" "write" (func $write (param i32 i32))) '
            '(memory (export "memory") 1) (data (i32.const 0) "ok") '
            '(func (export "cordis_meta") (result i32) '
            "i32.const 0 i32.const 2 call $write i32.const 0))",
        )
        output: list[bytes] = []
        runtime = Runtime()
        state: list[str] = []

        def write(caller: wasmtime.Caller, pointer: int, length: int) -> None:
            """Read the actual guest memory during a host callback."""
            memory = caller.get("memory")
            assert isinstance(memory, wasmtime.Memory)
            output.append(bytes(memory.read(caller, pointer, pointer + length)))
            state.append(runtime.read(lambda: state[0]))

        linker = wasmtime.Linker(engine)
        linker.define_func(
            "cordis",
            "write",
            wasmtime.FuncType([wasmtime.ValType.i32()] * 2, []),
            write,
            access_caller=True,
        )
        first = wasmtime.Store(engine)
        second = wasmtime.Store(engine)
        one = linker.instantiate(first, module).exports(first)
        two = linker.instantiate(second, module).exports(second)
        call = one["cordis_meta"]
        memory = one["memory"]
        other_memory = two["memory"]
        assert isinstance(call, wasmtime.Func)
        assert isinstance(memory, wasmtime.Memory)
        assert isinstance(other_memory, wasmtime.Memory)

        def invoke() -> None:
            """Invoke guest code outside the state lock so its host callback can re-enter."""
            self.assertEqual(call(first), 0)

        def transition() -> tuple[None, list[Action]]:
            """Commit state before synchronously entering the guest as a queued action."""
            state.append("committed")
            return None, [invoke]

        runtime.run(transition)
        self.assertEqual(output, [b"ok"])
        self.assertEqual(state, ["committed", "committed"])
        memory.write(first, b"no", 0)
        self.assertEqual(other_memory.read(second, 0, 2), b"ok")

    def test_core_host_does_not_supply_wasi(self) -> None:
        """Expose the import mismatch of a WASI-dependent Python guest."""
        engine = wasmtime.Engine()
        module = wasmtime.Module(
            engine,
            '(module (import "wasi_snapshot_preview1" "fd_write" '
            "(func (param i32 i32 i32 i32) (result i32))))",
        )
        with self.assertRaisesRegex(wasmtime.WasmtimeError, "unknown import"):
            wasmtime.Linker(engine).instantiate(wasmtime.Store(engine), module)

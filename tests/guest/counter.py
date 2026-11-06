"""A typed Python guest used unchanged by every exported transport."""

from dataclasses import dataclass

from typing_extensions import Self

from pykardia.guest import Error, Guest, Host, JsonValue, Level, Meta, Request, serve


@dataclass
class CounterConfig:
    """Initial per-instance state, with defaults used for null guest config."""

    initial: int = 0


class Counter(Guest[CounterConfig]):
    """A per-fiber counter exposing all guest SDK host callback operations."""

    Config = CounterConfig

    def __init__(self, count: int) -> None:
        """Retain only this instance's mutable counter."""
        self.count = count

    @staticmethod
    def meta() -> Meta:
        """Declare one counter plugin and its provided service."""
        return Meta.plugin("python-counter").provide("counter")

    @classmethod
    def apply(cls, host: Host, config: CounterConfig, imports: dict[str, JsonValue]) -> Self:
        """Provide the initial view, log application and create independent mutable state."""
        host.provide("counter", {"initial": config.initial, "imports": imports})
        host.log(Level.Info, "Python guest applied 你好")
        return cls(config.initial)

    def call(self, host: Host, request: Request) -> JsonValue:
        """Handle counter updates, host forwarding and explicit error/print cases."""
        if request.method == "increment":
            assert isinstance(request.args, int)
            self.count += request.args
            return self.count
        if request.method == "view":
            assert isinstance(request.args, str)
            return host.view(request.args)
        if request.method == "invoke":
            assert isinstance(request.args, dict)
            key, method = request.args["key"], request.args["method"]
            assert isinstance(key, str) and isinstance(method, str)
            return host.invoke(key, method, request.args.get("args"))
        if request.method == "fail":
            raise Error("counter refuses")
        if request.method == "raise":
            raise ValueError("Python exception 你好")
        if request.method == "exit":
            raise SystemExit("guest requested exit")
        if request.method == "print":
            print("application output 你好")
            return self.count
        return super().call(host, request)

    def dispose(self, host: Host) -> None:
        """Report release through the original host logging ABI."""
        host.log(Level.Info, "Python guest disposed")


if __name__ == "__main__":
    serve(Counter)


class Proxy(Counter):
    """Forward real calls to another guest while exposing imported dependency data."""

    @staticmethod
    def meta() -> Meta:
        """Declare both external services and one static value dependency."""
        return (
            Meta.plugin("python-proxy")
            .inject("counter")
            .inject("nothing")
            .provide("proxy")
            .dep("data")
        )

    @classmethod
    def apply(cls, host: Host, config: CounterConfig, imports: dict[str, JsonValue]) -> Self:
        """Invoke the existing counter during apply and publish the actual imported values."""
        host.invoke("counter", "increment", config.initial)
        host.provide("proxy", {"imports": imports})
        return cls(0)

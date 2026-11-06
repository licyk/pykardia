"""Export this typed Python counter as a process, dynamic library or bare-WASM guest."""

from dataclasses import dataclass

from typing_extensions import Self

from pykardia.guest import Error, Guest, Host, JsonValue, Level, Meta, Request, serve


@dataclass
class CounterConfig:
    """Initial counter value supplied in the loader entry's config."""

    initial: int = 0


class Counter(Guest[CounterConfig]):
    """Keep one independent counter per host plugin fiber."""

    Config = CounterConfig

    def __init__(self, value: int) -> None:
        """Create the state of one applied guest instance."""
        self.value = value

    @staticmethod
    def meta() -> Meta:
        """Declare the plugin and its callable service."""
        return Meta.plugin("counter").provide("counter")

    @classmethod
    def apply(cls, host: Host, config: CounterConfig, imports: dict[str, JsonValue]) -> Self:
        """Publish an initial JSON view and initialize this instance."""
        host.provide("counter", {"initial": config.initial})
        host.log(Level.Info, "counter ready")
        return cls(config.initial)

    def call(self, host: Host, request: Request) -> JsonValue:
        """Increment by an integer, returning an explicit SDK error for invalid requests."""
        if request.method != "increment":
            return super().call(host, request)
        if type(request.args) is not int:
            raise Error("increment expects an integer")
        self.value += request.args
        return self.value

    def dispose(self, host: Host) -> None:
        """Report disposal through the host logger."""
        host.log(Level.Info, "counter disposed")


if __name__ == "__main__":
    serve(Counter)

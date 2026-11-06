"""Python declarations corresponding to Rust's service derive and plugin attribute macros."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from types import FunctionType, NoneType
from typing import TypeVar, cast, get_args, get_origin, get_type_hints, overload

from typing_extensions import TypeForm

from pykardia.core.config import decode
from pykardia.core.context import Context
from pykardia.core.deps import Deps, Injected
from pykardia.core.plugin import Inject, PluginDef
from pykardia.core.service import Service
from pykardia.core.value import JsonValue

S = TypeVar("S", bound=Service)
PluginFunction = Callable[..., Awaitable[None]]


@overload
def service(cls: type[S], *, name: str | None = None) -> type[S]:
    """Decorate a service class directly."""
    ...


@overload
def service(cls: None = None, *, name: str | None = None) -> Callable[[type[S]], type[S]]:
    """Configure a service decorator before applying it to a class."""
    ...


def service(
    cls: type[S] | None = None, *, name: str | None = None
) -> type[S] | Callable[[type[S]], type[S]]:
    """Bind a Service subclass to an explicit or hyphenated name without replacing its identity."""

    def decorate(target: type[S]) -> type[S]:
        """Assign only the declaration metadata, retaining user methods and constructors."""
        if not issubclass(target, Service):
            raise TypeError("a service class must inherit Service")
        target.NAME = name if name is not None else declaration_name(target.__name__)
        return target

    return decorate if cls is None else decorate(cls)


@overload
def plugin(
    function: PluginFunction, *, name: str | None = None, provide: Iterable[str] = ()
) -> PluginDef:
    """Turn an annotated async function directly into a reusable definition."""
    ...


@overload
def plugin(
    function: None = None, *, name: str | None = None, provide: Iterable[str] = ()
) -> Callable[[PluginFunction], PluginDef]:
    """Configure name and declared provisions before decorating a function."""
    ...


def plugin(
    function: PluginFunction | None = None, *, name: str | None = None, provide: Iterable[str] = ()
) -> PluginDef | Callable[[PluginFunction], PluginDef]:
    """Build a reusable definition from context and optional typed config annotations."""
    provisions = tuple(provide)

    def decorate(target: PluginFunction) -> PluginDef:
        """Validate the declaration once, then compose ordinary typed plugin machinery."""
        dependencies, config_type, has_config = declaration(target)

        def decoder(value: JsonValue) -> object:
            """Decode only at the configuration boundary using the declared type."""
            return decode(value, config_type)

        async def apply(context: Context, config: object) -> None:
            """Invoke the annotated function with its known context and decoded config types."""
            argument = context if dependencies is None else Injected[object](context, dependencies)
            if has_config:
                await target(argument, config)
            else:
                await target(argument)

        definition = (
            PluginDef.typed(apply, decoder)
            .name(
                name if name is not None else declaration_name(cast(FunctionType, target).__name__)
            )
            .provides(provisions)
        )
        return (
            definition if dependencies is None else definition.inject(Inject(dependencies.keys()))
        )

    return decorate if function is None else decorate(function)


def declaration(function: PluginFunction) -> tuple[Deps | None, TypeForm[object], bool]:
    """Read function annotations once and reject declarations the runtime cannot execute."""
    if not inspect.iscoroutinefunction(function):
        raise TypeError("a plugin function must be async")
    parameters = list(inspect.signature(function).parameters.values())
    if len(parameters) not in (1, 2) or any(
        parameter.kind
        not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ):
        raise TypeError("expected one context parameter and an optional configuration parameter")
    hints = get_type_hints(function)
    context_type = hints.get(parameters[0].name)
    dependencies: Deps | None = None
    if get_origin(context_type) is Injected:
        declared = get_args(context_type)
        if len(declared) != 1 or get_origin(declared[0]) is not tuple:
            raise TypeError("Injected must declare a tuple of service types")
        services = get_args(declared[0])
        if any(not isinstance(item, type) or not issubclass(item, Service) for item in services):
            raise TypeError("Injected dependencies must be Service types")
        dependencies = Deps(*services)
    elif context_type is not Context:
        raise TypeError("the first plugin parameter must be Context or Injected[tuple[...]]")
    config_type = hints.get(parameters[1].name) if len(parameters) == 2 else NoneType
    if config_type is None:
        raise TypeError("the configuration parameter must have a type annotation")
    return dependencies, cast(TypeForm[object], config_type), len(parameters) == 2


def declaration_name(name: str) -> str:
    """Apply the macro's ASCII case conversion, including separate acronym capitals."""
    result = ""
    for index, character in enumerate(name):
        if character == "_":
            result += "-"
        elif "A" <= character <= "Z":
            if index > 0 and not result.endswith("-"):
                result += "-"
            result += character.lower()
        else:
            result += character
    return result

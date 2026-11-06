"""Console exporter configuration with the source's JSON field spellings."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, TypeAlias

from pykardia.core.logger import Level

Colors: TypeAlias = bool | Annotated[int, "u8"]
Size: TypeAlias = Annotated[int, "u64"]


class Align(Enum):
    """Position the logger label before or after the level marker."""

    Left = "left"
    Right = "right"


@dataclass
class LabelStyle:
    """Minimum label width, optional margin and alignment."""

    width: Size = 0
    margin: Size | None = None
    align: Align = Align.Left


@dataclass
class ConsoleConfig:
    """Defaults and camelCase serialization aliases for the console plugin."""

    colors: Colors | None = None
    max_length: Size = field(default=10240, metadata={"alias": "maxLength"})
    levels: dict[str, Level] = field(default_factory=dict)
    show_diff: bool = field(default=False, metadata={"alias": "showDiff"})
    show_time: str = field(default="yyyy-MM-dd hh:mm:ss ", metadata={"alias": "showTime"})
    label: LabelStyle = field(default_factory=LabelStyle)

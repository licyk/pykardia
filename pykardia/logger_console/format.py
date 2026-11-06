"""Stable color hashing, ANSI rendering and local time formatting."""

import math
from datetime import datetime

C16 = (6, 2, 3, 4, 5, 1)
C256 = (
    20,
    21,
    26,
    27,
    32,
    33,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    56,
    57,
    62,
    63,
    68,
    69,
    74,
    75,
    76,
    77,
    78,
    79,
    80,
    81,
    92,
    93,
    98,
    99,
    112,
    113,
    129,
    134,
    135,
    148,
    149,
    160,
    161,
    162,
    163,
    164,
    165,
    166,
    167,
    168,
    169,
    170,
    171,
    172,
    173,
    178,
    179,
    184,
    185,
    196,
    197,
    198,
    199,
    200,
    201,
    202,
    203,
    204,
    205,
    206,
    207,
    208,
    209,
    214,
    215,
    220,
    221,
)


def name_color(name: str, colors: int) -> int:
    """Hash UTF-16 code units with signed 32-bit wrapping into the source palette."""
    hashed = 0
    encoded = name.encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset : offset + 2], "little")
        hashed = (hashed * 7 + unit + 13) & 0xFFFFFFFF
    if hashed >= 0x80000000:
        hashed -= 0x100000000
    if colors == 0:
        return 0
    palette = C16 if colors == 1 else C256
    return palette[abs(hashed) % len(palette)]


def color(colors: int, code: int, value: str, decoration: str) -> str:
    """Wrap text in the reference ANSI sequence, omitting decorations at level one."""
    if colors == 0:
        return value
    sequence = str(code) if code < 8 else f"8;5;{code}"
    return f"\x1b[3{sequence}{decoration if colors >= 2 else ''}m{value}\x1b[0m"


def format_duration(ms: int) -> str:
    """Format milliseconds using Rust's half-away-from-zero rounding and unit thresholds."""
    absolute = abs(ms)
    for threshold, unit, suffix in (
        (84_600_000, 86_400_000, "d"),
        (3_570_000, 3_600_000, "h"),
        (59_500, 60_000, "m"),
        (1000, 1000, "s"),
    ):
        if absolute >= threshold:
            quotient = float(ms) / float(unit)
            rounded = math.floor(abs(quotient) + 0.5)
            return f"{-rounded if quotient < 0 else rounded}{suffix}"
    return f"{ms}ms"


def time_template(template: str) -> str:
    """Replace the source tokens in order with one snapshot of the local wall clock."""
    now = datetime.now()
    for token, value in (
        ("yyyy", f"{now.year:04d}"),
        ("yy", f"{now.year % 100:02d}"),
        ("MM", f"{now.month:02d}"),
        ("dd", f"{now.day:02d}"),
        ("hh", f"{now.hour:02d}"),
        ("mm", f"{now.minute:02d}"),
        ("ss", f"{now.second:02d}"),
        ("SSS", f"{now.microsecond // 1000:03d}"),
    ):
        template = template.replace(token, value)
    return template

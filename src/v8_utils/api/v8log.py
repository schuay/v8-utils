"""Analysing a V8 log (d8 --prof --log-ic --log-maps): deopts, inline caches,
map transitions, tick profiles, and VM states."""

from ..v8log import (
    V8Log,
    analyze_deopts,
    analyze_fn,
    analyze_ics,
    analyze_maps,
    analyze_profile,
    analyze_vms,
    format_deopts,
    format_fn,
    format_ics,
    format_maps,
    format_profile,
    format_vms,
)

__all__ = [
    "V8Log",
    "analyze_deopts",
    "analyze_fn",
    "analyze_ics",
    "analyze_maps",
    "analyze_profile",
    "analyze_vms",
    "format_deopts",
    "format_fn",
    "format_ics",
    "format_maps",
    "format_profile",
    "format_vms",
]

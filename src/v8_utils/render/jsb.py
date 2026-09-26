"""Text for a JetStream comparison: one row per metric, one column group per
variant, with the change against the base variant."""

from rich import box
from rich.console import Console
from rich.table import Table

from ..api import jsb


def _fmt_stat(s: jsb.Stats) -> str:
    if len(s.values) == 1:
        return f"{s.values[0]:.2f}"
    return f"{s.mean:.2f} ±{s.stdev_pct:.1f}%"


def _fmt_pct(d: jsb.Delta) -> str:
    if d.pct is None:
        return "N/A"
    return f"{'+' if d.pct > 0 else ''}{d.pct:.1f}%"


def format_comparison(
    comparison: jsb.Comparison, show_all: bool = False, ansi: bool = False
) -> str:
    """The comparison as a table. With more than one variant, a metric whose
    change is not significant against any experiment variant is left out
    unless show_all."""
    labels = comparison.labels
    has_comparison = len(labels) >= 2

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("metric")
    table.add_column(labels[0], justify="right")
    for label in labels[1:]:
        table.add_column(label, justify="right")
        table.add_column("chg%", justify="right")
        table.add_column("p", justify="right")
        table.add_column("confidence", justify="right")

    omitted = 0
    for m in comparison.metrics:
        base = m.stats[0]
        cells: list[str] = [m.metric, _fmt_stat(base) if base else "N/A"]
        for exp, d in zip(m.stats[1:], m.deltas):
            cells.append(_fmt_stat(exp) if exp else "N/A")
            if d is None:
                cells.extend(["", "", ""])
                continue
            pct = _fmt_pct(d)
            # JetStream: bigger is always better
            if pct.startswith("+"):
                style = "green"
            elif pct.startswith("-"):
                style = "red"
            else:
                style = ""
            cells.append(f"[{style}]{pct}[/]" if style else pct)
            cells.append(f"{d.p_value:.4f}" if d.p_value is not None else "")
            cells.append(d.confidence)
        if not has_comparison or show_all or any(d and d.significant for d in m.deltas):
            table.add_row(*cells)
        else:
            omitted += 1

    console = Console(
        no_color=not ansi, highlight=False, width=200, force_terminal=ansi
    )
    with console.capture() as capture:
        console.print(table, end="")
    table_text = capture.get()

    n = comparison.runs
    title = ", ".join(comparison.lineitems) if comparison.lineitems else "full suite"
    lines = [f"{title}  ({comparison.suite}, {n} run{'s' if n > 1 else ''})"]
    lines.append(table_text)
    if omitted:
        dim, reset = ("\033[2m", "\033[0m") if ansi else ("", "")
        lines.append(
            f"{dim}({omitted} non-significant result"
            f"{'s' if omitted != 1 else ''} omitted"
            f" — pass --show-all for all results){reset}"
        )
    return "\n".join(lines)

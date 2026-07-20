"""Small concurrency helpers shared by CLI and MCP code.

Kept dependency-free (stdlib only) and separate from tools.py so that light
tool groups -- e.g. the gerrit MCP group, which only needs an HTTP client -- can
reuse it without importing tools.py and, through it, the pinpoint/scipy stack.
"""

import concurrent.futures
from collections.abc import Callable


def _run_concurrent(
    fns: list[Callable[[], object]],
    on_progress: Callable[[int, int], None] | None = None,
) -> list:
    """Run callables concurrently, returning results in input order.

    on_progress(done, total) is called after each completion.
    Ctrl-C cancels pending futures and re-raises KeyboardInterrupt.
    """
    if len(fns) <= 1:
        return [fn() for fn in fns]
    with concurrent.futures.ThreadPoolExecutor() as ex:
        future_to_idx = {ex.submit(fn): i for i, fn in enumerate(fns)}
        results = [None] * len(fns)
        try:
            done = 0
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()
                done += 1
                if on_progress:
                    on_progress(done, len(fns))
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    return results

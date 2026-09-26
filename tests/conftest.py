import pytest

from v8_utils import trust


@pytest.fixture(autouse=True)
def _trust_off():
    """Trusted author domains are process-wide; no test inherits another's."""
    trust._domains = None
    yield
    trust._domains = None

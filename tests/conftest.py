import pytest

from v8_utils import trust


@pytest.fixture(autouse=True)
def _trust_off():
    """No test inherits trusted author domains from another's context."""
    trust.reset()
    yield
    trust.reset()

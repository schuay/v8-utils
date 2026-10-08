import pytest

from v8_utils import identity, trust


@pytest.fixture(autouse=True)
def _trust_off():
    """No test inherits trusted author domains from another's context."""
    trust.reset()
    yield
    trust.reset()


@pytest.fixture(autouse=True)
def _identity_default():
    """No test inherits another's identity configuration or its last failure."""
    identity.reset()
    yield
    identity.reset()

from src.cloudmover.providers import proton
def test_verification_timeout_policy():
    assert proton.VERIFY_LIST_TIMEOUT == 600.0

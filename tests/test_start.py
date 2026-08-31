
from src.cloudmover.start import choose_batch, fmt_duration, VERSION

def test_version():
    assert VERSION == "0.19.3"

def test_batch_reduces_near_reserve():
    reserve=20_000_000_000
    assert choose_batch(reserve+500_000_000,reserve,25)==5
    assert choose_batch(reserve+2_000_000_000,reserve,25)==10
    assert choose_batch(reserve+10_000_000_000,reserve,25)==25

def test_duration():
    assert fmt_duration(65)=="0:01:05"

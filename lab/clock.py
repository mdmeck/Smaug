"""Wall clock for runner.py and broker.py, behind one seam so the replay
test (lab/replay_test.py) can drive a whole session in simulated time."""
import time as _time

import pandas as pd

NY = "America/New_York"


def now():
    return pd.Timestamp.now(tz=NY)


def time():
    return _time.time()


def sleep(seconds):
    _time.sleep(seconds)

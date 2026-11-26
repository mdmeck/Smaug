"""Strategy modules. Each one is a file here exposing:

    NAME: str
    DESCRIPTION: str
    DEFAULTS: dict                      # every tunable, with its default
    def signals(day, ctx, params) -> list[Signal]

`day` is the session's RTH 1-min bars *up to now* — the whole day in a
backtest, a growing prefix live. `ctx` holds prior-session context (see
backtest.session_context). The same function drives backtest.py and
runner.py, so there is no second implementation to drift out of sync.

The contract that makes that safe: a signal at bar `ts` may only depend on
bars with index <= ts and on ctx. `python -m lab.research causal <name>`
checks it by re-running on every prefix of sampled days and comparing.
"""
import importlib
import pkgutil
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Signal:
    ts: object                      # bar whose close triggers the entry
    direction: str                  # "long" (buy call) | "short" (buy put)
    dte: int = 0                    # sessions to expiry; 0 = same-day
    otm: int = 0                    # strikes out of the money at entry
    stop_pct: Optional[float] = None    # premium stop, fraction of fill
    target_pct: Optional[float] = None  # premium target, fraction of fill
    u_stop: Optional[float] = None      # underlying stop level
    u_target: Optional[float] = None    # underlying target level
    exit_by: str = "15:45"          # hard time exit (NY)
    max_hold_min: Optional[int] = None
    # scale-out and runner (lab/exits.py); all off by default
    scale_frac: Optional[float] = None  # fraction of contracts sold at the first target
    trail_pct: Optional[float] = None   # runner: exit this far below its premium high
    trail_ema: Optional[list] = None    # runner: [fast, slow, minutes] EMA cross exit
    runner_be: bool = True              # runner's stop moves to entry after the scale
    tag: str = ""
    meta: dict = field(default_factory=dict)

    def to_json(self):
        d = asdict(self)
        d["ts"] = str(self.ts)
        return d


def load(name):
    return importlib.import_module(f"{__name__}.{name}")


def available():
    import lab.strategies as pkg
    return sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_"))

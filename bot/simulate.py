"""`--simulate` demo switches. None of these exist on Demoblaze itself."""
from __future__ import annotations

from dataclasses import dataclass, field

HELP = (
    "price_drift=<prod_id>:<pct>  raise the observed price of a product by pct%\n"
    "stock_out=<prod_id>          treat a product as out of stock\n"
    "checkout_timeout=<batch_no>  pretend the confirmation never appeared after Purchase\n"
    "interrupt=<batch_no>         Ctrl+C-style stop halfway through building that batch"
)


class SimulationError(ValueError):
    pass


@dataclass
class Simulation:
    price_drift: dict[int, float] = field(default_factory=dict)
    stock_out: set[int] = field(default_factory=set)
    checkout_timeout: set[int] = field(default_factory=set)
    interrupt: set[int] = field(default_factory=set)

    def observed_price(self, prod_id: int, live_price: float) -> float:
        pct = self.price_drift.get(prod_id)
        return round(live_price * (1 + pct / 100.0), 2) if pct else live_price

    def describe(self) -> list[str]:
        out = [f"price_drift {p}:+{v:g}%" for p, v in self.price_drift.items()]
        out += [f"stock_out {p}" for p in sorted(self.stock_out)]
        out += [f"checkout_timeout batch {b}" for b in sorted(self.checkout_timeout)]
        out += [f"interrupt batch {b}" for b in sorted(self.interrupt)]
        return out


def parse_simulations(values: list[str] | None) -> Simulation:
    """Accepts repeated flags and comma lists: `stock_out=3,14 price_drift=1:20`."""
    sim = Simulation()
    key = None
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, part = (s.strip() for s in part.split("=", 1))
            if key is None:
                raise SimulationError(f"bad --simulate value {part!r}; expected key=value")
            try:
                if key == "price_drift":
                    pid, pct = part.split(":", 1)
                    sim.price_drift[int(pid)] = float(pct.rstrip("%"))
                elif key == "stock_out":
                    sim.stock_out.add(int(part))
                elif key == "checkout_timeout":
                    sim.checkout_timeout.add(int(part))
                elif key == "interrupt":
                    sim.interrupt.add(int(part))
                else:
                    raise SimulationError(f"unknown --simulate key {key!r}. Supported:\n{HELP}")
            except ValueError as exc:
                if isinstance(exc, SimulationError):
                    raise
                raise SimulationError(f"bad --simulate value {key}={part!r}") from exc
    return sim

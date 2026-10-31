"""Session spend tracking and caps."""

from __future__ import annotations

from dataclasses import dataclass, field

# Roles that keep the player's own scene alive; background roles stop first.
ESSENTIAL_ROLES = {"gm", "narrator", "agent", "chronicler"}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    price_in_per_m: float
    price_out_per_m: float
    warn_usd: float
    hard_usd: float
    spent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    warned: bool = False
    by_role: dict[str, float] = field(default_factory=dict)

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in * self.price_in_per_m / 1e6 + tokens_out * self.price_out_per_m / 1e6

    def charge(self, role: str, tokens_in: int, tokens_out: int) -> float:
        c = self.cost(tokens_in, tokens_out)
        self.spent += c
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.by_role[role] = self.by_role.get(role, 0.0) + c
        return c

    def pressure(self) -> float:
        """0 below the warn line, rising to 1 at the hard cap."""
        if self.spent <= self.warn_usd:
            return 0.0
        span = max(1e-9, self.hard_usd - self.warn_usd)
        return min(1.0, (self.spent - self.warn_usd) / span)

    def check(self, role: str) -> None:
        if self.spent >= self.hard_usd:
            raise BudgetExceeded(
                f"session budget ${self.hard_usd:.2f} reached (spent ${self.spent:.3f}); "
                "raise budget.session_hard_usd in nemothrones.yaml to continue")
        if self.pressure() >= 0.75 and role not in ESSENTIAL_ROLES:
            raise BudgetExceeded(f"budget pressure: skipping background role {role!r}")

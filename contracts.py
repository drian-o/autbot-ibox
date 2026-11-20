"""Small, merchant-independent contract shared by the worker and adapters."""
from dataclasses import dataclass


class AdapterError(Exception):
    """A check cannot be completed reliably."""


class NeedsAction(AdapterError):
    """The operator must act in the browser before another attempt."""


@dataclass(frozen=True)
class StockSnapshot:
    available: bool
    unit_price: int | None
    name: str = ""
    variant: str = ""
    note: str = ""


@dataclass(frozen=True)
class CheckoutResult:
    status: str
    order_ref: str = ""
    payment_url: str = ""
    total: int | None = None
    note: str = ""

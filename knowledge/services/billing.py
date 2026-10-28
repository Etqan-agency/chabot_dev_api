"""Points: every OpenAI call is charged to the tenant whose work it is.

One point is $0.001 of real provider cost (``LLM_PRICING`` /
``EMBEDDING_PRICING``). Wallets hold milli-points, so a $0.00004 embedding is
charged exactly instead of being rounded up to a whole point.

The LLM and embedding backends call ``meter()`` after every provider call. Whom
it charges is decided by the innermost ``charge_to()`` scope -- a context
variable, so concurrent requests never mix. Work done on another thread joins
the scope through ``bind()``. A call outside any scope (a management command)
is not charged.

Two ways to charge:

* **As it goes** (documents, crawl, API import, knowledge edits, translation):
  each call is taken from the wallet when it is made, so a long run stops
  costing once the points are gone (it checks ``scope_has_points()``).
* **Reserved** (chat): points are held before the answer and settled to the
  real cost after it -- or fully released when the answer failed.

Either way the scope writes ONE ``UsageRecord`` for the operation on exit.

Only the thread that opened a scope touches the database. Calls metered on
worker threads (the batched tree translation, the API analysis) are kept in
memory and taken from the wallet by the owning thread at its next call or on
exit; meanwhile the workers judge "any points left?" from the balance the owner
last read minus what they have spent since.
"""

from __future__ import annotations

import contextvars
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MILLI_PER_POINT = 1000


def usd_to_milli(cost_usd: float) -> int:
    """Provider cost -> milli-points (1 milli-point = $0.000001), rounded up."""
    if not cost_usd or cost_usd <= 0:
        return 0
    return math.ceil(round(cost_usd * 1_000_000, 6))


def milli_to_points(milli: int) -> float:
    return round((milli or 0) / MILLI_PER_POINT, 3)


@dataclass
class ChargeScope:
    user: object
    kind: str
    # Milli-points held up front (chat). None = charge each call as it happens.
    reserved: int | None = None
    exempt: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    milli: int = 0
    model: str = ""
    # The operation failed: release the reservation and record nothing.
    void: bool = False
    # Extra UsageRecord fields (response_time_ms, confident, chunk_count).
    record: dict = field(default_factory=dict)
    usage_record: object = None
    # Metered but not yet taken from the wallet (calls made on worker threads).
    pending: int = 0
    # The wallet balance the owning thread last read, for the workers' checks.
    known_balance: int = 0
    owner_thread: int = field(default_factory=threading.get_ident)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def points(self) -> float:
        return milli_to_points(self.milli)

    def on_owner_thread(self) -> bool:
        return threading.get_ident() == self.owner_thread


_current: contextvars.ContextVar[ChargeScope | None] = contextvars.ContextVar("points_scope", default=None)


def current_scope() -> ChargeScope | None:
    return _current.get()


def _wallet():
    from subscriptions import services

    return services


@contextmanager
def charge_to(user, kind: str, *, reserved: int | None = None):
    """Charge the OpenAI calls made inside the block to ``user``.

    With ``reserved`` (milli-points already held by ``reserve_chat_points``),
    nothing is taken per call: on exit the hold is settled to the real cost,
    or released in full when the block raised or set ``scope.void``.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        yield None
        return
    services = _wallet()
    scope = ChargeScope(user=user, kind=kind, reserved=reserved, exempt=services.is_points_exempt(user))
    if not scope.exempt and reserved is None:
        scope.known_balance = services.points_milli(user)
    token = _current.set(scope)
    try:
        yield scope
    except BaseException:
        if reserved is not None:
            scope.void = True  # never charge for an answer the user didn't get
        raise
    finally:
        _current.reset(token)
        _close(scope)


def bind(fn):
    """Wrap ``fn`` so it runs inside the caller's scope on a worker thread."""
    scope = _current.get()

    def run(*args, **kwargs):
        token = _current.set(scope)
        try:
            return fn(*args, **kwargs)
        finally:
            _current.reset(token)

    return run


def meter(model: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Record one provider call against the current scope (no scope = free)."""
    scope = _current.get()
    if scope is None:
        logger.debug("OpenAI call outside a points scope (%s) -- not charged", model)
        return
    from .quota import estimate_cost

    cost = estimate_cost(model, tokens_in or 0, tokens_out or 0)
    milli = usd_to_milli(cost)
    with scope._lock:
        scope.tokens_in += max(0, tokens_in or 0)
        scope.tokens_out += max(0, tokens_out or 0)
        scope.cost_usd += cost
        scope.milli += milli
        # Name the operation after its LLM (the embedding model only when no LLM ran).
        if tokens_out or not scope.model:
            scope.model = model or scope.model
        if scope.reserved is None and not scope.exempt:
            scope.pending += milli
    if scope.on_owner_thread():
        _flush(scope)


def _flush(scope: ChargeScope) -> None:
    """Take what was metered so far from the wallet (owning thread only)."""
    with scope._lock:
        pending, scope.pending = scope.pending, 0
        scope.known_balance = max(0, scope.known_balance - pending)
    if pending:
        _wallet().charge_points(scope.user, pending)


def scope_has_points() -> bool:
    """May the current scope keep calling OpenAI? (Always True outside a scope.)"""
    scope = _current.get()
    if scope is None or scope.exempt or scope.reserved is not None:
        return True
    if scope.on_owner_thread():
        _flush(scope)
        scope.known_balance = _wallet().points_milli(scope.user)
        return scope.known_balance >= MILLI_PER_POINT
    with scope._lock:  # a worker: no database, judge from the last known balance
        return scope.known_balance - scope.pending >= MILLI_PER_POINT


def _close(scope: ChargeScope) -> None:
    _flush(scope)
    if scope.reserved is not None and not scope.exempt:
        _wallet().settle_points(scope.user, scope.reserved, 0 if scope.void else scope.milli)
    if scope.void or not (scope.tokens_in or scope.tokens_out or scope.milli):
        return
    from ..models import UsageRecord

    try:
        scope.usage_record = UsageRecord.objects.create(
            user=scope.user,
            kind=scope.kind,
            model=scope.model,
            tokens_in=scope.tokens_in,
            tokens_out=scope.tokens_out,
            cost_usd=scope.cost_usd,
            points_milli=0 if scope.exempt else scope.milli,
            **scope.record,
        )
    except Exception:
        logger.exception("Could not record usage for %s (%s)", getattr(scope.user, "pk", None), scope.kind)

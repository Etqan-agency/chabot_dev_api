"""Per-user feature access — one place that answers "can this tenant use X?".

Every gated capability is a **feature key** in ``FEATURES``. Access resolves in
this precedence:

1. Staff / superuser → always allowed (internal accounts are never gated).
2. An explicit per-user **override** (set by an admin from the dashboard) →
   that value wins.
3. Otherwise the feature's **default**, derived from the tenant's plan / tier so
   behaviour matches what it was before overrides existed.

Views call ``has_feature(user, key)``; the admin dashboard reads
``resolved_features(user)`` and writes ``set_overrides(user, {...})``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    default: Callable[[object], bool]


def _active_plan(user):
    from .services import active_plan

    return active_plan(user)


def _api_sync_default(user) -> bool:
    plan = _active_plan(user)
    if plan is not None:
        return bool(plan.allow_api_sync)
    return bool(getattr(settings, "FREE_TIER_ALLOW_API_SYNC", False))


def _api_key_default(user) -> bool:
    # API-key access is a paid feature: on for any active plan, off for free tier.
    return _active_plan(user) is not None


# The registry. Add a feature here + gate its view with has_feature(user, key).
FEATURES: dict[str, Feature] = {
    "api_sync": Feature(
        "api_sync",
        "External API sync",
        "Import knowledge from an external API (POST /sync-api-content/).",
        _api_sync_default,
    ),
    "website_crawl": Feature(
        "website_crawl",
        "Website crawl",
        "Crawl a whole website into knowledge (POST /crawl-website/).",
        _api_sync_default,
    ),
    "api_key": Feature(
        "api_key",
        "API key access",
        "View and use the tenant API key for external integrations.",
        _api_key_default,
    ),
    "guided_tree": Feature(
        "guided_tree",
        "Guided question tree",
        "Author the multilingual guided question tree.",
        lambda user: True,
    ),
    "whatsapp": Feature(
        "whatsapp",
        "WhatsApp integration",
        "Send WhatsApp messages via the tenant's connected number.",
        lambda user: True,
    ),
}


def is_exempt(user) -> bool:
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _overrides(user) -> dict:
    try:
        return user.feature_overrides.overrides or {}
    except Exception:
        return {}


def has_feature(user, key: str) -> bool:
    """Can ``user`` use the feature ``key``? (staff → always; override → wins; else default)."""
    if is_exempt(user):
        return True
    feature = FEATURES.get(key)
    if feature is None:
        return False
    override = _overrides(user).get(key)
    if override is not None:
        return bool(override)
    try:
        return bool(feature.default(user))
    except Exception:
        return False


def catalog() -> list[dict]:
    """The list of feature keys + labels (for rendering dashboard toggles)."""
    return [{"key": f.key, "label": f.label, "description": f.description} for f in FEATURES.values()]


def resolved_features(user) -> dict:
    """Every feature's effective value for ``user``, with WHY (source)."""
    exempt = is_exempt(user)
    overrides = _overrides(user)
    out = {}
    for key, feature in FEATURES.items():
        if exempt:
            enabled, source = True, "staff"
        elif key in overrides and overrides[key] is not None:
            enabled, source = bool(overrides[key]), "override"
        else:
            try:
                enabled = bool(feature.default(user))
            except Exception:
                enabled = False
            source = "default"
        out[key] = {"enabled": enabled, "source": source, "label": feature.label}
    return out


def set_overrides(user, mapping: dict) -> dict:
    """Apply admin overrides. ``{key: bool}`` sets; ``{key: None}`` clears (inherit).

    Unknown keys are ignored. Returns the stored overrides dict.
    """
    from .models import UserFeatureOverride

    row, _ = UserFeatureOverride.objects.get_or_create(user=user)
    current = dict(row.overrides or {})
    for key, value in (mapping or {}).items():
        if key not in FEATURES:
            continue
        if value is None:
            current.pop(key, None)  # clear → inherit default
        else:
            current[key] = bool(value)
    row.overrides = current
    row.save(update_fields=["overrides", "updated_at"])
    # Refresh the reverse-relation cache on this instance so a later has_feature()
    # on the SAME user object reflects the change (fresh objects re-query anyway).
    try:
        user.feature_overrides = row
    except Exception:
        pass
    return current

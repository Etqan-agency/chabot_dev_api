"""Track the points granted per period (grant + admin top-ups).

The dashboard showed "used / total" against the plan's points only, so a
top-up never entered the total ("658 / 300"). Existing wallets get what they
hold now plus what this period already spent -- the real total -- and never
less than the plan's (or the free tier's) grant.
"""

from django.conf import settings
from django.db import migrations, models
from django.db.models import Sum
from django.utils import timezone

MILLI_PER_POINT = 1000


def backfill_granted(apps, schema_editor):
    CreditWallet = apps.get_model("subscriptions", "CreditWallet")
    Subscription = apps.get_model("subscriptions", "Subscription")
    UsageRecord = apps.get_model("knowledge", "UsageRecord")
    free_milli = int(getattr(settings, "FREE_TIER_POINTS", 0)) * MILLI_PER_POINT
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for wallet in CreditWallet.objects.all().iterator():
        sub = Subscription.objects.filter(user_id=wallet.user_id).select_related("plan").first()
        if sub is not None:
            base = sub.plan.points * MILLI_PER_POINT
            since = sub.current_period_start
        else:
            base = free_milli
            since = max(wallet.last_free_grant_at, month_start)
        spent = (
            UsageRecord.objects.filter(user_id=wallet.user_id, created_at__gte=since).aggregate(
                s=Sum("points_milli")
            )["s"]
            or 0
        )
        wallet.granted_milli = max(base, wallet.milli_points + spent)
        wallet.save(update_fields=["granted_milli"])


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0007_points"),
        ("knowledge", "0011_usage_points"),
    ]

    operations = [
        migrations.AddField(
            model_name="creditwallet",
            name="granted_milli",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(backfill_granted, migrations.RunPython.noop),
    ]

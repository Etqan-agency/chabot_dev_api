"""Credits -> points.

A plan sold N questions as 2N credits; it now grants points, where one point is
$0.001 of real OpenAI cost and a gpt-4o RAG answer costs about 12. Converting at
that rate keeps every plan and wallet worth the same number of questions:

* ``Plan.included_credits`` -> ``Plan.points``: points = credits / 2 * 12.
* ``CreditWallet.balance`` (credits) -> ``milli_points``: the same rate, x1000.
  A tenant without a subscription is capped at the new free grant.
"""

from django.conf import settings
from django.db import migrations, models

POINTS_PER_CREDIT = 6  # (1 question / 2 credits) * 12 points per question


def credits_to_points(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "Plan")
    CreditWallet = apps.get_model("subscriptions", "CreditWallet")
    Subscription = apps.get_model("subscriptions", "Subscription")

    for plan in Plan.objects.all():
        plan.points = plan.points * POINTS_PER_CREDIT
        plan.save(update_fields=["points"])

    subscribed = set(Subscription.objects.values_list("user_id", flat=True))
    free_cap = int(getattr(settings, "FREE_TIER_POINTS", 300)) * 1000
    for wallet in CreditWallet.objects.all():
        milli = wallet.milli_points * POINTS_PER_CREDIT * 1000
        if wallet.user_id not in subscribed:
            milli = min(milli, free_cap)
        wallet.milli_points = milli
        wallet.save(update_fields=["milli_points"])


def points_to_credits(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "Plan")
    CreditWallet = apps.get_model("subscriptions", "CreditWallet")
    for plan in Plan.objects.all():
        plan.points = plan.points // POINTS_PER_CREDIT
        plan.save(update_fields=["points"])
    for wallet in CreditWallet.objects.all():
        wallet.milli_points = wallet.milli_points // (POINTS_PER_CREDIT * 1000)
        wallet.save(update_fields=["milli_points"])


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0006_userfeatureoverride"),
    ]

    operations = [
        migrations.RenameField("plan", "included_credits", "points"),
        migrations.RenameField("creditwallet", "balance", "milli_points"),
        migrations.AlterField(
            model_name="creditwallet",
            name="milli_points",
            field=models.BigIntegerField(default=0),
        ),
        migrations.RunPython(credits_to_points, points_to_credits),
    ]

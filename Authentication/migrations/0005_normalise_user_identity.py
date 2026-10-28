"""Normalise stored emails/usernames, and refuse to proceed on real collisions.

0005 adds case-insensitive unique indexes on email and username. Any existing
database that already holds two accounts differing only by case or surrounding
whitespace would fail that migration with a bare IntegrityError naming an index,
which tells an operator nothing about which rows to fix.

So this runs first and does two things:

1. Trims and lowercases addresses that merely *look* different. A row whose
   normalised form is unique is repaired silently -- nothing is lost, and it is
   the value every lookup already matched case-insensitively.
2. Stops with an explicit list when normalising is not enough: two genuinely
   distinct accounts claiming one identity. That is a product decision (merge,
   rename, or retire one), not something a migration may guess at.

Deliberately irreversible in the destructive direction only: reverse is a no-op,
because un-normalising would mean inventing the original casing.
"""

from collections import defaultdict

from django.db import migrations


def _collisions(model, field):
    """{normalised value: [pks]} for every normalised value used more than once."""
    groups = defaultdict(list)
    for pk, value in model.objects.values_list("pk", field):
        if not value:
            continue
        groups[str(value).strip().lower()].append(pk)
    return {value: pks for value, pks in groups.items() if len(pks) > 1}


def normalise(apps, schema_editor):
    User = apps.get_model("Authentication", "User")

    blocking = []
    for field, label in (("email", "email address"), ("username", "username")):
        for value, pks in _collisions(User, field).items():
            blocking.append(f"  {label} {value!r} is used by {len(pks)} accounts: {', '.join(str(p) for p in pks)}")

    if blocking:
        raise RuntimeError(
            "Cannot enforce one-account-per-identity: this database already "
            "contains accounts that collide once case and whitespace are "
            "ignored.\n\n" + "\n".join(blocking) + "\n\n"
            "Decide what each collision should become -- merge the accounts, "
            "rename one, or deactivate the one that is not in use -- then run "
            "the migration again. Nothing has been changed."
        )

    # No collisions: safe to canonicalise in place.
    touched = 0
    for user in User.objects.all().iterator():
        email = (user.email or "").strip()
        username = (user.username or "").strip()
        new_email = email.lower() if email else email
        if new_email != (user.email or "") or username != (user.username or ""):
            user.email = new_email
            user.username = username
            user.save(update_fields=["email", "username"])
            touched += 1
    if touched:
        print(f"  normalised {touched} account(s)")


def noop(apps, schema_editor):
    """Reverse is a no-op: the original casing is not recoverable."""


class Migration(migrations.Migration):
    dependencies = [
        ("Authentication", "0004_passwordchangecode"),
    ]

    operations = [
        migrations.RunPython(normalise, noop),
    ]

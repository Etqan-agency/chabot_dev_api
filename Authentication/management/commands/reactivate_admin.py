"""Recover a locked-out admin: reactivate + re-grant staff/superuser.

Usage:
    python manage.py reactivate_admin                 # reactivate ALL superusers
    python manage.py reactivate_admin --username bob   # a specific account
    python manage.py reactivate_admin --list           # just show admin states

Safe to run anytime; it only ever ENABLES access (sets is_active/is_staff/
is_superuser True). Handy when someone deactivates their own admin account.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Reactivate a locked-out admin/superuser account."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Reactivate this username specifically.")
        parser.add_argument("--list", action="store_true", help="Only list admin accounts and their state.")

    def handle(self, *args, **options):
        User = get_user_model()

        if options["list"]:
            admins = User.objects.filter(is_staff=True) | User.objects.filter(is_superuser=True)
            for u in admins.distinct():
                self.stdout.write(f"{u.username}  active={u.is_active}  staff={u.is_staff}  super={u.is_superuser}")
            return

        if options["username"]:
            qs = User.objects.filter(username=options["username"])
            if not qs.exists():
                self.stderr.write(self.style.ERROR(f"No user named {options['username']!r}."))
                return
        else:
            # Default: every superuser (the accounts that must never stay locked).
            qs = User.objects.filter(is_superuser=True)
            if not qs.exists():
                self.stderr.write(self.style.ERROR("No superusers found."))
                return

        for u in qs:
            u.is_active = True
            u.is_staff = True
            u.is_superuser = True
            u.save(update_fields=["is_active", "is_staff", "is_superuser"])
            self.stdout.write(self.style.SUCCESS(f"Reactivated {u.username}."))

"""Set a tenant up for a voice-ordering demo.

Usage:
  python manage.py voice_demo <username>                  # agent + sample menu + new token
  python manage.py voice_demo <username> --agent-id <id>  # also link the ElevenLabs agent
  python manage.py voice_demo <username> --no-menu        # keep the menu as it is

Creates the tenant if it doesn't exist. The sample menu is only added when the
tenant has no menu items yet. Prints a new tool token (the old one stops working).
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.crypto import get_random_string

from voice.models import MenuItem, VoiceAgent

SAMPLE_MENU = [
    ("شاورما فراخ", "سندوتشات", "85"),
    ("شاورما لحمة", "سندوتشات", "110"),
    ("فرخة مشوية", "وجبات", "240"),
    ("بطاطس", "إضافات", "35"),
    ("بيبسي", "مشروبات", "20"),
]


class Command(BaseCommand):
    help = "Create a voice agent, a sample menu and a tool token for a tenant."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--agent-id", default="", help="ElevenLabs agent id to link.")
        parser.add_argument("--delivery-fee", default="20", help="Delivery fee (default 20).")
        parser.add_argument("--no-menu", action="store_true", help="Don't add the sample menu.")

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            password = get_random_string(16)
            user = User.objects.create_user(username=options["username"], password=password)
            self.stdout.write(f"Created tenant {user.username} (password: {password})")

        agent, _ = VoiceAgent.objects.get_or_create(tenant=user)
        agent.delivery_fee = Decimal(options["delivery_fee"])
        if options["agent_id"]:
            agent.agent_id = options["agent_id"]
        agent.save()

        if not options["no_menu"] and not MenuItem.objects.filter(tenant=user).exists():
            for order, (name, category, price) in enumerate(SAMPLE_MENU):
                MenuItem.objects.create(
                    tenant=user, name=name, category=category, price=Decimal(price), sort_order=order
                )
            self.stdout.write(f"Added {len(SAMPLE_MENU)} sample menu items.")

        token = agent.issue_token()
        self.stdout.write(self.style.SUCCESS(f"Tool token (shown once): {token}"))

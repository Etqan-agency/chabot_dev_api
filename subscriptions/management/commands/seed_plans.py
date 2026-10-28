"""Create/update the recommended subscription plans (idempotent).

Run:  python manage.py seed_plans

Token caps are a fair-use safety net (~5,000 tokens per question of headroom;
real average is ~2,800). Prices are suggested selling prices — edit freely in
the admin. All plans use gpt-4o by default.
"""

from django.core.management.base import BaseCommand

from subscriptions.models import Plan

# (slug, defaults). monthly_token_cap / monthly_questions: 0 = unlimited.
PLANS = [
    {
        "slug": "trial",
        "name": "Trial",
        "price_usd": 0,
        "sort_order": 0,
        "monthly_questions": 100,
        "monthly_token_cap": 500_000,
        "max_documents": 5,
        "max_total_mb": 10,
        "max_requests_per_min": 20,
        "llm_model": "gpt-4o",
        "description": "Free trial — 100 questions/month.",
    },
    {
        "slug": "starter",
        "name": "Starter",
        "price_usd": 39,
        "sort_order": 1,
        "monthly_questions": 1_000,
        "monthly_token_cap": 5_000_000,
        "max_documents": 25,
        "max_total_mb": 50,
        "max_requests_per_min": 60,
        "llm_model": "gpt-4o",
        "description": "1,000 questions/month for small sites.",
    },
    {
        "slug": "growth",
        "name": "Growth",
        "price_usd": 149,
        "sort_order": 2,
        "monthly_questions": 5_000,
        "monthly_token_cap": 25_000_000,
        "max_documents": 100,
        "max_total_mb": 200,
        "max_requests_per_min": 120,
        "llm_model": "gpt-4o",
        "description": "5,000 questions/month for growing businesses.",
    },
    {
        "slug": "business",
        "name": "Business",
        "price_usd": 399,
        "sort_order": 3,
        "monthly_questions": 15_000,
        "monthly_token_cap": 75_000_000,
        "max_documents": 300,
        "max_total_mb": 500,
        "max_requests_per_min": 240,
        "llm_model": "gpt-4o",
        "description": "15,000 questions/month for high-volume use.",
    },
]


class Command(BaseCommand):
    help = "Seed the recommended subscription plans (idempotent)."

    def handle(self, *args, **options):
        for spec in PLANS:
            slug = spec.pop("slug")
            name = spec["name"]
            plan, created = Plan.objects.update_or_create(slug=slug, defaults=spec)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                f"{verb}: {name} — {plan.monthly_questions or 'unlimited'} q/mo, "
                f"${plan.price_usd}, model {plan.llm_model}"
            )
        self.stdout.write(self.style.SUCCESS("\nPlans seeded. Edit them anytime in the admin."))

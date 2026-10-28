"""ChatbotConfig endpoint + the system-prompt assembler.

The prompt is system-controlled: tenants set identity fields only; the strict
grounding + insider-voice rules are fixed in code and asserted here."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import ChatbotConfig
from knowledge.services.chatbot_config import (
    ResolvedConfig,
    build_no_data_prompt,
    build_system_prompt,
    resolve_config,
)

from .factories import make_tenant


class ConfigEndpointTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("alice")
        self.url = reverse("chatbot-config")

    def test_get_autocreates_config(self):
        self.assertFalse(ChatbotConfig.objects.filter(user=self.user).exists())
        res = self.client.get(self.url, headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(ChatbotConfig.objects.filter(user=self.user).exists())

    def test_update_identity_fields(self):
        res = self.client.patch(
            self.url,
            {"assistant_name": "Sara", "company_name": "Original Software"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 200)
        cfg = ChatbotConfig.objects.get(user=self.user)
        self.assertEqual(cfg.assistant_name, "Sara")
        self.assertEqual(cfg.company_name, "Original Software")

    def test_config_is_per_tenant(self):
        other, other_key = make_tenant("bob")
        self.client.patch(self.url, {"assistant_name": "Sara"}, format="json", headers={"x-api-key": self.key})
        res = self.client.get(self.url, headers={"x-api-key": other_key})
        # Bob gets his own fresh config, not Alice's.
        self.assertNotEqual(res.data["assistant_name"], "Sara")

    def test_requires_auth(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)


class ResolveConfigTests(TestCase):
    def test_anonymous_gets_defaults(self):
        cfg = resolve_config(None)
        self.assertTrue(cfg.strict_grounding)
        self.assertEqual(cfg.tone, "friendly")

    def test_reads_saved_config(self):
        user, _ = make_tenant("alice")
        ChatbotConfig.objects.create(user=user, assistant_name="Sara", company_name="Acme", tone="formal")
        cfg = resolve_config(user)
        self.assertEqual(cfg.assistant_name, "Sara")
        self.assertEqual(cfg.company_name, "Acme")
        self.assertEqual(cfg.tone, "formal")


class SystemPromptTests(TestCase):
    def _prompt(self, **kw):
        return build_system_prompt(ResolvedConfig(**kw))

    def test_speaks_as_insider(self):
        p = self._prompt(company_name="Acme")
        self.assertIn("we", p.lower())
        self.assertIn("Acme", p)

    def test_forbids_hedging_language(self):
        p = self._prompt(company_name="Acme")
        self.assertIn("based on the available information", p.lower())  # listed as forbidden
        self.assertNotIn("Context:", p)  # the word "Context" was removed

    def test_strict_grounding_rule_present(self):
        p = self._prompt(company_name="Acme", strict_grounding=True)
        self.assertIn("What you know", p)

    def test_no_data_prompt_is_warm_and_factless(self):
        p = build_no_data_prompt("بتبيعو اي", ResolvedConfig(company_name="Acme"))
        self.assertIn("Acme", p)
        self.assertIn("بتبيعو اي", p)

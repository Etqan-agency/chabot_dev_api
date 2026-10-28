"""Per-user feature access: resolution precedence (staff > override > default),
the admin control endpoints, and enforcement at the gated views."""

from __future__ import annotations

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.tests.factories import make_tenant
from subscriptions import features
from subscriptions.models import UserFeatureOverride

from .tests import make_plan, subscribe


class FeatureResolutionTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("feat_user")  # free tier, non-staff
        self.admin, _ = make_tenant("feat_admin", is_staff=True)

    def test_staff_always_has_every_feature(self):
        for key in features.FEATURES:
            self.assertTrue(features.has_feature(self.admin, key))

    def test_free_tier_defaults(self):
        # api_sync/website_crawl/api_key OFF by default for free tier; guided/wa ON.
        self.assertFalse(features.has_feature(self.user, "api_sync"))
        self.assertFalse(features.has_feature(self.user, "website_crawl"))
        self.assertFalse(features.has_feature(self.user, "api_key"))
        self.assertTrue(features.has_feature(self.user, "guided_tree"))
        self.assertTrue(features.has_feature(self.user, "whatsapp"))

    def test_paid_plan_enables_api_key(self):
        subscribe(self.user, make_plan(slug="pro", allow_api_sync=True))
        self.assertTrue(features.has_feature(self.user, "api_key"))
        self.assertTrue(features.has_feature(self.user, "api_sync"))

    def test_override_grants_a_feature_a_free_user_lacks(self):
        features.set_overrides(self.user, {"api_sync": True})
        self.assertTrue(features.has_feature(self.user, "api_sync"))

    def test_override_revokes_a_default_on_feature(self):
        features.set_overrides(self.user, {"guided_tree": False})
        self.assertFalse(features.has_feature(self.user, "guided_tree"))

    def test_clearing_override_restores_default(self):
        features.set_overrides(self.user, {"api_sync": True})
        self.assertTrue(features.has_feature(self.user, "api_sync"))
        features.set_overrides(self.user, {"api_sync": None})  # clear → inherit
        self.assertFalse(features.has_feature(self.user, "api_sync"))
        self.assertNotIn("api_sync", UserFeatureOverride.objects.get(user=self.user).overrides)

    def test_unknown_keys_ignored(self):
        features.set_overrides(self.user, {"not_a_feature": True})
        self.assertEqual(UserFeatureOverride.objects.get(user=self.user).overrides, {})

    def test_unknown_feature_key_is_false(self):
        self.assertFalse(features.has_feature(self.user, "nope"))

    def test_resolved_reports_source(self):
        features.set_overrides(self.user, {"api_sync": True})
        res = features.resolved_features(self.user)
        self.assertEqual(res["api_sync"], {"enabled": True, "source": "override", "label": res["api_sync"]["label"]})
        self.assertEqual(res["guided_tree"]["source"], "default")


class FeatureApiTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("f_owner")
        self.admin, self.admin_key = make_tenant("f_admin", is_staff=True)

    def test_catalog_requires_auth_and_lists_features(self):
        self.assertEqual(self.client.get(reverse("feature-catalog")).status_code, 401)
        res = self.client.get(reverse("feature-catalog"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        keys = {f["key"] for f in res.data["features"]}
        self.assertIn("api_sync", keys)
        self.assertIn("guided_tree", keys)

    def test_my_features(self):
        res = self.client.get(reverse("my-features"), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        self.assertIn("api_sync", res.data["features"])

    def test_admin_reads_and_sets_user_features(self):
        url = reverse("user-features", args=[str(self.user.pk)])
        # non-admin forbidden
        self.assertEqual(self.client.get(url, headers={"x-api-key": self.key}).status_code, 403)
        # admin reads
        res = self.client.get(url, headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["features"]["api_sync"]["enabled"])
        # admin grants api_sync + website_crawl
        res = self.client.patch(
            url,
            {"overrides": {"api_sync": True, "website_crawl": True}},
            format="json",
            headers={"x-api-key": self.admin_key},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["features"]["api_sync"]["enabled"])
        self.assertTrue(features.has_feature(self.user, "website_crawl"))

    def test_admin_unknown_user_404(self):
        url = reverse("user-features", args=["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(self.client.get(url, headers={"x-api-key": self.admin_key}).status_code, 404)

    def test_patch_rejects_non_object(self):
        url = reverse("user-features", args=[str(self.user.pk)])
        res = self.client.patch(url, {"overrides": "nope"}, format="json", headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 400)


class FeatureEnforcementTests(APITestCase):
    """An admin override actually opens/closes the gated endpoints."""

    def setUp(self):
        self.user, self.key = make_tenant("gate_user")  # free tier

    def test_website_crawl_gate_follows_override(self):
        url = reverse("crawl-website")
        # default (free) → 402
        self.assertEqual(
            self.client.post(
                url, {"url": "https://ex.com"}, format="json", headers={"x-api-key": self.key}
            ).status_code,
            402,
        )
        # admin grants it → no longer 402 (400/202 depending on URL, but not 402)
        features.set_overrides(self.user, {"website_crawl": True})
        res = self.client.post(url, {"url": "https://ex.com"}, format="json", headers={"x-api-key": self.key})
        self.assertNotEqual(res.status_code, 402)

    def test_api_key_gate_follows_override(self):
        url = reverse("user-api-key")
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(url).status_code, 402)  # free tier
        features.set_overrides(self.user, {"api_key": True})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_guided_tree_gate_follows_override(self):
        from knowledge.models import AvailableLanguage

        AvailableLanguage.objects.get_or_create(code="ar", name="Arabic")
        url = reverse("guided-tree-list")
        # default ON → create works
        with override_settings(GUIDED_TREE_TRANSLATE_MODE="sync"):
            ok = self.client.post(url, {"title": "x"}, format="json", headers={"x-api-key": self.key})
        self.assertEqual(ok.status_code, 201)
        # admin revokes → 402
        features.set_overrides(self.user, {"guided_tree": False})
        blocked = self.client.post(url, {"title": "y"}, format="json", headers={"x-api-key": self.key})
        self.assertEqual(blocked.status_code, 402)

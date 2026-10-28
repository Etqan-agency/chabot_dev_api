"""Guided question tree: positional mirroring, translation (mocked), cached
reads, canonical-only writes, and the HTTP surface."""

from __future__ import annotations

import json
import re
import threading
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from knowledge.models import AvailableLanguage, QuestionTreeNode
from knowledge.services import guided_tree as gt

from .factories import make_tenant


def fake_translate(text, target_lang, source_lang=None):
    """Deterministic stand-in for the LLM (``gt._translate``): prefix the target lang code."""
    return f"[{target_lang}] {text}"


def fake_chunk(items, target_lang, source_lang=None):
    """Stand-in for a batched LLM call: each text through ``gt._translate`` (whatever
    a test patched it to); a text that fails is left out, as a model dropping it would."""
    out = {}
    for key, text in items.items():
        try:
            result = gt._translate(text, target_lang, source_lang)
        except Exception:
            continue
        if result:
            out[key] = result
    return out


# Run translation inline so tests are deterministic; canonical = 'ar'.
GT_SETTINGS = {
    "GUIDED_TREE_TRANSLATE_MODE": "sync",
    "GUIDED_TREE_CANONICAL_LANGUAGE": "ar",
    "GUIDED_TREE_CACHE_TTL": 300,
}


@override_settings(**GT_SETTINGS)
class MirroringTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("tree_owner")
        AvailableLanguage.objects.create(code="ar", name="Arabic")
        AvailableLanguage.objects.create(code="en", name="English")
        cache.clear()
        self.p = mock.patch.object(gt, "_translate", side_effect=fake_translate)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.chunk = mock.patch.object(gt, "_translate_chunk", side_effect=fake_chunk)
        self.chunk.start()
        self.addCleanup(self.chunk.stop)

    def test_create_canonical_generates_mirror(self):
        root = gt.create_node(owner=self.user, title="ما هي الخدمة؟", answer="نحن نبيع")
        en = QuestionTreeNode.objects.get(owner=self.user, language="en")
        self.assertEqual(en.title, "[en] ما هي الخدمة؟")
        self.assertEqual(en.answer, "[en] نحن نبيع")
        self.assertEqual(en.order, root.order)
        self.assertIsNone(en.parent)

    def test_mirror_matches_by_position_not_fk(self):
        root = gt.create_node(owner=self.user, title="root")
        child = gt.create_node(owner=self.user, parent=root, title="child")
        en_child = gt.mirror_of(child, "en")
        self.assertIsNotNone(en_child)
        self.assertEqual(en_child.language, "en")
        self.assertEqual(gt.position_path(en_child), gt.position_path(child))
        # canonical_of walks back from the mirror to the source.
        self.assertEqual(gt.canonical_of(en_child).id, child.id)

    def test_order_autoincrement_and_uniqueness(self):
        a = gt.create_node(owner=self.user, title="a")
        b = gt.create_node(owner=self.user, title="b")
        self.assertEqual([a.order, b.order], [0, 1])
        with self.assertRaises(ValueError):
            gt.create_node(owner=self.user, title="dup", order=0)  # sibling order taken

    def test_edit_targets_canonical_and_retranslates(self):
        root = gt.create_node(owner=self.user, title="old")
        en = gt.mirror_of(root, "en")
        # Edit via the MIRROR node — must resolve to canonical and update both.
        gt.update_node(en, title="new")
        root.refresh_from_db()
        self.assertEqual(root.title, "new")
        self.assertEqual(gt.mirror_of(root, "en").title, "[en] new")

    def test_delete_removes_all_mirrors_and_canonical(self):
        root = gt.create_node(owner=self.user, title="x")
        gt.create_node(owner=self.user, parent=root, title="y")
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user).count(), 4)  # 2 ar + 2 en
        gt.delete_node(gt.mirror_of(root, "en"))  # delete via a mirror
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user).count(), 0)

    def test_resync_language_rebuilds_from_canonical(self):
        gt.create_node(owner=self.user, title="one")
        gt.create_node(owner=self.user, title="two")
        QuestionTreeNode.objects.filter(owner=self.user, language="en").delete()
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user, language="en").count(), 0)
        gt.resync_language(self.user, "en")
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user, language="en").count(), 2)


@override_settings(**GT_SETTINGS)
class TranslateTextTests(APITestCase):
    def test_empty_is_passthrough(self):
        self.assertEqual(gt.translate_text("", "en"), "")
        self.assertEqual(gt.translate_text(None, "en"), "")

    def test_strips_quotes(self):
        class LLM:
            def complete(self, s, u, *, temperature=0):
                return '"Hello"'

        with mock.patch("knowledge.services.llm.get_backend", return_value=LLM()):
            self.assertEqual(gt.translate_text("مرحبا", "en"), "Hello")

    def test_returns_original_on_total_failure(self):
        with mock.patch("knowledge.services.llm.get_backend", side_effect=RuntimeError):
            self.assertEqual(gt.translate_text("مرحبا", "en"), "مرحبا")  # never throws


@override_settings(**GT_SETTINGS)
class CachedReadTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("reader")
        AvailableLanguage.objects.create(code="ar", name="Arabic")
        cache.clear()

    def test_build_tree_is_nested_and_ordered(self):
        with mock.patch.object(gt, "active_target_languages", return_value=[]):
            root = gt.create_node(owner=self.user, title="root")
            gt.create_node(owner=self.user, parent=root, title="c0", order=0)
            gt.create_node(owner=self.user, parent=root, title="c1", order=1)
        tree = gt.build_tree(self.user, "ar")
        self.assertEqual(len(tree), 1)
        self.assertEqual([c["title"] for c in tree[0]["children"]], ["c0", "c1"])

    def test_cache_is_used_and_invalidated_on_write(self):
        with mock.patch.object(gt, "active_target_languages", return_value=[]):
            gt.create_node(owner=self.user, title="first")
        gt.get_tree(self.user, "ar")  # populate cache
        self.assertIsNotNone(cache.get(f"guided_tree:{self.user.pk}:ar"))
        with mock.patch.object(gt, "active_target_languages", return_value=[]):
            gt.create_node(owner=self.user, title="second")  # write invalidates
        self.assertIsNone(cache.get(f"guided_tree:{self.user.pk}:ar"))


@override_settings(**GT_SETTINGS)
class GuidedTreeApiTests(APITestCase):
    def setUp(self):
        self.user, self.key = make_tenant("api_owner")
        self.admin, self.admin_key = make_tenant("api_admin", is_staff=True)
        AvailableLanguage.objects.create(code="ar", name="Arabic")
        AvailableLanguage.objects.create(code="en", name="English")
        cache.clear()
        self.p = mock.patch.object(gt, "_translate", side_effect=fake_translate)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.chunk = mock.patch.object(gt, "_translate_chunk", side_effect=fake_chunk)
        self.chunk.start()
        self.addCleanup(self.chunk.stop)

    def test_requires_auth(self):
        self.assertEqual(self.client.get(reverse("guided-tree-list")).status_code, 401)

    def test_create_and_read_tree(self):
        res = self.client.post(
            reverse("guided-tree-list"),
            {"title": "سؤال", "answer": "جواب"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 201)
        # Canonical tree
        ar = self.client.get(reverse("guided-tree-list"), headers={"x-api-key": self.key})
        self.assertEqual(ar.data["language"], "ar")
        self.assertEqual(len(ar.data["tree"]), 1)
        # Mirror tree
        en = self.client.get(reverse("guided-tree-list") + "?language=en", headers={"x-api-key": self.key})
        self.assertEqual(en.data["tree"][0]["title"], "[en] سؤال")

    def test_duplicate_order_rejected(self):
        self.client.post(
            reverse("guided-tree-list"), {"title": "a", "order": 0}, format="json", headers={"x-api-key": self.key}
        )
        res = self.client.post(
            reverse("guided-tree-list"), {"title": "b", "order": 0}, format="json", headers={"x-api-key": self.key}
        )
        self.assertEqual(res.status_code, 400)

    def test_tenant_isolation_on_read(self):
        gt.create_node(owner=self.admin, title="admin-only")
        res = self.client.get(reverse("guided-tree-list"), headers={"x-api-key": self.key})
        self.assertEqual(res.data["tree"], [])  # owner sees none of admin's nodes

    def test_delete_via_api(self):
        node = gt.create_node(owner=self.user, title="del")
        res = self.client.delete(reverse("guided-tree-detail", args=[node.id]), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 204)
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user).count(), 0)

    def test_languages_list_and_add_remove(self):
        # list
        res = self.client.get(reverse("tree-language-list"), headers={"x-api-key": self.key})
        self.assertEqual({lang["code"] for lang in res.data}, {"ar", "en"})
        # a tenant adds a language for its own tree
        res = self.client.post(
            reverse("tree-language-list"),
            {"code": "fr", "name": "French"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 201)
        # ...but only an admin adds one for every tenant
        res = self.client.post(
            reverse("tree-language-list"),
            {"code": "de", "name": "German", "global": True},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 403)
        res = self.client.post(
            reverse("tree-language-list"),
            {"code": "de", "name": "German", "global": True},
            format="json",
            headers={"x-api-key": self.admin_key},
        )
        self.assertEqual(res.status_code, 201)
        # canonical cannot be removed
        res = self.client.delete(reverse("tree-language-detail", args=["ar"]), headers={"x-api-key": self.admin_key})
        self.assertEqual(res.status_code, 400)

    def test_cannot_add_canonical_language(self):
        res = self.client.post(
            reverse("tree-language-list"),
            {"code": "ar", "name": "Arabic"},
            format="json",
            headers={"x-api-key": self.admin_key},
        )
        self.assertEqual(res.status_code, 400)

    def test_retrieve_node_and_404(self):
        node = gt.create_node(owner=self.user, title="one")
        ok = self.client.get(reverse("guided-tree-detail", args=[node.id]), headers={"x-api-key": self.key})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["title"], "one")
        missing = self.client.get(
            reverse("guided-tree-detail", args=["00000000-0000-0000-0000-000000000000"]),
            headers={"x-api-key": self.key},
        )
        self.assertEqual(missing.status_code, 404)

    def test_create_with_foreign_parent_rejected(self):
        other_root = gt.create_node(owner=self.admin, title="admins")
        res = self.client.post(
            reverse("guided-tree-list"),
            {"title": "child", "parent": str(other_root.id)},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_node_success_and_404(self):
        node = gt.create_node(owner=self.user, title="old")
        ok = self.client.patch(
            reverse("guided-tree-detail", args=[node.id]),
            {"title": "new"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["title"], "new")
        missing = self.client.patch(
            reverse("guided-tree-detail", args=["00000000-0000-0000-0000-000000000000"]),
            {"title": "x"},
            format="json",
            headers={"x-api-key": self.key},
        )
        self.assertEqual(missing.status_code, 404)

    def test_patch_duplicate_order_rejected(self):
        gt.create_node(owner=self.user, title="a", order=0)
        b = gt.create_node(owner=self.user, title="b", order=1)
        res = self.client.patch(
            reverse("guided-tree-detail", args=[b.id]), {"order": 0}, format="json", headers={"x-api-key": self.key}
        )
        self.assertEqual(res.status_code, 400)

    def test_delete_404(self):
        res = self.client.delete(
            reverse("guided-tree-detail", args=["00000000-0000-0000-0000-000000000000"]),
            headers={"x-api-key": self.key},
        )
        self.assertEqual(res.status_code, 404)

    def test_flat_dump(self):
        root = gt.create_node(owner=self.user, title="r")
        gt.create_node(owner=self.user, parent=root, title="c")
        res = self.client.get(reverse("guided-tree-flat") + "?language=ar", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 2)

    def test_retranslate_endpoint(self):
        gt.create_node(owner=self.user, title="r")
        res = self.client.post(
            reverse("guided-tree-retranslate"), {"language": "en"}, format="json", headers={"x-api-key": self.key}
        )
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["languages"], ["en"])

    def test_remove_language_success_and_404(self):
        AvailableLanguage.objects.create(code="fr", name="French")
        gt.create_node(owner=self.user, title="r")  # creates fr mirror too
        ok = self.client.delete(reverse("tree-language-detail", args=["fr"]), headers={"x-api-key": self.admin_key})
        self.assertEqual(ok.status_code, 204)
        self.assertFalse(AvailableLanguage.objects.filter(code="fr").exists())
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user, language="fr").count(), 0)
        missing = self.client.delete(
            reverse("tree-language-detail", args=["zz"]), headers={"x-api-key": self.admin_key}
        )
        self.assertEqual(missing.status_code, 404)

    def test_remove_language_non_admin_forbidden(self):
        res = self.client.delete(reverse("tree-language-detail", args=["en"]), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 403)


@override_settings(**GT_SETTINGS)
class ServiceBranchTests(APITestCase):
    def setUp(self):
        self.user, _ = make_tenant("svc_owner")
        AvailableLanguage.objects.create(code="ar", name="Arabic")
        AvailableLanguage.objects.create(code="en", name="English")
        cache.clear()
        self.p = mock.patch.object(gt, "_translate", side_effect=fake_translate)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.chunk = mock.patch.object(gt, "_translate_chunk", side_effect=fake_chunk)
        self.chunk.start()
        self.addCleanup(self.chunk.stop)

    def test_canonical_of_and_mirror_of_identity(self):
        root = gt.create_node(owner=self.user, title="r")
        self.assertEqual(gt.canonical_of(root).id, root.id)  # already canonical
        self.assertEqual(gt.mirror_of(root, "ar").id, root.id)  # same language

    def test_order_change_triggers_resync(self):
        a = gt.create_node(owner=self.user, title="a", order=0)
        gt.create_node(owner=self.user, title="b", order=1)
        # move a to order 5 (free) → mirrors resync to the new address
        gt.update_node(a, order=5)
        en = gt.mirror_of(QuestionTreeNode.objects.get(pk=a.pk), "en")
        self.assertIsNotNone(en)
        self.assertEqual(en.order, 5)

    def test_resync_language_noop_for_canonical(self):
        gt.create_node(owner=self.user, title="r")
        before = QuestionTreeNode.objects.filter(owner=self.user, language="ar").count()
        gt.resync_language(self.user, "ar")  # canonical → no-op
        self.assertEqual(QuestionTreeNode.objects.filter(owner=self.user, language="ar").count(), before)

    @override_settings(GUIDED_TREE_CACHE_TTL=0)
    def test_get_tree_without_cache(self):
        gt.create_node(owner=self.user, title="r")
        tree = gt.get_tree(self.user, "ar")  # ttl=0 → build directly, no cache write
        self.assertEqual(len(tree), 1)
        self.assertIsNone(cache.get(f"guided_tree:{self.user.pk}:ar"))

    def test_sync_isolates_child_failure(self):
        # Build the canonical tree WITHOUT generating mirrors, then sync with one
        # child's translation blowing up — the sibling must still get its mirror
        # (per-node failure isolation).
        with mock.patch.object(gt, "active_target_languages", return_value=[]):
            root = gt.create_node(owner=self.user, title="root")
            gt.create_node(owner=self.user, parent=root, title="bad", order=0)
            gt.create_node(owner=self.user, parent=root, title="good", order=1)

        def flaky(text, target_lang, source_lang=None):
            if text == "bad":
                raise RuntimeError("translate boom")
            return f"[{target_lang}] {text}"

        with mock.patch.object(gt, "_translate", side_effect=flaky):
            gt.sync_language(self.user, "en")
        titles = set(QuestionTreeNode.objects.filter(owner=self.user, language="en").values_list("title", flat=True))
        self.assertIn("[en] good", titles)  # sibling survived the bad child

    def test_resync_isolates_root_failure(self):
        with mock.patch.object(gt, "active_target_languages", return_value=[]):
            gt.create_node(owner=self.user, title="bad", order=0)
            gt.create_node(owner=self.user, title="good", order=1)

        def flaky(text, target_lang, source_lang=None):
            if text == "bad":
                raise RuntimeError("boom")
            return f"[{target_lang}] {text}"

        with mock.patch.object(gt, "_translate", side_effect=flaky):
            gt.resync_language(self.user, "en")  # one root fails, the other completes
        self.assertTrue(QuestionTreeNode.objects.filter(owner=self.user, language="en", title="[en] good").exists())


class TranslateFallbackTests(APITestCase):
    def test_fallback_provider_used_when_primary_fails(self):
        class Primary:
            def complete(self, s, u, *, temperature=0):
                raise RuntimeError("primary down")

        class Fallback:
            def complete(self, s, u, *, temperature=0):
                return "translated"

        backends = iter([Primary(), Fallback()])
        with mock.patch("knowledge.services.llm.get_backend", side_effect=lambda m=None: next(backends)):
            self.assertEqual(gt.translate_text("مرحبا", "en"), "translated")


@override_settings(**GT_SETTINGS)
class TenantLanguageSyncTests(APITestCase):
    """Per-tenant languages and the incremental, in-place mirror sync."""

    def setUp(self):
        self.user, self.key = make_tenant("lang_owner")
        self.other, self.other_key = make_tenant("lang_other")
        cache.clear()
        self.translate = mock.patch.object(gt, "_translate", side_effect=fake_translate).start()
        mock.patch.object(gt, "_translate_chunk", side_effect=fake_chunk).start()
        self.addCleanup(mock.patch.stopall)

    def add_language(self, code, key=None):
        return self.client.post(
            reverse("tree-language-list"),
            {"code": code, "name": code.upper()},
            format="json",
            headers={"x-api-key": key or self.key},
        )

    def titles(self, language, owner=None):
        return sorted(
            QuestionTreeNode.objects.filter(owner=owner or self.user, language=language).values_list(
                "title", flat=True
            )
        )

    def test_adding_a_language_translates_the_whole_tree_keeping_its_shape(self):
        root = gt.create_node(owner=self.user, title="root", answer="a")
        child = gt.create_node(owner=self.user, parent=root, title="child")
        gt.create_node(owner=self.user, parent=child, title="grandchild")
        self.assertEqual(self.add_language("en").status_code, 201)
        self.assertEqual(self.titles("en"), ["[en] child", "[en] grandchild", "[en] root"])
        en_child = gt.mirror_of(child, "en")
        self.assertEqual(en_child.parent.title, "[en] root")  # under its parent, not a root
        self.assertEqual(gt.mirror_of(root, "en").answer, "[en] a")

    def test_languages_are_per_tenant(self):
        gt.create_node(owner=self.user, title="mine")
        gt.create_node(owner=self.other, title="theirs")
        self.add_language("en")
        self.assertEqual(self.titles("en"), ["[en] mine"])
        self.assertEqual(self.titles("en", owner=self.other), [])  # the other tenant never asked for en
        codes = {
            row["code"]
            for row in self.client.get(reverse("tree-language-list"), headers={"x-api-key": self.other_key}).data
        }
        self.assertNotIn("en", codes)

    def test_new_question_is_translated_into_every_language(self):
        self.add_language("en")
        self.add_language("fr")
        root = gt.create_node(owner=self.user, title="q")
        gt.create_node(owner=self.user, parent=root, title="sub")
        self.assertEqual(self.titles("en"), ["[en] q", "[en] sub"])
        self.assertEqual(self.titles("fr"), ["[fr] q", "[fr] sub"])
        self.assertEqual(gt.mirror_of(root.children.get(), "fr").parent.title, "[fr] q")

    def test_child_written_before_its_parent_mirror_still_lands_under_it(self):
        # A burst upload: the canonical tree exists before any mirror does.
        with mock.patch.object(gt, "schedule_sync"):
            root = gt.create_node(owner=self.user, title="root")
            gt.create_node(owner=self.user, parent=root, title="child")
            AvailableLanguage.objects.create(owner=self.user, code="en", name="English")
        gt.sync_owner(self.user.pk)
        self.assertEqual([n["title"] for n in gt.build_tree(self.user, "en")], ["[en] root"])
        self.assertEqual(gt.build_tree(self.user, "en")[0]["children"][0]["title"], "[en] child")

    def test_sync_translates_only_what_changed(self):
        self.add_language("en")
        a = gt.create_node(owner=self.user, title="a", answer="x")
        gt.create_node(owner=self.user, title="b")
        self.translate.reset_mock()
        gt.update_node(a, title="a2")
        self.assertEqual(sorted(c.args[0] for c in self.translate.call_args_list), ["a2", "x"])
        self.assertEqual(self.titles("en"), ["[en] a2", "[en] b"])

    def test_retranslate_is_in_place(self):
        self.add_language("en")
        node = gt.create_node(owner=self.user, title="q")
        mirror_id = gt.mirror_of(node, "en").id
        self.translate.side_effect = lambda text, lang, src=None: f"<{lang}> {text}"
        res = self.client.post(reverse("guided-tree-retranslate"), {}, format="json", headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 202)
        mirror = gt.mirror_of(node, "en")
        self.assertEqual((mirror.id, mirror.title), (mirror_id, "<en> q"))  # same row, never wiped

    def test_failed_translation_keeps_source_text_and_is_retried(self):
        self.translate.side_effect = lambda *a, **k: None  # every model down
        self.add_language("en")
        node = gt.create_node(owner=self.user, title="q")
        mirror = gt.mirror_of(node, "en")
        self.assertEqual((mirror.title, mirror.source_hash), ("q", ""))
        row = self.client.get(reverse("tree-language-list"), headers={"x-api-key": self.key}).data[0]
        self.assertEqual((row["translated_nodes"], row["total_nodes"]), (0, 1))  # not counted as done
        self.translate.side_effect = fake_translate  # back up
        gt.sync_owner(self.user.pk)
        self.assertEqual(gt.mirror_of(node, "en").title, "[en] q")

    def test_moved_and_deleted_nodes_leave_no_stale_mirrors(self):
        self.add_language("en")
        a = gt.create_node(owner=self.user, title="a", order=0)
        b = gt.create_node(owner=self.user, title="b", order=1)
        gt.update_node(a, order=5)
        gt.delete_node(b)
        self.assertEqual(
            list(QuestionTreeNode.objects.filter(owner=self.user, language="en").values_list("order", "title")),
            [(5, "[en] a")],
        )

    def test_removing_own_language_drops_only_own_mirrors(self):
        gt.create_node(owner=self.user, title="mine")
        gt.create_node(owner=self.other, title="theirs")
        self.add_language("en")
        self.add_language("en", key=self.other_key)
        res = self.client.delete(reverse("tree-language-detail", args=["en"]), headers={"x-api-key": self.key})
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self.titles("en"), [])
        self.assertEqual(self.titles("en", owner=self.other), ["[en] theirs"])

    def test_cannot_shadow_a_global_language(self):
        AvailableLanguage.objects.create(code="en", name="English")
        self.assertEqual(self.add_language("en").status_code, 400)

    def test_list_reports_translation_progress(self):
        gt.create_node(owner=self.user, title="q1")
        gt.create_node(owner=self.user, title="q2")
        self.add_language("en")
        row = next(
            r
            for r in self.client.get(reverse("tree-language-list"), headers={"x-api-key": self.key}).data
            if r["code"] == "en"
        )
        self.assertEqual(
            (row["scope"], row["translated_nodes"], row["total_nodes"], row["translating"]), ("account", 2, 2, False)
        )

    def test_a_running_sync_absorbs_later_writes(self):
        lock, _ = gt._sync_keys(self.user.pk)
        AvailableLanguage.objects.create(owner=self.user, code="en", name="English")
        cache.add(lock, 1)  # another worker is mid-sync
        gt.create_node(owner=self.user, title="late")
        self.assertEqual(self.titles("en"), [])  # this write only marked the tenant dirty
        self.assertTrue(gt.is_syncing(self.user))
        cache.delete(lock)  # the holder finishes; its re-check of "dirty" runs again:
        gt.sync_owner(self.user.pk)
        self.assertEqual(self.titles("en"), ["[en] late"])


class FakeLLM:
    """An LLM backend that answers both the batched (JSON) and the single-text prompt."""

    def __init__(self, *, drop=None, fence=False, batch_down=False):
        self.drop, self.fence, self.batch_down = drop, fence, batch_down
        self.batch_calls, self.single_calls = [], []
        self.lock = threading.Lock()

    def complete(self, system, user, *, temperature=0):
        lang = re.search(r"code '([^']+)'", system).group(1)
        if "JSON object" in system:
            with self.lock:
                self.batch_calls.append((lang, json.loads(user)))
            if self.batch_down:
                raise RuntimeError("batch endpoint down")
            out = {k: f"[{lang}] {v}" for k, v in json.loads(user).items() if v != self.drop}
            body = json.dumps(out, ensure_ascii=False)
            return f"```json\n{body}\n```" if self.fence else body
        with self.lock:
            self.single_calls.append((lang, user))
        return f"[{lang}*] {user}"


@override_settings(**GT_SETTINGS)
class BatchedTranslationTests(APITestCase):
    """Translation is batched (one LLM call per batch) and every language runs at once."""

    def setUp(self):
        self.user, _ = make_tenant("batch_owner")
        cache.clear()
        with mock.patch.object(gt, "schedule_sync"):
            root = gt.create_node(owner=self.user, title="root", answer="root answer")
            for i in range(3):
                gt.create_node(owner=self.user, parent=root, title=f"child {i}")
        for code in ("en", "fr"):
            AvailableLanguage.objects.create(owner=self.user, code=code, name=code)

    def sync(self, llm):
        with mock.patch("knowledge.services.llm.get_backend", return_value=llm):
            gt.sync_owner(self.user.pk)

    def titles(self, language):
        return sorted(
            QuestionTreeNode.objects.filter(owner=self.user, language=language).values_list("title", flat=True)
        )

    def test_whole_tree_is_one_call_per_language(self):
        llm = FakeLLM()
        self.sync(llm)
        self.assertEqual(sorted(lang for lang, _ in llm.batch_calls), ["en", "fr"])
        self.assertEqual(len(llm.batch_calls[0][1]), 5)  # 4 titles + 1 answer, together
        self.assertEqual(llm.single_calls, [])
        self.assertEqual(self.titles("fr"), ["[fr] child 0", "[fr] child 1", "[fr] child 2", "[fr] root"])
        root_en = QuestionTreeNode.objects.get(owner=self.user, language="en", parent=None)
        self.assertEqual(root_en.answer, "[en] root answer")
        self.assertEqual(root_en.children.count(), 3)  # the shape survives batching

    @override_settings(GUIDED_TREE_TRANSLATE_BATCH_ITEMS=2)
    def test_large_trees_are_split_into_batches(self):
        llm = FakeLLM()
        self.sync(llm)
        self.assertEqual(len(llm.batch_calls), 6)  # 5 texts / 2 per batch = 3 per language
        self.assertEqual(len(self.titles("en")), 4)

    def test_fenced_reply_is_parsed_and_dropped_texts_fall_back_to_one_call(self):
        llm = FakeLLM(fence=True, drop="child 1")
        self.sync(llm)
        self.assertIn("[en] child 0", self.titles("en"))  # read through the ```json fence
        self.assertIn("[en*] child 1", self.titles("en"))  # the model dropped it: translated alone
        self.assertEqual(sorted(llm.single_calls), [("en", "child 1"), ("fr", "child 1")])

    def test_a_failed_batch_falls_back_to_one_call_per_text(self):
        llm = FakeLLM(batch_down=True)
        self.sync(llm)
        self.assertEqual(self.titles("en"), ["[en*] child 0", "[en*] child 1", "[en*] child 2", "[en*] root"])
        self.assertFalse(QuestionTreeNode.objects.filter(owner=self.user, language="en", source_hash="").exists())

    def test_unchanged_nodes_are_not_sent_again(self):
        self.sync(FakeLLM())
        llm = FakeLLM()
        node = QuestionTreeNode.objects.get(owner=self.user, language="ar", title="child 2")
        node.title = "child 2 edited"
        node.save()
        self.sync(llm)
        self.assertEqual(
            sorted(items for _, batch in llm.batch_calls for items in batch.values()), ["child 2 edited"] * 2
        )

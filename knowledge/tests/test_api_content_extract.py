"""extract_items() — the whole API payload becomes records, not just `results`."""

from __future__ import annotations

from django.test import SimpleTestCase

from knowledge.services.api_content_processor import extract_items


class ExtractItemsTests(SimpleTestCase):
    def test_envelope_fields_are_kept_alongside_the_items(self):
        payload = {
            "count": 2,
            "next": "https://api.x.com/items/?page=2",
            "results": [{"id": 1}, {"id": 2}],
        }
        items = extract_items(payload)
        self.assertEqual(items[:2], [{"id": 1}, {"id": 2}])
        self.assertEqual(
            items[2],
            {"response_metadata": {"count": 2, "next": "https://api.x.com/items/?page=2"}},
        )

    def test_bare_list_payload_is_used_as_is(self):
        self.assertEqual(extract_items([{"id": 1}, {"id": 2}]), [{"id": 1}, {"id": 2}])

    def test_items_list_is_auto_detected_when_the_key_is_absent(self):
        payload = {"status": "ok", "data": [{"id": 1}, {"id": 2}, {"id": 3}]}
        items = extract_items(payload)  # default items_key="results" is absent
        self.assertEqual(items[:3], [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual(items[3], {"response_metadata": {"status": "ok"}})

    def test_explicit_key_wins_over_a_longer_list(self):
        payload = {"results": [{"id": 1}], "logs": [1, 2, 3, 4]}
        items = extract_items(payload, items_key="results")
        self.assertEqual(items[0], {"id": 1})
        self.assertEqual(items[1], {"response_metadata": {"logs": [1, 2, 3, 4]}})

    def test_object_without_any_list_is_a_single_record(self):
        payload = {"id": 7, "title": "About us", "body": "..."}
        self.assertEqual(extract_items(payload), [payload])

    def test_empty_results_still_yields_the_metadata(self):
        self.assertEqual(
            extract_items({"count": 0, "results": []}),
            [{"response_metadata": {"count": 0}}],
        )

    def test_nothing_at_all_yields_no_records(self):
        self.assertEqual(extract_items({"results": []}), [])
        self.assertEqual(extract_items({}), [])
        self.assertEqual(extract_items([]), [])

    def test_scalar_payload_is_wrapped(self):
        self.assertEqual(extract_items("hello"), [{"value": "hello"}])

    def test_metadata_record_cannot_collide_with_an_item_id(self):
        # The envelope carries its own "id"; nesting it under response_metadata
        # keeps it from overwriting the item that shares that id.
        payload = {"id": 1, "results": [{"id": 1, "title": "A"}]}
        items = extract_items(payload)
        self.assertEqual(items, [{"id": 1, "title": "A"}, {"response_metadata": {"id": 1}}])

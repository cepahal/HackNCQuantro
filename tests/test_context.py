import unittest

from backend.services.specialist.context import select_surrounding_context


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.alignment = {
            "status": "verified",
            "source_media_id": "source_001",
            "matches": [{
                "match_id": "match_001",
                "source_start_seconds": 8.0,
                "source_end_seconds": 16.0,
            }],
        }

        self.features = {
            "media_id": "source_001",
            "duration_seconds": 24.0,
            "transcript": {
                "status": "complete",
                "processed_ranges": [{
                    "start_seconds": 0.0,
                    "end_seconds": 24.0,
                }],
                "sentences": [
                    {
                        "sentence_id": "s1",
                        "start_seconds": 0.0,
                        "end_seconds": 8.0,
                        "text": "Would you cancel the program?",
                        "speaker_id": None,
                    },
                    {
                        "sentence_id": "s2",
                        "start_seconds": 8.0,
                        "end_seconds": 16.0,
                        "text": "If those numbers were accurate, yes.",
                        "speaker_id": None,
                    },
                    {
                        "sentence_id": "s3",
                        "start_seconds": 16.0,
                        "end_seconds": 24.0,
                        "text": "But those numbers are not accurate.",
                        "speaker_id": None,
                    },
                ],
            },
        }

    def test_padding_clamps_to_recording(self):
        result = select_surrounding_context(self.alignment, self.features)
        window = result["windows"][0]

        self.assertEqual(window["requested_start_seconds"], 0.0)
        self.assertEqual(window["requested_end_seconds"], 24.0)
        self.assertEqual(window["coverage"], "complete")
        self.assertEqual(len(window["sentences"]), 3)

    def test_missing_processing_is_partial(self):
        self.features["transcript"]["processed_ranges"] = [{
            "start_seconds": 8.0,
            "end_seconds": 16.0,
        }]

        result = select_surrounding_context(self.alignment, self.features)

        self.assertEqual(result["windows"][0]["coverage"], "partial")
        self.assertTrue(result["limitations"])

    def test_processed_silence_is_complete(self):
        self.features["transcript"]["sentences"] = []

        result = select_surrounding_context(self.alignment, self.features)

        self.assertEqual(result["windows"][0]["coverage"], "complete")
        self.assertEqual(result["windows"][0]["sentences"], [])

    def test_no_processed_data_is_unavailable(self):
        self.features["transcript"]["processed_ranges"] = []
        self.features["transcript"]["sentences"] = []

        result = select_surrounding_context(self.alignment, self.features)

        self.assertEqual(result["windows"][0]["coverage"], "unavailable")

    def test_windows_merge_and_preserve_match_ids(self):
        self.alignment["matches"].append({
            "match_id": "match_002",
            "source_start_seconds": 18.0,
            "source_end_seconds": 22.0,
        })

        result = select_surrounding_context(
            self.alignment,
            self.features,
            {"padding_seconds": 2.0},
        )

        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(
            result["windows"][0]["match_ids"],
            ["match_001", "match_002"],
        )

        separate = select_surrounding_context(
            self.alignment,
            self.features,
            {
                "padding_seconds": 2.0,
                "merge_overlapping_windows": False,
            },
        )

        self.assertEqual(len(separate["windows"]), 2)

    def test_rejects_unverified_alignment(self):
        self.alignment["status"] = "no_verified_match"

        with self.assertRaises(ValueError):
            select_surrounding_context(self.alignment, self.features)


if __name__ == "__main__":
    unittest.main()
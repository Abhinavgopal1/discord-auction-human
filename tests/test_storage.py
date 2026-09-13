import json
import tempfile
import unittest
from pathlib import Path

from storage import STATE_KEYS, StateStore


class StorageTests(unittest.TestCase):
    def test_legacy_files_migrate_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "teams.json").write_text(
                json.dumps({"1": [{"name": "A"}]}), encoding="utf-8"
            )
            (root / "lineups.json").write_text(
                json.dumps({"1": {"players": []}}), encoding="utf-8"
            )
            data = StateStore(root).load()
            self.assertEqual(data["user_teams"]["1"][0]["name"], "A")
            self.assertIn("main", data["user_lineups"]["1"])
            self.assertTrue((root / "teams.json").exists())

    def test_failed_serialization_does_not_replace_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory)
            empty = {key: {} for key in STATE_KEYS}
            store.save(empty)
            before = store.path.read_text(encoding="utf-8")
            invalid = {key: {} for key in STATE_KEYS}
            invalid["user_teams"] = {"1": {"bad": float("nan")}}
            with self.assertRaises(ValueError):
                store.save(invalid)
            self.assertEqual(store.path.read_text(encoding="utf-8"), before)

    def test_version_mismatch_is_safe_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                StateStore(directory).load()
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["version"], 99
            )


if __name__ == "__main__":
    unittest.main()

"""Data integrity regressions; these tests run without Discord or network access."""

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1] / "players"
POSITIONS = {"GK", "CB", "LB", "RB", "CM", "CAM", "LW", "RW", "ST"}


class PlayerPoolTests(unittest.TestCase):
    def test_every_player_file_is_valid_and_has_no_repeated_names(self):
        for path in ROOT.rglob("*.json"):
            with self.subTest(file=str(path.relative_to(ROOT))):
                rows = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(rows, list)
                self.assertTrue(rows)
                names = set()
                for row in rows:
                    self.assertIsInstance(row, dict)
                    self.assertIsInstance(row["name"], str)
                    name = row["name"].strip().casefold()
                    self.assertTrue(name)
                    self.assertNotIn(name, names, row["name"])
                    names.add(name)
                    self.assertEqual(row["position"].upper(), path.stem.upper())
                    self.assertIs(type(row["base_price"]), int)
                    self.assertGreaterEqual(row["base_price"], 1_000_000)
                    self.assertLessEqual(row["base_price"], 50_000_000)
                    if "tier" in row:
                        self.assertEqual(row["tier"], self.tier(row["base_price"]))

    @staticmethod
    def tier(price):
        return "A" if price >= 40_000_000 else "B" if price >= 25_000_000 else "C"

    def test_current_season_has_unique_players_and_every_position_and_tier(self):
        paths = list((ROOT / "26-27").glob("*.json"))
        self.assertEqual({p.stem.upper() for p in paths}, POSITIONS)
        names = set()
        clubs = set()
        for path in paths:
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(rows), 20, path.name)
            self.assertEqual({r["tier"] for r in rows}, {"A", "B", "C"}, path.name)
            for row in rows:
                name = row["name"].casefold()
                self.assertNotIn(name, names, row["name"])
                names.add(name)
                clubs.add(row["club"])
                for key in ("club", "nation", "league"):
                    self.assertTrue(row[key].strip(), (name, key))
                self.assertEqual(row["season"], "2026-27")
                self.assertEqual(row["verified_on"], "2026-09-11")
                self.assertIs(type(row["rating"]), int)
                self.assertGreaterEqual(row["rating"], 65)
                self.assertLessEqual(row["rating"], 94)
                source = urlparse(row["source_url"])
                self.assertEqual(source.scheme, "https")
                self.assertIn(
                    source.hostname,
                    {
                        "www.premierleague.com",
                        "www.realmadrid.com",
                        "www.fcbarcelona.com",
                        "www.psg.fr",
                        "fcbayern.com",
                    },
                )
                self.assertGreater(len(source.path), 1)
                expected = round(1 + 49 * ((row["rating"] - 65) / 29) ** 2) * 1_000_000
                self.assertEqual(row["base_price"], expected, name)
        self.assertGreaterEqual(len(names), 250)
        self.assertGreaterEqual(len(clubs), 15)

    def test_2024_left_wingers_are_not_a_copy_of_attacking_midfielders(self):
        lw = json.loads((ROOT / "24-25" / "lw.json").read_text(encoding="utf-8"))
        cam = json.loads((ROOT / "24-25" / "cam.json").read_text(encoding="utf-8"))
        self.assertNotEqual({r["name"] for r in lw}, {r["name"] for r in cam})
        self.assertTrue(all(r["position"] == "LW" for r in lw))
        self.assertIn("Raphinha", {r["name"] for r in lw})
        self.assertIn("Cole Palmer", {r["name"] for r in cam})


if __name__ == "__main__":
    unittest.main()

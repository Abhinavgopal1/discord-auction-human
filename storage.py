"""Atomic snapshots and one-time, non-destructive legacy JSON migration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_KEYS = (
    "user_teams",
    "user_budgets",
    "user_lineups",
    "active_lineups",
    "user_stats",
    "draft_clash_wins",
    "koth_state",
)
LEGACY_FILES = {
    "user_teams": "teams.json",
    "user_budgets": "budgets.json",
    "user_lineups": "lineups.json",
    "user_stats": "stats.json",
    "active_lineups": "active_lineups.json",
    "draft_clash_wins": "draft_clash_wins.json",
    "koth_state": "koth.json",
}


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"

    def load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError(
                    "Unsupported state.json format; existing data was left intact."
                )
        else:
            data = {}
            for key, filename in LEGACY_FILES.items():
                legacy = self.directory / filename
                if legacy.exists():
                    data[key] = json.loads(legacy.read_text(encoding="utf-8"))
        result = {}
        for key in STATE_KEYS:
            value = data.get(key, {})
            if not isinstance(value, dict):
                raise ValueError(f"Invalid saved {key}; existing data was left intact.")
            result[key] = value
        for uid, lineups in result["user_lineups"].items():
            if isinstance(lineups, dict) and isinstance(lineups.get("players"), list):
                result["user_lineups"][uid] = {"main": lineups}
        return result

    def save(self, state):
        self.directory.mkdir(parents=True, exist_ok=True)
        snapshot = {"version": 1, **{key: state[key] for key in STATE_KEYS}}
        # Serialize first so invalid state cannot damage the previous snapshot.
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False)
        fd, temporary = tempfile.mkstemp(
            prefix="state-", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

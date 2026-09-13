"""vodsearch/channels.py - Hand-editable JSON registry of Twitch channels for VOD downloading.
Per-VOD state lives elsewhere in the SQLite index."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")


def normalize_login(s: str) -> str:
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    if "twitch.tv/" in s:
        idx = s.index("twitch.tv/") + len("twitch.tv/")
        segment = s[idx:].split("/")[0].split("?")[0].split("#")[0]
        s = segment
    s = s.lower()
    if not LOGIN_RE.match(s):
        raise ValueError(f"invalid Twitch login: {s!r}")
    return s


def parse_tracks(spec: str) -> Dict[int, str]:
    if not spec or not spec.strip():
        return {}
    result: Dict[int, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            num_str, label = part.split(":", 1)
            result[int(num_str.strip())] = label.strip()
        else:
            n = int(part.strip())
            result[n] = f"track{n}"
    return result


@dataclass
class Channel:
    login: str
    user_id: str = ""
    display_name: str = ""
    root: str = ""
    profile: str = ""
    tracks: str = ""
    quality: str = ""
    keep: int = -1
    enabled: bool = True
    added_at: str = ""

    def track_map(self) -> Dict[int, str]:
        return parse_tracks(self.tracks) if self.tracks else {0: self.login}

    def to_dict(self) -> dict:
        return {
            "login": self.login,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "root": self.root,
            "profile": self.profile,
            "tracks": self.tracks,
            "quality": self.quality,
            "keep": self.keep,
            "enabled": self.enabled,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Channel:
        return cls(
            login=normalize_login(d.get("login", "")),
            user_id=d.get("user_id", ""),
            display_name=d.get("display_name", ""),
            root=d.get("root", ""),
            profile=d.get("profile", ""),
            tracks=d.get("tracks", ""),
            quality=d.get("quality", ""),
            keep=d.get("keep", -1),
            enabled=d.get("enabled", True),
            added_at=d.get("added_at", ""),
        )


class Registry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.channels: Dict[str, Channel] = {}
        self.load()

    def load(self) -> Dict[str, Channel]:
        if not self.path.exists():
            return self.channels
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"corrupt JSON at {self.path}: {e}") from e
        self.channels = {}
        for login, ch_data in data.get("channels", {}).items():
            ch = Channel.from_dict(ch_data)
            self.channels[ch.login] = ch
        return self.channels

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        payload = {"version": 1, "channels": {k: v.to_dict() for k, v in sorted(self.channels.items())}}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(str(tmp_path), str(self.path))

    def get(self, login: str) -> Optional[Channel]:
        try:
            return self.channels.get(normalize_login(login))
        except ValueError:
            return None

    def add(self, ch: Channel) -> Channel:
        ch.login = normalize_login(ch.login)
        if ch.login in self.channels:
            raise ValueError(f"channel {ch.login!r} already present")
        if not ch.added_at:
            ch.added_at = datetime.now().replace(microsecond=0).isoformat()
        self.channels[ch.login] = ch
        return ch

    def update(self, ch: Channel) -> Channel:
        ch.login = normalize_login(ch.login)
        if ch.login not in self.channels:
            raise KeyError(ch.login)
        self.channels[ch.login] = ch
        return ch

    def remove(self, login: str) -> bool:
        try:
            login = normalize_login(login)
        except ValueError:
            return False
        return self.channels.pop(login, None) is not None

    def enabled(self) -> List[Channel]:
        return sorted(
            (ch for ch in self.channels.values() if ch.enabled),
            key=lambda c: c.login,
        )

    def __len__(self) -> int:
        return len(self.channels)

    def __contains__(self, login: str) -> bool:
        try:
            return normalize_login(login) in self.channels
        except ValueError:
            return False

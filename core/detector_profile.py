"""
Detector profile configuration for SMITE 2 HUD element localization.

JSON Schema (v1):
{
  "schema": 1,
  "channel": "streamer_name",
  "canvas": [1920, 1080],
  "regions": {
    "kda": [x1, y1, x2, y2],
    ...
  },
  "group_mode": "gaps",
  "kda_field_windows": {"K": [x1, x2], "D": [...], "A": [...]},
  "portrait_enabled": true,
  "overlay_icons": false,
  "reference_icons": false,
  "digit_templates_dir": "path/to/templates",
  "notes": "optional remarks"
}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from core.detector_regions import DEFAULTS, load_regions

CANVAS = (1920, 1080)
REGION_KEYS = ("kda", "portrait", "overlay_check", "hud_check", "gameplay_check")
CROP_KEYS = ("kda", "overlay_check", "hud_check", "gameplay_check")
GROUP_MODES = ("gaps", "fields")
SCHEMA_VERSION = 1

class ProfileError(ValueError):
    pass

def union_box(regions: Iterable[Tuple[int, int, int, int]], canvas: Tuple[int, int] = CANVAS) -> Tuple[int, int, int, int]:
    regions_list = list(regions)
    if not regions_list:
        return (0, 0, canvas[0], canvas[1])
        
    x1 = min(r[0] for r in regions_list)
    y1 = min(r[1] for r in regions_list)
    x2 = max(r[2] for r in regions_list)
    y2 = max(r[3] for r in regions_list)
    
    x1 = max(0, x1)
    y1 = max(0, y1)
    x2 = min(canvas[0], x2)
    y2 = min(canvas[1], y2)
    
    x = x1 - (x1 % 2)
    y = y1 - (y1 % 2)
    x2_even = x2 + (1 if x2 % 2 else 0)
    y2_even = y2 + (1 if y2 % 2 else 0)
    
    w = x2_even - x
    h = y2_even - y
    
    if x + w > canvas[0]:
        w = canvas[0] - x
    if y + h > canvas[1]:
        h = canvas[1] - y
        
    return (x, y, w, h)

@dataclass(frozen=True)
class DetectorProfile:
    regions: Dict[str, Tuple[int, int, int, int]]
    group_mode: str = "fields"
    kda_field_windows: Optional[Dict[str, Tuple[int, int]]] = None
    portrait_enabled: bool = True
    overlay_icons: bool = True
    reference_icons: bool = True
    digit_templates_dir: Optional[Path] = None
    name: str = "default"
    source_path: Optional[Path] = None
    notes: str = ""

    @staticmethod
    def _coerce_region(key: str, value) -> tuple:
        return tuple(int(v) for v in value)

    @classmethod
    def default(cls) -> "DetectorProfile":
        regions = {k: cls._coerce_region(k, v) for k, v in load_regions().items()}
        return cls(
            regions=regions,
            group_mode="fields",
            kda_field_windows=None,
            portrait_enabled=True,
            overlay_icons=True,
            reference_icons=True,
            digit_templates_dir=None,
            name="default",
            source_path=None,
            notes=""
        )

    @classmethod
    def from_dict(cls, d: dict, *, name: str = "profile", source_path: Optional[Path] = None) -> "DetectorProfile":
        if not isinstance(d, dict):
            raise ProfileError("Input must be a dictionary")
            
        if "schema" in d and not isinstance(d["schema"], int):
            raise ProfileError("schema must be an integer")
            
        if "canvas" in d:
            if list(d["canvas"]) != [1920, 1080]:
                raise ProfileError("canvas must equal [1920, 1080]")
                
        profile_name = d.get("channel", name)
        
        raw_regions = d.get("regions", {})
        if not isinstance(raw_regions, dict):
            raise ProfileError("regions must be a dictionary")
            
        final_regions = {}
        for key in REGION_KEYS:
            if key in raw_regions:
                val = raw_regions[key]
                if isinstance(val, bool) or not isinstance(val, (list, tuple)):
                    raise ProfileError(f"region '{key}' must be a list/tuple of 4 ints")
                if len(val) != 4:
                    raise ProfileError(f"region '{key}' must have exactly 4 elements")
                if any(isinstance(v, bool) for v in val):
                    raise ProfileError(f"region '{key}' contains non-integer values")
                try:
                    iv = tuple(int(v) for v in val)
                except (TypeError, ValueError):
                    raise ProfileError(f"region '{key}' must contain integers")
                x1, y1, x2, y2 = iv
                if not (0 <= x1 < x2 <= 1920 and 0 <= y1 < y2 <= 1080):
                    raise ProfileError(f"region '{key}' coordinates out of bounds or invalid order")
                final_regions[key] = cls._coerce_region(key, val)
            else:
                final_regions[key] = tuple(DEFAULTS[key])
                
        for key in raw_regions:
            if key not in REGION_KEYS:
                raise ProfileError(f"Unknown region key '{key}'")
                
        group_mode = d.get("group_mode", "gaps")
        if group_mode not in GROUP_MODES:
            raise ProfileError("group_mode must be one of 'gaps' or 'fields'")
            
        kw = d.get("kda_field_windows")
        if kw is not None:
            if not isinstance(kw, dict):
                raise ProfileError("kda_field_windows must be a dict or null")
            if set(kw.keys()) != {"K", "D", "A"}:
                raise ProfileError("kda_field_windows must have keys exactly 'K', 'D', 'A'")
            for k, v in kw.items():
                if not isinstance(v, (list, tuple)) or len(v) != 2:
                    raise ProfileError(f"kda_field_windows['{k}'] must be a 2-int pair")
                if any(isinstance(x, bool) for x in v):
                    raise ProfileError(f"kda_field_windows['{k}'] contains non-integer values")
                try:
                    iv = tuple(int(x) for x in v)
                except (TypeError, ValueError):
                    raise ProfileError(f"kda_field_windows['{k}'] must contain integers")
                if iv[0] >= iv[1]:
                    raise ProfileError(f"kda_field_windows['{k}'] must be ascending")
            kw = {k: tuple(int(x) for x in v) for k, v in kw.items()}
            
        portrait_enabled = d.get("portrait_enabled", True)
        if not isinstance(portrait_enabled, bool):
            raise ProfileError("portrait_enabled must be a boolean")
        overlay_icons = d.get("overlay_icons", False)
        if not isinstance(overlay_icons, bool):
            raise ProfileError("overlay_icons must be a boolean")
        reference_icons = d.get("reference_icons", False)
        if not isinstance(reference_icons, bool):
            raise ProfileError("reference_icons must be a boolean")
            
        dt_dir = d.get("digit_templates_dir")
        if dt_dir is not None:
            if not isinstance(dt_dir, str):
                raise ProfileError("digit_templates_dir must be a string or null")
            p = Path(dt_dir)
            if not p.is_absolute() and source_path is not None:
                p = source_path.parent / p
            dt_dir = p
            
        notes = d.get("notes", "")
        if not isinstance(notes, str):
            raise ProfileError("notes must be a string")
            
        return cls(
            regions=final_regions,
            group_mode=group_mode,
            kda_field_windows=kw,
            portrait_enabled=portrait_enabled,
            overlay_icons=overlay_icons,
            reference_icons=reference_icons,
            digit_templates_dir=dt_dir,
            name=profile_name,
            source_path=source_path,
            notes=notes
        )

    @classmethod
    def load(cls, path: Path | str) -> "DetectorProfile":
        p = Path(path)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ProfileError(f"File not found: {p}")
        except json.JSONDecodeError as e:
            raise ProfileError(f"Invalid JSON: {e}")
            
        if not isinstance(data, dict):
            raise ProfileError("JSON root must be an object")
            
        name = p.parent.name if p.name == "profile.json" else p.stem
        return cls.from_dict(data, name=name, source_path=p)

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "channel": self.name,
            "canvas": list(CANVAS),
            "regions": {k: list(v) for k, v in self.regions.items()},
            "group_mode": self.group_mode,
            "kda_field_windows": {k: list(v) for k, v in self.kda_field_windows.items()} if self.kda_field_windows else None,
            "portrait_enabled": self.portrait_enabled,
            "overlay_icons": self.overlay_icons,
            "reference_icons": self.reference_icons,
            "digit_templates_dir": str(self.digit_templates_dir) if self.digit_templates_dir else None,
            "notes": self.notes
        }

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = p.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write("\n")
        os.replace(str(tmp_path), str(p))

    def crop_box(self) -> Tuple[int, int, int, int]:
        keys = list(CROP_KEYS)
        if self.portrait_enabled:
            keys.append("portrait")
        regions = [self.regions[k] for k in keys]
        return union_box(regions)

    def crop_origin(self) -> Tuple[int, int]:
        x, y, _, _ = self.crop_box()
        return (x, y)

    def region(self, key: str) -> Tuple[int, int, int, int]:
        return self.regions[key]

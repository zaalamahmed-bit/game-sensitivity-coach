"""Portable, deliberately nullable game settings and discovery context."""
import json
import math
from pathlib import Path


PROFILE_SCHEMA = "game-sensitivity/profile-v1"
KNOWN_SETTINGS = ("dpi", "look_sensitivity", "scoped_multiplier", "normal_zoom_multiplier")


def validate_setting_value(field, value, prefix="settings."):
    if not isinstance(field, str) or not field.strip():
        raise ValueError("Setting names must be nonempty strings")
    if value is None:
        return
    if field in KNOWN_SETTINGS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(prefix + field + " must be a finite positive number or null")
        if value <= 0 or (field == "dpi" and not isinstance(value, int)):
            raise ValueError(prefix + field + " must be positive; DPI must be an integer")
    elif not isinstance(value, (str, bool, int, float)):
        raise ValueError(prefix + field + " must be a JSON scalar or null")
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
        raise ValueError(prefix + field + " must be finite")


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object; unknown values may be null")
    for field, value in settings.items():
        validate_setting_value(field, value)
    return settings


def load_profile(path=None):
    if path is None:
        return {}
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        profile = json.load(stream)
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    if profile.get("schema", PROFILE_SCHEMA) != PROFILE_SCHEMA:
        raise ValueError("Unsupported profile schema; expected " + PROFILE_SCHEMA)
    json.dumps(profile, allow_nan=False)
    validate_settings(profile.get("settings", {}))
    for key in ("game", "window_title"):
        if key in profile and (not isinstance(profile[key], str) or not profile[key].strip()):
            raise ValueError("Profile " + key + " must be a nonempty string")
    for key in ("context", "setting_sources"):
        if key in profile and not isinstance(profile[key], dict):
            raise ValueError("Profile " + key + " must be an object")
    if any(not isinstance(value, dict) for value in profile.get("setting_sources", {}).values()):
        raise ValueError("Each setting_sources entry must be an object")
    if "evidence" in profile and not isinstance(profile["evidence"], list):
        raise ValueError("Profile evidence must be a list")
    for key in ("settings_source", "source"):
        if key in profile and not isinstance(profile[key], str):
            raise ValueError("Profile " + key + " must be text")
    return profile

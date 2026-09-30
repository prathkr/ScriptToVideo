import re

from typing import Dict, Iterable, List

PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def extract_placeholders(scripts: Iterable[str]) -> List[str]:
    """Return every distinct {placeholder} name used across all scripts, sorted."""
    placeholders = set()

    for script in scripts:
        if not script:
            continue
        placeholders.update(PLACEHOLDER_PATTERN.findall(script))

    return sorted(placeholders)


def replace_placeholders(text: str, values: Dict[str, str]) -> str:
    result = text
    for key, value in values.items():
        result = result.replace(f"{{{key}}}", value)
    return result


def missing_placeholders(text: str, values: Dict[str, str]) -> List[str]:
    """Which {placeholders} in this text still have no (non-empty) value?"""
    found = set(PLACEHOLDER_PATTERN.findall(text))
    return sorted(p for p in found if not values.get(p))

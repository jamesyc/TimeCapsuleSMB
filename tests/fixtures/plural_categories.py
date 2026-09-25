"""Generate the CLDR plural categories the macOS plural tests expect.

The Swift PluralLocalizationTests check that Foundation picks, for every app
language and boundary count, the category CLDR assigns. The categories come
from babel's CLDR data, so no plural rule is written by hand here or in Swift.

    python -m tests.fixtures.plural_categories --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from babel import Locale


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/plural_categories.json"
)

# App language code -> CLDR locale. The app's Portuguese is Brazilian.
LOCALES = {
    "en": "en",
    "de": "de",
    "nl": "nl",
    "fr": "fr",
    "es": "es",
    "it": "it",
    "pt": "pt_BR",
    "ru": "ru",
    "lt": "lt",
    "zh-Hans": "zh_Hans",
}

# Counts at every boundary between the ten languages' categories: 0 and 1, the
# last-digit rules of Russian and Lithuanian (2-4, 5-9, 11-19, 21, 101, 111)
# and the round-million rule of Spanish, Italian, French and Portuguese.
BOUNDARY_COUNTS = [
    0, 1, 2, 3, 4, 5, 9, 10, 11, 12, 14, 19, 20, 21, 22, 25,
    100, 101, 111, 112, 1_000, 1_000_000, 1_000_001, 2_000_000,
]

# Counts that reach every integer category of these languages: their rules
# depend only on the last two digits and on round millions.
INTEGER_COUNTS = [*range(200), 1_000, 1_000_000, 1_000_001, 2_000_000]


def plural_category(language: str, count: int) -> str:
    """The CLDR plural category of a non-negative integer count."""
    return Locale.parse(LOCALES[language]).plural_form(count)


def integer_categories(language: str) -> set[str]:
    """Every category an integer count can take in this language."""
    return {plural_category(language, count) for count in INTEGER_COUNTS}


def all_categories(language: str) -> set[str]:
    """Every category the language defines, including fraction-only ones."""
    return set(Locale.parse(LOCALES[language]).plural_form.tags) | {"other"}


def build() -> dict[str, object]:
    return {
        "counts": BOUNDARY_COUNTS,
        "categories": {
            language: {str(count): plural_category(language, count) for count in BOUNDARY_COUNTS}
            for language in LOCALES
        },
    }


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the fixture instead of checking it")
    args = parser.parse_args(argv)
    text = render()
    if args.write:
        FIXTURE_PATH.write_text(text)
        return 0
    if FIXTURE_PATH.read_text() != text:
        print(f"{FIXTURE_PATH} is stale; run python -m tests.fixtures.plural_categories --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

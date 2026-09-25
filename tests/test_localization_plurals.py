"""Checks the plural entries in the macOS app's Localizable.stringsdict files.

Foundation picks a plural form from the locale the app formats with
(verified on macOS 2026-09-25; see macos/LOCALIZATION_GLOSSARY.md). A form
Foundation can pick but the catalog lacks silently falls back to "other", and
a malformed entry can crash String(format:), so the structure is checked here
and the rendered sentences in the Swift PluralLocalizationTests.
"""
from __future__ import annotations

import plistlib
import re
import unittest

from tests.test_summaries import LANGUAGES, RESOURCES, STRING_LINE, catalog, placeholder_types


# The forms each language's plural entries must define: the CLDR categories
# its integer counts use, plus the "other" fallback Foundation requires.
# Lithuanian "many" only applies to fractions, so it is allowed but not
# required.
REQUIRED_FORMS = {
    "en": {"one", "other"},
    "de": {"one", "other"},
    "nl": {"one", "other"},
    "es": {"one", "many", "other"},
    "it": {"one", "many", "other"},
    "fr": {"one", "many", "other"},
    "pt": {"one", "many", "other"},
    "ru": {"one", "few", "many", "other"},
    "lt": {"one", "few", "other"},
    "zh-Hans": {"other"},
}
ALLOWED_FORMS = {**REQUIRED_FORMS, "lt": {"one", "few", "many", "other"}}
VARIABLE = re.compile(r"%(?:\d+\$)?#@(\w+)@")
SPECIFIER = re.compile(r"%(?:\d+\$)?(?:#@\w+@|l{0,2}[diu]|@)")


def plural_rule(language: str, count: int) -> str:
    """The CLDR category of a non-negative integer count."""
    last, last_two = count % 10, count % 100
    if language in ("en", "de", "nl"):
        return "one" if count == 1 else "other"
    if language in ("es", "it"):
        if count == 1:
            return "one"
        return "many" if count != 0 and count % 1_000_000 == 0 else "other"
    if language in ("fr", "pt"):
        if count in (0, 1):
            return "one"
        return "many" if count % 1_000_000 == 0 else "other"
    if language == "ru":
        if last == 1 and last_two != 11:
            return "one"
        if 2 <= last <= 4 and not 12 <= last_two <= 14:
            return "few"
        return "many"
    if language == "lt":
        if last == 1 and not 11 <= last_two <= 19:
            return "one"
        if 2 <= last <= 9 and not 11 <= last_two <= 19:
            return "few"
        return "other"
    return "other"


def stringsdict(language: str) -> dict[str, dict[str, object]]:
    with open(RESOURCES / f"{language}.lproj" / "Localizable.stringsdict", "rb") as handle:
        return plistlib.load(handle)


def strings_keys(language: str) -> set[str]:
    text = (RESOURCES / f"{language}.lproj" / "Localizable.strings").read_text()
    return {m.group(1) for line in text.splitlines() if (m := STRING_LINE.match(line))}


def variables(entry: dict[str, object]) -> dict[str, dict[str, str]]:
    return {name: value for name, value in entry.items() if isinstance(value, dict)}  # type: ignore[misc]


class PluralRuleTests(unittest.TestCase):
    """The expected categories, which the Swift tests check Foundation against."""

    EDGE_CASES = {
        "en": {0: "other", 1: "one", 2: "other", 11: "other", 21: "other", 1_000_000: "other"},
        "de": {0: "other", 1: "one", 2: "other", 101: "other"},
        "es": {0: "other", 1: "one", 2: "other", 1_000: "other", 1_000_000: "many", 2_000_000: "many"},
        "it": {0: "other", 1: "one", 2: "other", 1_000_000: "many"},
        "fr": {0: "one", 1: "one", 2: "other", 1_000: "other", 1_000_000: "many", 1_000_001: "other"},
        "pt": {0: "one", 1: "one", 2: "other", 1_000_000: "many"},
        "ru": {0: "many", 1: "one", 2: "few", 4: "few", 5: "many", 11: "many", 12: "many", 14: "many",
               21: "one", 22: "few", 25: "many", 101: "one", 111: "many", 112: "many", 1_000_000: "many"},
        "lt": {0: "other", 1: "one", 2: "few", 9: "few", 10: "other", 11: "other", 19: "other", 20: "other",
               21: "one", 22: "few", 101: "one", 111: "other", 1_000_000: "other"},
        "zh-Hans": {0: "other", 1: "other", 2: "other"},
    }

    def test_edge_counts_fall_in_the_expected_category(self) -> None:
        for language, cases in self.EDGE_CASES.items():
            for count, category in cases.items():
                with self.subTest(language=language, count=count):
                    self.assertEqual(plural_rule(language, count), category)

    def test_required_forms_are_the_integer_categories_plus_other(self) -> None:
        # Foundation needs "other" as the fallback even where no integer uses it (Russian).
        counts = [*range(300), 1_000_000, 2_000_000, 3_000_000]
        for language in LANGUAGES:
            with self.subTest(language=language):
                categories = {plural_rule(language, count) for count in counts}
                self.assertEqual(categories | {"other"}, REQUIRED_FORMS[language])


class PluralCatalogTests(unittest.TestCase):
    def test_every_language_has_the_same_plural_keys_as_english(self) -> None:
        english = set(stringsdict("en"))
        self.assertGreater(len(english), 0)
        for language in LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(set(stringsdict(language)), english)

    def test_plural_keys_are_not_also_plain_strings(self) -> None:
        # A key in both files would render from the plural entry, leaving a
        # stale plain translation nobody sees.
        for language in LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(set(stringsdict(language)) & strings_keys(language), set())

    def test_plural_formats_take_the_same_arguments_as_english(self) -> None:
        english = catalog("en")
        for language in LANGUAGES:
            localized = catalog(language)
            for key in stringsdict("en"):
                with self.subTest(language=language, key=key):
                    self.assertEqual(placeholder_types(localized[key]), placeholder_types(english[key]))

    def test_every_variable_is_a_declared_integer_plural_with_the_language_forms(self) -> None:
        for language in LANGUAGES:
            for key, entry in stringsdict(language).items():
                fmt = entry["NSStringLocalizedFormatKey"]
                assert isinstance(fmt, str)
                declared = variables(entry)
                with self.subTest(language=language, key=key):
                    self.assertEqual(set(VARIABLE.findall(fmt)), set(declared), "used and declared variables differ")
                for name, forms in declared.items():
                    with self.subTest(language=language, key=key, variable=name):
                        self.assertEqual(forms.get("NSStringFormatSpecTypeKey"), "NSStringPluralRuleType")
                        # The app passes Swift Int, which only %lld reads correctly.
                        self.assertEqual(forms.get("NSStringFormatValueTypeKey"), "lld")
                        categories = set(forms) - {"NSStringFormatSpecTypeKey", "NSStringFormatValueTypeKey"}
                        self.assertLessEqual(REQUIRED_FORMS[language], categories)
                        self.assertLessEqual(categories, ALLOWED_FORMS[language])

    def test_plural_forms_show_only_their_own_count(self) -> None:
        for language in LANGUAGES:
            for key, entry in stringsdict(language).items():
                for name, forms in variables(entry).items():
                    texts = {c: t for c, t in forms.items() if not c.startswith("NSString")}
                    with self.subTest(language=language, key=key, variable=name):
                        for category, text in texts.items():
                            self.assertTrue(text.strip(), category)
                            self.assertIn(SPECIFIER.findall(text), ([], ["%lld"]), f"{category}: {text}")
                        # Either every form shows the number or none does (a
                        # verb that only agrees with a count shown elsewhere).
                        self.assertEqual(len({"%lld" in text for text in texts.values()}), 1, texts)

    def test_no_translation_keeps_a_parenthesized_plural(self) -> None:
        pattern = re.compile(r"\((?:s|es|e|-?[a-zą-ž]{1,3})\)")
        for language in LANGUAGES:
            for key, text in catalog(language).items():
                with self.subTest(language=language, key=key):
                    self.assertIsNone(pattern.search(text), text)
            for key, entry in stringsdict(language).items():
                for forms in variables(entry).values():
                    for text in forms.values():
                        with self.subTest(language=language, key=key):
                            self.assertIsNone(pattern.search(text), text)


if __name__ == "__main__":
    unittest.main()

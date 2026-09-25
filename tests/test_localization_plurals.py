"""Checks the plural entries in the macOS app's Localizable.stringsdict files.

Foundation picks a plural form from the locale the app formats with
(verified on macOS 2026-09-25; see macos/LOCALIZATION_GLOSSARY.md). A form
Foundation can pick but the catalog lacks silently falls back to "other", and
a malformed entry can crash String(format:), so the structure is checked here
and the rendered sentences in the Swift PluralLocalizationTests. Plural
categories come from CLDR through babel; none is written by hand.
"""
from __future__ import annotations

import plistlib
import re
import unittest

from tests.fixtures import plural_categories
from tests.fixtures.plural_categories import all_categories, integer_categories
from tests.test_summaries import LANGUAGES, RESOURCES, STRING_LINE, catalog, placeholder_types


# Project choices on top of CLDR. CLDR puts Portuguese 0 in "one", but
# Brazilian usage says "0 dispositivos", and Foundation uses a zero form for
# exactly 0; see the glossary's Plurals section.
ZERO_FORM_LANGUAGES = {"pt"}
VARIABLE = re.compile(r"%(?:\d+\$)?#@(\w+)@")
SPECIFIER = re.compile(r"%(?:\d+\$)?(?:#@\w+@|l{0,2}[diu]|@)")
# A plural squeezed into parentheses right after a word: "device(s)",
# "problème(s)", "failo(-ų)", "устройств(а)". Not "(ssh)" after a space.
PARENTHESIZED_PLURAL = re.compile(r"[^\W\d_]\(-?[^\W\d_]{1,3}\)")


def required_forms(language: str) -> set[str]:
    """Every form a plural variable must define: each category an integer
    count can take, the "other" fallback Foundation requires, and the
    project's zero form where it applies."""
    zero = {"zero"} if language in ZERO_FORM_LANGUAGES else set()
    return integer_categories(language) | {"other"} | zero


def allowed_forms(language: str) -> set[str]:
    """Required forms plus categories only fractions use (Lithuanian many)."""
    return required_forms(language) | all_categories(language)


def stringsdict(language: str) -> dict[str, dict[str, object]]:
    with open(RESOURCES / f"{language}.lproj" / "Localizable.stringsdict", "rb") as handle:
        return plistlib.load(handle)


def strings_keys(language: str) -> set[str]:
    text = (RESOURCES / f"{language}.lproj" / "Localizable.strings").read_text()
    return {m.group(1) for line in text.splitlines() if (m := STRING_LINE.match(line))}


def variables(entry: dict[str, object]) -> dict[str, dict[str, str]]:
    return {name: value for name, value in entry.items() if isinstance(value, dict)}  # type: ignore[misc]


class PluralCategoryFixtureTests(unittest.TestCase):
    def test_fixture_matches_babel(self) -> None:
        self.assertEqual(plural_categories.FIXTURE_PATH.read_text(), plural_categories.render())

    def test_fixture_covers_every_app_language(self) -> None:
        self.assertEqual(set(plural_categories.LOCALES), set(LANGUAGES))

    def test_boundary_counts_reach_every_integer_category(self) -> None:
        # The Swift sweep renders only the boundary counts, so they must hit
        # every form a translator writes.
        for language in LANGUAGES:
            with self.subTest(language=language):
                reached = {plural_categories.plural_category(language, n) for n in plural_categories.BOUNDARY_COUNTS}
                self.assertEqual(reached, integer_categories(language))


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
                        self.assertLessEqual(required_forms(language), categories)
                        self.assertLessEqual(categories, allowed_forms(language))

    def test_portuguese_zero_uses_the_plural_wording(self) -> None:
        for key, entry in stringsdict("pt").items():
            for name, forms in variables(entry).items():
                with self.subTest(key=key, variable=name):
                    self.assertEqual(forms["zero"], forms["other"])

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

    def test_parenthesized_plural_pattern(self) -> None:
        for text in ("device(s)", "problème(s)", "failo(-ų)", "устройств(а)", "tomų(-ų)"):
            with self.subTest(text=text):
                self.assertIsNotNone(PARENTHESIZED_PLURAL.search(text))
        for text in ("Connect over SSH (ssh)", "version (7.8.1)", "(s)"):
            with self.subTest(text=text):
                self.assertIsNone(PARENTHESIZED_PLURAL.search(text))

    def test_no_translation_keeps_a_parenthesized_plural(self) -> None:
        for language in LANGUAGES:
            for key, text in catalog(language).items():
                with self.subTest(language=language, key=key):
                    self.assertIsNone(PARENTHESIZED_PLURAL.search(text), text)
            for key, entry in stringsdict(language).items():
                for forms in variables(entry).values():
                    for text in forms.values():
                        with self.subTest(language=language, key=key):
                            self.assertIsNone(PARENTHESIZED_PLURAL.search(text), text)


if __name__ == "__main__":
    unittest.main()

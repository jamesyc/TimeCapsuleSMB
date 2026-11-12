"""The mechanical rules of macos/LOCALIZATION_GLOSSARY.md, checked in every
catalog: punctuation, quotation marks, form of address and the labels a
sentence tells the user to use."""

from __future__ import annotations

import ast
import plistlib
import re
import unittest
from pathlib import Path

from timecapsulesmb.app.confirmations import DISK_FAILING_PRESENTATION_IDS
from tests.test_summaries import LANGUAGES, RESOURCES, STRING_LINE


def catalog_texts(language: str) -> list[tuple[str, str]]:
    """Every text a language shows: each Localizable.strings value and every
    form of every plural entry."""
    path = RESOURCES / f"{language}.lproj" / "Localizable.strings"
    texts = [(m.group(1), m.group(2)) for line in path.read_text().splitlines() if (m := STRING_LINE.match(line))]
    with open(RESOURCES / f"{language}.lproj" / "Localizable.stringsdict", "rb") as handle:
        for key, entry in plistlib.load(handle).items():
            for name, forms in entry.items():
                for form, text in forms.items() if isinstance(forms, dict) else [("format", forms)]:
                    if isinstance(text, str) and not form.startswith("NSString"):
                        texts.append((f"{key} {name} {form}", text))
    return texts


class LocalizationStyleTests(unittest.TestCase):
    def test_each_language_quotes_with_its_own_marks_only(self) -> None:
        # A straight " is a leftover; each language has one kind of curly mark.
        marks = set("“”„‘’«»")
        own = {"en": "“”", "it": "“”’", "pt": "“”", "zh-Hans": "“”", "nl": "‘’", "de": "„“", "lt": "„“",
               "es": "«»", "ru": "«»", "fr": "«»’"}
        for language in LANGUAGES:
            foreign = marks - set(own[language])
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertNotIn('\\"', text)
                    self.assertEqual(set(text) & foreign, set(), text)

    def test_french_and_italian_write_typographic_apostrophes(self) -> None:
        straight = re.compile(r"(?<=[^\W\d_])'(?=[^\W\d_])")
        for language in ("fr", "it"):
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertIsNone(straight.search(text), text)

    def test_english_and_dutch_write_straight_apostrophes(self) -> None:
        curly = re.compile(r"(?<=[^\W\d_])’(?=[^\W\d_])")
        for language in ("en", "nl"):
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertIsNone(curly.search(text), text)

    def test_french_uses_a_non_breaking_space_before_double_punctuation(self) -> None:
        # A plain space lets "?", "!", ":", ";" or a closing "»" wrap onto a
        # line of its own, or leaves an opening "«" alone at a line's end.
        plain = re.compile(r" [?!:;»]|« ")
        for key, text in catalog_texts("fr"):
            with self.subTest(key=key):
                self.assertIsNone(plain.search(text), text)

    def test_ellipses_are_three_periods_except_in_chinese(self) -> None:
        for language in LANGUAGES:
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    if language != "zh-Hans":
                        self.assertNotIn("…", text)
                        continue
                    # A Chinese line still in progress, or a label that opens a
                    # dialog, ends with Apple's single …, as Apple's own status
                    # lines do; …… marks text left out and never ends a line.
                    # Each line of a value counts on its own.
                    self.assertNotIn("...", text)
                    self.assertNotIn("………", text)
                    for line in re.split(r"\\n|\n", text):
                        self.assertFalse(line.endswith("……"), text)
                        single = line.replace("……", "")
                        if "…" in single:
                            self.assertTrue(single.endswith("…") and single.count("…") == 1 and not single.endswith("。…"), text)

    def test_netbsd_releases_have_a_space_before_their_number(self) -> None:
        # As netbsd.org writes them: NetBSD 4, never NetBSD4.
        for language in LANGUAGES:
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertIsNone(re.search(r"NetBSD\d", text), text)

    def test_german_hyphenates_netbsd_compounds(self) -> None:
        # NetBSD-4-Gerät, as Duden hyphenates Windows-10-Gerät: never
        # NetBSD 4-Gerät, NetBSD 4 Gerät or NetBSD-4 Gerät. A name that
        # follows is no compound (auf NetBSD 4 Samba starten).
        for key, text in catalog_texts("de"):
            with self.subTest(key=key):
                self.assertNotRegex(text, r"NetBSD \d-|NetBSD \d (?!Samba|Bonjour|Netatalk|AirPort)[A-ZÄÖÜ][a-zäöüß]|NetBSD-\d(?!-)")

    def test_dutch_joins_netbsd_compounds_with_a_hyphen(self) -> None:
        # NetBSD 4-apparaten, never NetBSD 4 apparaten. Dutch nouns are
        # lowercase like the words around them, so the nouns are listed.
        nouns = r"apparaat|apparaten|activering|herstart|opstart|opstart-hook|services|software"
        for key, text in catalog_texts("nl"):
            with self.subTest(key=key):
                self.assertNotRegex(text, rf"NetBSD \d ({nouns})\b")

    def test_only_german_hyphenates_netbsd(self) -> None:
        for language in LANGUAGES:
            if language == "de":
                continue
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertNotRegex(text, r"NetBSD-\d")

    def test_helper_strings_have_a_space_before_the_netbsd_release(self) -> None:
        # The helper's English reaches the CLI and, untranslated, the app. Its
        # shell scripts embedded in Python strings count too; Python comments and
        # docstrings do not, nor the files under assets/.
        for path in sorted((Path(__file__).resolve().parents[1] / "src" / "timecapsulesmb").rglob("*.py")):
            tree = ast.parse(path.read_text())
            docstrings = {
                id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertNotRegex(node.value, r"NetBSD\d")

    def test_chinese_uses_full_width_punctuation(self) -> None:
        half_width = re.compile(r"[()]|[一-鿿][,;:!?]")
        for key, text in catalog_texts("zh-Hans"):
            with self.subTest(key=key):
                self.assertIsNone(half_width.search(text), text)

    def test_russian_writes_ye_never_yo(self) -> None:
        for key, text in catalog_texts("ru"):
            with self.subTest(key=key):
                self.assertNotRegex(text, "[ёЁ]")

    def test_spanish_mac_is_masculine(self) -> None:
        for key, text in catalog_texts("es"):
            with self.subTest(key=key):
                self.assertNotRegex(text, r"\b(esta|la|una|otra|nueva) Mac\b")

    def test_lithuanian_keeps_apple_names_unquoted(self) -> None:
        for key, text in catalog_texts("lt"):
            for name in ("„Apple“", "„Mac“"):
                with self.subTest(key=key, name=name):
                    self.assertNotIn(name, text)

    def test_each_language_addresses_the_user_one_way(self) -> None:
        # de Sie, nl u, fr vous, es usted, it tu, pt você, ru вы, zh 你, lt jūs:
        # neither the other form's pronouns nor its commands appear.
        other_form = {
            "de": r"\b(du|dein|deine|deinen|deinem|deiner|deines|dich|dir)\b"
                  r"|(?<![\w-])(Prüfe|Führe|Starte|Öffne|Wähle|Klicke|Trenne|Verbinde|Lösche|prüfe|führe|starte|öffne|wähle"
                  r"|klicke|versuche es)(?![\w-])",
            "nl": r"\b(je|jij|jou|jouw)\b",
            "fr": r"\b(tu|toi|ton|tes)\b|(^|[.;:] )(Vérifie|Ouvre|Attends|Réessaie|Choisis|Sélectionne|Débranche|Consulte|Patiente) ",
            # A bare third person reads as a tú command too, so an operation
            # that restarts the device is named as the subject.
            "es": r"\b(tú|tu|tus|ti)\b|(?<!\w)(Inténtalo|inténtalo|Asegúrate|asegúrate|Revisa|revisa|vuelve a intentarlo"
                  r"|espera unos|Espera unos)(?!\w)|(^|[.;:] |\(o )(Reinicia|reinicia|Ejecuta|Abre|Elige|Selecciona|Habilita) ",
            "it": r"\b(Lei|Suo|Sua|Suoi|Sue)\b|(?<!\w)(Esegua|Verifichi|Apra|Attenda|Riprovi|Selezioni|Scelga|Inserisca"
                  r"|Colleghi|Riavvii|Consulti)(?!\w)",
            "pt": r"\b(tu|teu|teus|tua|tuas)\b|(^|[.;:] )(Verifica|Executa|Abre|Tenta|Escolhe|Seleciona|Reinicia) ",
            "ru": r"(?<!\w)(ты|тебя|тебе|твой|твоя|твои|твое|Проверь|Запусти|Открой|Подожди|Выбери|Нажми|Отключи|Подключи"
                  r"|Повтори|Перезагрузи|Убедись|Попробуй)(?!\w)",
            "zh-Hans": "您",
            "lt": r"\b(tu|tavo|tau|tave)\b|(?<!\w)(Paleisk|Patikrink|Atidaryk|Palauk|Pasirink|Spustelėk|Atjunk|Prijunk"
                  r"|Bandyk)(?!\w)",
        }
        for language, pattern in other_form.items():
            for key, text in catalog_texts(language):
                with self.subTest(language=language, key=key):
                    self.assertIsNone(re.search(pattern, text), text)

    def test_forget_never_uses_the_uninstall_verb(self) -> None:
        # Forget deletes the saved device from this Mac; Uninstall deletes
        # TimeCapsuleSMB from the device. The question Forget asks and its
        # menu item must not read as uninstalling.
        for language in LANGUAGES:
            strings = dict(catalog_texts(language))
            uninstall = strings["recovery.action.uninstall"].lower()
            question = re.split(r"[?？]", strings["dialog.forget.message"])[0]
            for text in (strings["sidebar.menu.remove_from_this_mac"], question):
                with self.subTest(language=language, text=text):
                    self.assertNotIn(uninstall, text.lower())

    def test_english_quotes_the_labels_it_tells_the_user_to_use(self) -> None:
        strings = dict(catalog_texts("en"))
        label_key = re.compile(r"^(toolbar|dashboard\.(tab|action)|maintenance\.(workflow|action)|recovery\.action|flash\.action)\."
                               r"|^(diagnostics\.copy|profile_editor\.save|ssh_access_notice\.enable_action)$")
        labels = {text for key, text in strings.items() if label_key.match(key) and "%" not in text}
        labels |= {"System Settings", "Privacy & Security", "Local Network"}
        names = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        unquoted = re.compile(rf"\b(?:[Rr]un|[Oo]pen|[Cc]hoose|[Cc]lick|[Uu]se|[Ss]elect|in)\s+(?:{names})\b")
        for key, text in strings.items():
            # Titles and buttons name their own action; only sentences count.
            if label_key.match(key) or key.endswith((".title", ".action")) or not re.search(r"[.!?]$", text):
                continue
            with self.subTest(key=key):
                self.assertIsNone(unquoted.search(text), text)

    def test_english_errors_and_recovery_name_operations_not_cli_commands(self) -> None:
        # "deploy", "doctor" and "configure" are CLI words; the app's
        # operations are Install / Update Samba, Checkup and adding a device.
        # A literal command (`tcapsule deploy`, `activate`) stays.
        cli_word = re.compile(r"(?<![\w-])(?<!tcapsule )(deploy|doctor|configure)(?![\w-])", re.I)
        for key, text in catalog_texts("en"):
            if not key.startswith("backend.recovery."):
                continue
            with self.subTest(key=key):
                self.assertIsNone(cli_word.search(re.sub(r"`[^`]*`", "", text)), text)


class FailingDiskConfirmationTests(unittest.TestCase):
    """Every failing-disk confirmation the helper can send has its own
    translation in every language: a missing one would show the helper's
    English, and one that names Wipe Internal Disk where the helper did not
    would tell the user something else."""

    def test_every_language_has_each_confirmation_in_its_own_words(self) -> None:
        english = dict(catalog_texts("en"))
        for language in LANGUAGES:
            texts = dict(catalog_texts(language))
            wipe_label = texts["maintenance.action.wipe_disk"]
            titles = set()
            for presentation_id in DISK_FAILING_PRESENTATION_IDS:
                with self.subTest(language=language, presentation_id=presentation_id):
                    key = f"confirm.{presentation_id}"
                    for part in ("title", "message", "action"):
                        self.assertTrue(texts.get(f"{key}.{part}"), f"{key}.{part}")
                    message = texts[f"{key}.message"]
                    if language != "en":
                        self.assertNotEqual(message, english[f"{key}.message"])
                    # Only the _wipe wording suggests Wipe Internal Disk, by
                    # the language's own label.
                    self.assertEqual(wipe_label in message, presentation_id.endswith("_wipe"))
                    titles.add(texts[f"{key}.title"])
            with self.subTest(language=language, check="one title"):
                self.assertEqual(len(titles), 1)



if __name__ == "__main__":
    unittest.main()

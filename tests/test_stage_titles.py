"""Every step the helper reports has a title in every catalog.

The app finds a step's title as timeline.<operation>.<stage>.title, else
timeline.stage.<stage>.title, shared by every operation with that stage
(OperationTimelineBuilder.stageText); its detail the same way. Saved install
states keep only the stage's name, so the key cannot come from the event.
"""
from __future__ import annotations

import re
import unittest

from tests.test_summaries import LANGUAGES, catalog
from timecapsulesmb.app import stage_policy

STAGE_KEY = re.compile(r"^timeline\.(?P<operation>[a-z-]+)\.(?P<stage>[a-z_0-9]+)\.(?P<part>title|detail)$")


def stage_text(strings: dict[str, str], operation: str, stage: str, part: str) -> str | None:
    return strings.get(f"timeline.{operation}.{stage}.{part}") or strings.get(f"timeline.stage.{stage}.{part}")


class StageTitleTests(unittest.TestCase):
    def test_every_stage_has_a_title_in_every_language(self) -> None:
        for language in LANGUAGES:
            strings = catalog(language)
            for operation, stage in stage_policy._POLICIES:
                with self.subTest(language=language, operation=operation, stage=stage):
                    self.assertTrue(stage_text(strings, operation, stage, "title"))

    def test_every_stage_key_names_a_stage_the_helper_reports(self) -> None:
        operations = {operation for operation, _ in stage_policy._POLICIES}
        stages = {stage for _, stage in stage_policy._POLICIES}
        for key in catalog("en"):
            match = STAGE_KEY.match(key)
            if not match or key.startswith("timeline.fsck.phase."):
                continue
            with self.subTest(key=key):
                self.assertIn(match["stage"], stages)
                if match["operation"] != "stage":
                    self.assertIn(match["operation"], operations)
                    self.assertIn((match["operation"], match["stage"]), stage_policy._POLICIES)

    def test_only_operations_that_start_file_sharing_after_a_reboot_report_it(self) -> None:
        # Its shared title says file sharing starts again: the other rebooting
        # operations leave it as the reboot left it.
        for stage in ("post_reboot_activation", "verify_runtime_activation"):
            with self.subTest(stage=stage):
                operations = {operation for operation, other in stage_policy._POLICIES if other == stage}
                self.assertLessEqual(operations, {"deploy", "fsck", "activate"})

    def test_an_operation_s_own_title_wins_over_the_shared_one(self) -> None:
        english = catalog("en")
        # Disk Repair finds volumes where Install / Update reads the disk list.
        self.assertNotEqual(
            stage_text(english, "fsck", "read_mast", "title"), stage_text(english, "deploy", "read_mast", "title"),
        )
        # Every rebooting operation shares the reboot steps.
        reboots = {stage_text(english, operation, "reboot", "title") for operation, stage in stage_policy._POLICIES
                   if stage == "reboot"}
        self.assertEqual(len(reboots), 1)


if __name__ == "__main__":
    unittest.main()

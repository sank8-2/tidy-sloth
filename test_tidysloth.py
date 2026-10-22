#!/usr/bin/env python3
"""Tests for the parts of Tidy Sloth that must never be wrong.

Run: python3 -m unittest -v test_tidysloth
"""
import unittest

from tidysloth import CATEGORIES, clean_stem, rule_verdict, safe_model_name


class TestProvenanceGuard(unittest.TestCase):
    """A model may reorganise a filename. It may never import another row's."""

    def test_rejects_names_from_other_rows(self):
        cases = [
            ("AC2", "1786076876258_SoulTree-Brochure-1 (1)"),
            ("Cost-Breakdown-6a61", "1786076876258_SoulTree-Brochure-1"),
            ("20260618-144350", "1001945305-FF-AH-Retail-Price-List-Feb-2026"),
            ("Aadhar-Card", "1748950125346_out-0"),
            ("Copy-of-2024-07-TBC-Banner", "1764919659917_floop-plan-4"),
        ]
        for proposed, source in cases:
            with self.subTest(proposed=proposed):
                self.assertEqual(safe_model_name(proposed, source), "")

    def test_accepts_respacing_without_separators(self):
        self.assertEqual(
            safe_model_name("soulace-new-2", "1787045719037_SoulaceNew2"),
            "soulace-new-2")

    def test_accepts_same_words(self):
        self.assertEqual(
            safe_model_name("SoulTree-Brochure-1",
                            "1786076876258_SoulTree-Brochure-1 (1)"),
            "SoulTree-Brochure-1")
        self.assertEqual(safe_model_name("Employee-1", "Employee (1)"),
                         "Employee-1")
        self.assertEqual(safe_model_name("invoice-march", "Invoice.pdf"),
                         "invoice-march")

    def test_strips_echoed_extension(self):
        self.assertEqual(safe_model_name("Invoice.pdf", "Invoice"), "Invoice")
        self.assertNotIn(".pdf", safe_model_name("thing.pdf.pdf", "thing"))

    def test_never_invents_meaning_for_noise(self):
        # "01" carries no semantics, so no rename may be invented for it.
        self.assertEqual(safe_model_name("project-proposal", "01"), "")
        self.assertEqual(safe_model_name("aadhar-card", "1748950125346_out-0"), "")


class TestRules(unittest.TestCase):

    def f(self, name, ext):
        return {"name": name, "stem": name.rsplit(".", 1)[0], "ext": ext}

    def test_purpose_beats_extension(self):
        cat, _, confident = rule_verdict(self.f("Invoice-March.pdf", "pdf"))
        self.assertEqual(cat, "Invoices")
        self.assertTrue(confident)

    def test_resume(self):
        cat, _, _ = rule_verdict(self.f("Resume-Sanketh-Kumar.pdf", "pdf"))
        self.assertEqual(cat, "Resumes")

    def test_meaningful_name_needs_no_model(self):
        cat, name, confident = rule_verdict(self.f("carprices.csv", "csv"))
        self.assertEqual(cat, "Data")
        self.assertTrue(confident)
        self.assertEqual(name, "carprices")

    def test_noise_goes_to_model(self):
        _, _, confident = rule_verdict(self.f("img.png", "png"))
        self.assertFalse(confident)
        _, _, confident = rule_verdict(self.f("1762857107887_floop.png", "png"))
        self.assertFalse(confident)

    def test_config_extensions(self):
        cat, _, _ = rule_verdict(self.f("housekraft.pem", "pem"))
        self.assertEqual(cat, "Config")
        cat, _, _ = rule_verdict(self.f("env.local", "local"))
        self.assertEqual(cat, "Config")

    def test_unknown_extension_falls_back(self):
        cat, _, _ = rule_verdict(self.f("mystery.zzz", "zzz"))
        self.assertEqual(cat, "Other")


class TestCleanStem(unittest.TestCase):

    def test_spaces_and_underscores_become_hyphens(self):
        self.assertEqual(clean_stem("My  File_name (1)"), "My-File-name-1")

    def test_length_cap(self):
        self.assertLessEqual(len(clean_stem("x" * 200)), 60)

    def test_strips_leading_message_id(self):
        self.assertEqual(clean_stem("1786076876258_SoulTree-Brochure-1"),
                         "SoulTree-Brochure-1")
        self.assertEqual(clean_stem("1762857107887_floop-plan-4"),
                         "floop-plan-4")

    def test_keeps_pure_digit_name(self):
        # stripping the "id" here would destroy the only name it has
        self.assertEqual(clean_stem("4446563839"), "4446563839")


if __name__ == "__main__":
    unittest.main()

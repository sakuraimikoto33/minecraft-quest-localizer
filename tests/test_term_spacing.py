from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry
from mq_localizer.protection import TokenProtector
from mq_localizer.translator import TranslationOptions, TranslationService, _existing_translation_is_safe


class TermSpacingTests(unittest.TestCase):
    def test_only_source_word_boundaries_are_restored_with_original_spacing(self) -> None:
        for opening, closing in [("", ""), ("&e", "&f"), ("§a§l", "§r"), ("&#55FFFF", "&r")]:
            source = f"The  {opening}Tempad{closing} mod contains information."
            protected = TokenProtector().protect(source, {"Tempad": "Tempad"})
            candidate = protected.protected.replace("The  ", "THE").replace(" mod ", "MOD ")
            with self.subTest(source=source):
                with self.assertRaisesRegex(TranslationError, "文字が連結"):
                    protected.restore(candidate)
                repaired = protected.restore_source_term_spacing(candidate)
                self.assertEqual(protected.restore(repaired), source.replace("The", "THE").replace("mod", "MOD"))
                self.assertEqual(protected.restore_source_term_spacing(repaired), repaired)

    def test_unknown_affixes_technical_neighbors_and_missing_tokens_are_not_repaired(self) -> None:
        protected = TokenProtector().protect("Use &eTempad&r mod.", {"Tempad": "Tempad"})
        for ending in ("Module.", "SuperMod.", "Mods.", "mod_name.", "mod{new}.", "mod:evil."):
            candidate = protected.protected.replace(" mod.", ending)
            with self.subTest(ending=ending):
                with self.assertRaises(TranslationError):
                    protected.restore(protected.restore_source_term_spacing(candidate))
        for source, terms in [
            ("Tempad {mod}.", {"Tempad": "Tempad"}),
            ("Tempad\nmod.", {"Tempad": "Tempad"}),
        ]:
            protected = TokenProtector().protect(source, terms)
            candidate = protected.protected.replace(" ", "")
            self.assertEqual(protected.restore_source_term_spacing(candidate), candidate)
        candidate = "Mod__MQP_FFFF__"
        self.assertEqual(protected.restore_source_term_spacing(candidate), candidate)

    def test_japanese_particles_and_source_attached_ascii_are_unchanged(self) -> None:
        for source, candidate_word in [("Tempad mod.", "の説明。"), ("A&aTempad&r mod.", "の説明。")]:
            protected = TokenProtector().protect(source, {"Tempad": "Tempad"})
            candidate = protected.protected.replace(" mod.", candidate_word)
            self.assertEqual(protected.restore_source_term_spacing(candidate), candidate)
            protected.restore(candidate)

    def test_latest_log_sources_translate_without_retry_and_remain_reusable(self) -> None:
        cases = [
            ("The &6Knowledge Projector&r contains all information on the &eTempad&r mod.",
             ["Knowledge Projector", "Tempad"]),
            ("The &eDyson Cube Project&f mod allows you to build your own Dyson Sphere "
             "megastructure in Minecraft and harvest the power of the sun, producing up to 1 BILLION FE/t!",
             ["Dyson Cube Project"]),
        ]
        for source, terms in cases:
            def translate(item):
                tokens = re.findall(r"__MQP_[0-9A-F]{4}__", item["text"])
                if len(tokens) == 2:
                    return tokens[1] + "MODの情報は" + tokens[0] + "にあります。"
                return tokens[0] + "Modでダイソン球を建設し、毎tick最大10億FEを発電できます。"

            client = RecordingClient(translate)
            adapter = RecordingAdapter()
            glossary = GlossaryCatalog().with_source_preserved_terms(terms)
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                project = project_for(source, Path(directory))
                outcome = TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model", glossary, TranslationOptions(),
                )
                self.assertEqual(outcome.translated, 1)
                self.assertEqual(len(client.calls), 1)
                candidate = adapter.translations[0]["u"]
                self.assertTrue(_existing_translation_is_safe(source, candidate, glossary))
                self.assertIn("&r MOD" if len(terms) == 2 else "&f Mod", candidate)

    def test_logged_styled_term_boundaries_are_repaired_after_japanese_expansion(self) -> None:
        cases = [
            (
                "The &aLapis Talisman&r attracts XP orbs.",
                ["Lapis Talisman"],
                lambda text, token: text.replace("The " + token, "The a" + token),
                "The a &aLapis Talisman&r attracts XP orbs.",
            ),
            (
                "Get more &6Blaze Powder&r per &cBlaze Rod&r.",
                ["Blaze Powder", "Blaze Rod"],
                lambda text, token: text.replace(token + " per", token + "per"),
                "Get more &6ブレイズパウダー&r per &cブレイズロッド&r.",
            ),
        ]
        for source, terms, mutate, expected in cases:
            def translate(item):
                tokens = re.findall(r"__MQP_[0-9A-F]{4}__", item["text"])
                return mutate(item["text"], tokens[0])

            client = RecordingClient(translate)
            adapter = RecordingAdapter()
            targets = {
                "Lapis Talisman": "Lapis Talisman",
                "Blaze Powder": "ブレイズパウダー",
                "Blaze Rod": "ブレイズロッド",
            }
            glossary = GlossaryCatalog(entries={
                term: GlossaryEntry(
                    source=term, target=targets[term], key=f"test.{term}",
                    mod_id="test", translated=targets[term] != term,
                    provenance="test",
                )
                for term in terms
            })
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                project = project_for(source, Path(directory))
                outcome = TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model", glossary, TranslationOptions(),
                )
                self.assertEqual(outcome.translated, 1)
                self.assertEqual(adapter.translations[0]["u"], expected)

    def test_unknown_attachment_still_retries_and_never_writes(self) -> None:
        client = RecordingClient(lambda item: item["text"].replace(" mod", "SuperMod"))
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for("The &eTempad&r mod contains information.", Path(directory))
            with self.assertRaisesRegex(TranslationError, "Tempad.*直後"):
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model",
                    GlossaryCatalog().with_source_preserved_terms(["Tempad"]), TranslationOptions(),
                )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(adapter.translations, [])

    def test_function_word_attachment_is_separated_but_unknown_word_is_rejected(self) -> None:
        protected = TokenProtector().protect("The &aLapis Talisman&r attracts XP.", {"Lapis Talisman": "Lapis Talisman"})
        token = next(token for token, value in protected.replacements.items() if value == "Lapis Talisman")
        for attached, should_repair in (("a" + token, True), (token + "per", True), ("Super" + token, False)):
            candidate = protected.protected.replace(token, attached)
            with self.subTest(attached=attached):
                repaired = protected.restore_source_term_spacing(candidate)
                if not should_repair:
                    self.assertEqual(repaired, candidate)
                    with self.assertRaises(TranslationError):
                        protected.restore(repaired)
                else:
                    expected = candidate.replace("a" + token, "a " + token)
                    expected = expected.replace(token + "per", token + " per")
                    self.assertEqual(repaired, expected)

    def test_spacing_inside_styled_child_survives_parent_token_aliases(self) -> None:
        source = "Use &eTempad mod&r documentation."
        client = RecordingClient(lambda item: item["text"].replace(" mod", "MOD"))
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                GlossaryCatalog().with_source_preserved_terms(["Tempad"]), TranslationOptions(),
            )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.translations[0]["u"], "Use &eTempad MOD&r documentation.")

    def test_spacing_does_not_repair_missing_or_duplicate_formatting(self) -> None:
        protected = TokenProtector().protect("Use &eTempad&r mod.", {"Tempad": "Tempad"})
        reset = next(token for token, value in protected.replacements.items() if value == "&r")
        for replacement in ("", reset + reset):
            candidate = protected.protected.replace(reset, replacement).replace(" mod", "MOD")
            self.assertEqual(protected.restore_source_term_spacing(candidate), candidate)
            with self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_source_quantities_can_follow_the_same_name_in_japanese(self) -> None:
        cases = [
            ("Insert 20 Honey Treats, a baby bee, and power.", "Honey Treats", "20", "20"),
            ("This means one &bQuarry Addon&r provides an 8-block range.", "Quarry Addon", "one", "1"),
            ("Use two &bQuarry Addons&r to cover 64 blocks.", "Quarry Addons", "two", "2"),
        ]
        for source, term, source_count, digits in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                def translate(item):
                    tokens = re.findall(r"__MQP_[0-9A-F]{4}__", item["text"])
                    return tokens[0] + digits + "個を使います。"
                client = RecordingClient(translate)
                adapter = RecordingAdapter()
                project = project_for(source, Path(directory))
                glossary = GlossaryCatalog().with_source_preserved_terms([term])
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model", glossary, TranslationOptions(),
                )
                candidate = adapter.translations[0]["u"]
                self.assertIn(" " + digits + "個", candidate)
                self.assertIn(term, candidate)
                self.assertEqual(len(client.calls), 1)
                self.assertTrue(_existing_translation_is_safe(source, candidate, glossary))
                # A quantity from elsewhere in the sentence cannot justify a
                # new attachment to this particular occurrence.
                damaged = TokenProtector().protect(source, {term: term})
                token = damaged.term_placeholders[0]
                response = damaged.protected.replace(source_count + " ", "", 1)
                response = response.replace(token, token + ("21" if digits == "20" else "64"))
                with self.assertRaises(TranslationError):
                    damaged.restore(damaged.restore_source_term_spacing(response))

    def test_adjacent_independent_names_are_separated_without_splitting_one_name(self) -> None:
        from mq_localizer.protection import TermReplacement
        source = "Read &bReactor Components&r and &2Reactor Mechanics&r quests."
        terms = {term: term for term in ("Reactor Components", "Reactor Mechanics")}
        protected = TokenProtector().protect(source, terms)
        candidate = protected.protected.replace(" and ", "")
        restored = protected.restore(protected.restore_source_term_spacing(candidate))
        self.assertIn("&2 Reactor Mechanics", restored)
        self.assertTrue(_existing_translation_is_safe(source, restored, GlossaryCatalog().with_source_preserved_terms(terms)))
        compound = TokenProtector().protect("Copper Gear", term_spans=[
            TermReplacement(0, 6, "Copper", 0), TermReplacement(7, 11, "Gear", 0),
        ])
        joined = compound.protected.replace(" ", "")
        self.assertEqual(compound.restore_source_term_spacing(joined), joined)

    def test_spacing_never_uses_words_or_counts_across_technical_boundaries(self) -> None:
        for separator in ("\n", r"\n", "\t", "{count}", "https://example.com"):
            source = "Use 20" + separator + " Honey Treats."
            protected = TokenProtector().protect(source, {"Honey Treats": "Honey Treats"})
            token = protected.term_placeholders[0]
            candidate = protected.protected.replace(token, token + "20")
            self.assertEqual(protected.restore_source_term_spacing(candidate), candidate)
            with self.assertRaises(TranslationError):
                protected.restore(candidate)


if __name__ == "__main__":
    unittest.main()

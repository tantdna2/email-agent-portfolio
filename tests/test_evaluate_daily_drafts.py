from contextlib import redirect_stderr, redirect_stdout
import csv
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate_daily_drafts as evaluation


MODEL = "example/same-model"
DATASET_SHA256 = "6cee8ad64d7e5f451fdec80dffe750f3f97017652b376e583eddb5beecd2795e"


def response(draft, model="same-model"):
    return json.dumps({
        "status": "ok",
        "result": {
            "meta": {"agentMeta": {"provider": "example", "model": model}},
            "payloads": [{"text": draft}],
        },
    })


class DraftEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = evaluation.load_cases(evaluation.DATASET.read_bytes())

    def test_dataset_is_balanced_and_rubric_stays_out_of_email(self):
        test_cases = [case for case in self.cases if case["split"] == "test"]
        self.assertEqual(len(test_cases), 30)
        self.assertEqual(sum(case["language"] == "vi" for case in test_cases), 15)
        self.assertEqual(set(evaluation.synthetic_email(test_cases[0])),
                         {"message_id", "thread_id", "from", "to", "subject", "date", "body"})

    def test_unexpected_or_missing_model_disqualifies_run(self):
        for raw in (response("{}", model="fallback"), '{"payloads": []}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "requested provider/model"):
                evaluation.checked_draft(raw, MODEL)

    def test_local_envelope_is_supported_and_contract_is_enforced(self):
        answer = {"summary": "Room unbooked.", "draft_body": "Can we confirm the room?"}
        local = json.loads(response(json.dumps(answer)))["result"]
        self.assertEqual(evaluation.checked_draft(json.dumps(local), MODEL), answer)
        self.assertIsNone(evaluation.checked_draft(response("no JSON"), MODEL))
        self.assertIsNone(evaluation.checked_draft(response('{"summary": "No draft"}'), MODEL))

    def generate_run(self, output, replies):
        version = subprocess.CompletedProcess([], 0, stdout="2026.9.5\n")
        # Stub every OpenClaw entry point, including profile and version checks.
        with (patch.object(evaluation.shutil, "which", return_value="/fake/openclaw"),
              patch.object(evaluation, "secure_path"),
              patch.object(evaluation, "model_environment", return_value={}),
              patch.object(evaluation, "captured", return_value=version),
              patch.object(evaluation, "_run_tool_free_process", side_effect=replies) as runner,
              redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr):
            self.runner = runner
            self.generation_stderr = stderr
            evaluation.main(["generate", "--split", "dev", "--model", MODEL,
                             "--output-dir", str(output)])

    def report(self, output):
        evaluation.main(["report", "--run-dir", str(output)])

    def assert_report_rejected(self, output, message):
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                self.report(output)
        self.assertEqual(error.exception.code, 1)
        self.assertIn(message, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_dataset_fingerprint_matches_git_lf_for_both_line_endings(self):
        canonical = evaluation.DATASET.read_bytes().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.jsonl"
            output = Path(directory) / "run"
            for ending in (b"\n", b"\r\n"):
                with self.subTest(ending=ending):
                    dataset.write_bytes(canonical.replace(b"\n", ending))
                    with (patch.object(evaluation, "DATASET", dataset),
                          patch.object(evaluation, "generate") as generate,
                          redirect_stdout(StringIO())):
                        evaluation.main(["generate", "--split", "dev", "--model", MODEL,
                                         "--output-dir", str(output)])
                    generate.assert_called_once_with(
                        [case for case in self.cases if case["split"] == "dev"],
                        MODEL, output, DATASET_SHA256, "dev",
                    )

    def test_identity_mismatch_aborts_after_a_complete_pair_without_writing_review(self):
        answer = response('{"summary": "Room unbooked.", "draft_body": "Can we confirm the room?"}')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with self.assertRaises(SystemExit) as error:
                self.generate_run(output, [answer, answer, response("{}", model="fallback")])
            self.assertEqual(error.exception.code, 1)
            self.assertIn("Generated dev-01", self.generation_stderr.getvalue())
            self.assertIn("dev-02 A: Response did not identify the requested provider/model",
                          self.generation_stderr.getvalue())
            self.assertEqual(self.runner.call_count, 3)
            self.assertFalse(output.exists())

    def test_paired_blind_review_and_report(self):
        def replies(email, prompt):
            if email["message_id"] == "dev-01" and prompt == evaluation.PROMPTS["baseline"]:
                return response("malformed")
            draft = "Can we confirm the details?"
            if email["message_id"] == "dev-02" and prompt == evaluation.PROMPTS["baseline"]:
                draft = "I confirm the booking."
            if email["message_id"] == "dev-03" and prompt == evaluation.PROMPTS["candidate"]:
                draft = "Thanks for your email."
            return response(json.dumps({"summary": email["subject"], "draft_body": draft}))

        dev_cases = [case for case in self.cases if case["split"] == "dev"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            self.generate_run(output, replies)
            calls = self.runner.call_args_list
            self.assertEqual(len(calls), 2 * len(dev_cases))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            review = json.loads((output / "review.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["case"] for entry in review], dev_cases)
            for index, entry in enumerate(review):
                pair = calls[2 * index:2 * index + 2]
                email = evaluation.synthetic_email(entry["case"])
                self.assertEqual(set(entry), {"case", "email_date", "answers"})
                self.assertEqual(entry["email_date"], "Mon, 12 Jan 2026 09:00:00 +0000")
                self.assertEqual(set(entry["answers"]), {"A", "B"})
                self.assertCountEqual([call.args[1] for call in pair], evaluation.PROMPTS.values())
                for label, call in zip(("A", "B"), pair):
                    arm = manifest["assignments"][entry["case"]["id"]][label]
                    self.assertEqual(call.args, (email, evaluation.PROMPTS[arm]))
                    self.assertEqual(entry["email_date"], call.args[0]["date"])
                    self.assertEqual(call.kwargs, {})

            self.assert_report_rejected(output, "Complete the three grades")
            grades = output / "grades.csv"
            with grades.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            answers = {(entry["case"]["id"], label): answer
                       for entry in review for label, answer in entry["answers"].items()}
            self.assertCountEqual([(row["case_id"], row["answer"]) for row in rows], answers)
            baseline_label = next(label for label, arm in manifest["assignments"]["dev-01"].items()
                                  if arm == "baseline")
            grades_by_answer = {(row["case_id"], row["answer"]): row for row in rows}
            invalid_answer = grades_by_answer["dev-01", baseline_label]
            self.assertEqual(invalid_answer, {
                "case_id": "dev-01", "answer": baseline_label,
                "facts_kept": "0", "unsupported_commitment": "", "needs_edit": "1",
                "notes": "Invalid model output",
            })
            self.assertEqual([key for key, answer in answers.items() if answer is None],
                             [("dev-01", baseline_label)])
            for key, answer in answers.items():
                if answer is None:
                    continue
                row = grades_by_answer[key]
                row.update(facts_kept="1", unsupported_commitment="0", needs_edit="0")
                if answer["draft_body"] == "I confirm the booking.":
                    row.update(unsupported_commitment="1", needs_edit="1",
                               notes="Promised an unconfirmed booking.")
                    commitment_answer = row
                if answer["draft_body"] == "Thanks for your email.":
                    row.update(facts_kept="0", needs_edit="1", notes="Omitted the correction.")

            def write_grades():
                with grades.open("w", encoding="utf-8", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=evaluation.GRADE_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)

            for row, field, value, message in (
                (invalid_answer, "facts_kept", "1", "Keep the prefilled grades"),
                (invalid_answer, "unsupported_commitment", "0", "Keep the prefilled grades"),
                (invalid_answer, "needs_edit", "0", "Keep the prefilled grades"),
                (commitment_answer, "unsupported_commitment", "2", "Complete the three grades"),
                (commitment_answer, "notes", "", "Explain the issue"),
            ):
                original = row[field]
                with self.subTest(case=row["case_id"], field=field):
                    row[field] = value
                    write_grades()
                    self.assert_report_rejected(output, message)
                row[field] = original

            write_grades()
            with redirect_stdout(StringIO()) as stdout:
                self.report(output)
            report = stdout.getvalue()
            self.assertIn("# Draft prompt evaluation (dev)", report)
            self.assertIn(f"Model: `{MODEL}`", report)
            self.assertIn("OpenClaw: `2026.9.5`", report)
            self.assertIn(f"Dataset SHA-256: `{DATASET_SHA256}`", report)
            for arm, digest in manifest["prompt_sha256"].items():
                self.assertIn(f"{arm.title()} prompt SHA-256: `{digest}`", report)
            self.assertIn("| Baseline | 5/6 | 5/6 | 1/5 | 2/6 |", report)
            self.assertIn("| Candidate | 6/6 | 5/6 | 0/6 | 1/6 |", report)
            self.assertEqual([line for line in report.splitlines() if line.startswith("- ")], [
                "- dev-01 (baseline) — Invalid model output",
                "- dev-03 (candidate) — Omitted the correction.",
            ])

            # Exercise the original packet shape using only the temporary mocked run.
            for entry in review:
                del entry["email_date"]
            (output / "review.json").write_text(json.dumps(review), encoding="utf-8")
            with redirect_stdout(StringIO()) as legacy_stdout:
                self.report(output)
            self.assertEqual(legacy_stdout.getvalue(), report)

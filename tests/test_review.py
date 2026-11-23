import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import review


KEY = "test-key-for-offline-fixtures-only-123456789"


def answer(body, choice="no_violation", tokens=200):
    probabilities = {"violation": 0.04, "no_violation": 0.92, "insufficient": 0.04}
    if choice != "no_violation":
        probabilities[choice], probabilities["no_violation"] = 0.92, 0.04
    return {"model": review.MODEL,
            "answers": {identifier: {"type": "choice", "choice": choice, "confidence": 0.88,
                                      "probabilities": dict(probabilities)} for identifier in body["questions"]},
            "usage": {"input_tokens": tokens, "output_tokens": 20}}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.put("lib/production/paid.mjs", "import { lookup } from './receipt.mjs';\nexport const run = () => lookup();\n")
        self.put("lib/production/receipt.mjs", "export const lookup = () => ({status: 'unknown'});\n")
        self.put("tests/paid.test.mjs", "import { run } from '../lib/production/paid.mjs';\n// fake test evidence\n")
        self.put(".env", "TYPESAFE_API_KEY=fixture-only-never-send\n")
        self.put("production-workspaces/job.json", '{"private": "fixture only"}')
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        self.run_path = self.base / "review-run"

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                              capture_output=True, check=True).stdout

    def put(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def prepare(self, **kwargs):
        return review.prepare(self.repo, output=self.run_path, **kwargs)[1]

    def first(self):
        manifest = review.read_json(self.run_path / "manifest.json")
        item = manifest["requests"][0]
        body = review.read_json(self.run_path / "requests" / (item["id"] + ".json"))
        return item, body

    def live(self, transport, **kwargs):
        return review.execute(self.run_path, live=True, budget="1", limit=1, key=KEY, transport=transport, **kwargs)

    def test_preparation_uses_commit_and_never_changes_worktree(self):
        self.put("lib/production/paid.mjs", "UNCOMMITTED CHANGE\n")
        before = self.git("status", "--porcelain=v1", "-z")
        manifest = self.prepare()
        self.assertFalse(manifest["worktree_changes_included"])
        self.assertNotIn("UNCOMMITTED", (self.run_path / "source/lib/production/paid.mjs").read_text())
        self.assertEqual(before, self.git("status", "--porcelain=v1", "-z"))

    def test_env_workspaces_secrets_and_symlinks_never_enter_payloads(self):
        fake_secret = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"
        self.put("lib/key.mjs", 'const key = "' + fake_secret + '";')
        (self.repo / "lib/link.mjs").symlink_to(self.repo / ".env")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "exclusions")
        manifest = self.prepare()
        all_payloads = "".join(path.read_text() for path in (self.run_path / "requests").glob("*.json"))
        self.assertNotIn("fixture-only-never-send", all_payloads)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", all_payloads)
        self.assertFalse((self.run_path / "source/.env").exists())
        self.assertFalse((self.run_path / "source/lib/link.mjs").exists())
        self.assertEqual({item["file"] for item in manifest["excluded"]}, {"lib/link.mjs", "lib/key.mjs"})

    def commit_fixture(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture extension")

    def test_generic_source_layout_and_languages_are_included(self):
        self.put("src/service.py", "def value():\n    return 1\n")
        self.put("backend/main.go", "package main\nfunc main() {}\n")
        self.put("crates/example/src/lib.rs", "pub fn value() -> u8 { 1 }\n")
        self.put("main.py", "print('fixture')\n")
        self.commit_fixture()
        manifest = self.prepare()
        files = {item["file"] for item in manifest["requests"]}
        self.assertTrue({"src/service.py", "backend/main.go", "crates/example/src/lib.rs", "main.py"}.issubset(files))

    def test_generated_dependencies_and_runtime_files_are_not_copied(self):
        for name in ("node_modules/a/index.js", "vendor/a.py", "dist/app.js", "coverage/result.json", "logs/session.json", "outputs/client.json"):
            self.put(name, '{"value": "not source"}\n')
        self.commit_fixture()
        self.prepare()
        for name in ("node_modules", "vendor", "dist", "coverage", "logs", "outputs"):
            self.assertFalse((self.run_path / "source" / name).exists())

    def test_python_and_nested_test_files_are_context_by_default(self):
        self.put("src/test_service.py", "def test_value():\n    assert True\n")
        self.put("packages/demo/__tests__/value.spec.ts", "export const fixture = true;\n")
        self.commit_fixture()
        manifest = self.prepare()
        self.assertTrue((self.run_path / "source/src/test_service.py").is_file())
        self.assertFalse(any(item["file"] in {"src/test_service.py", "packages/demo/__tests__/value.spec.ts"} for item in manifest["requests"]))

    def test_generic_test_files_can_be_explicitly_reviewed(self):
        self.put("src/test_service.py", "def test_value():\n    assert True\n")
        self.commit_fixture()
        manifest = self.prepare(with_tests=True)
        self.assertTrue(any(item["file"] == "src/test_service.py" for item in manifest["requests"]))

    def test_quoted_json_credential_is_withheld(self):
        self.put("src/settings.json", json.dumps({"api_key": "fictional-credential-" + "x" * 30}))
        self.commit_fixture()
        manifest = self.prepare()
        self.assertFalse((self.run_path / "source/src/settings.json").exists())
        self.assertTrue(any(item["file"] == "src/settings.json" and "credential" in item["reason"] for item in manifest["excluded"]))

    def test_related_import_and_test_context_is_identified(self):
        manifest = self.prepare()
        item = next(item for item in manifest["requests"] if item["file"].endswith("paid.mjs"))
        body = review.read_json(self.run_path / "requests" / (item["id"] + ".json"))
        self.assertEqual({entry["file"] for entry in body["state"]["related"]},
                         {"lib/production/receipt.mjs", "tests/paid.test.mjs"})

    def test_tests_are_context_unless_explicitly_included(self):
        manifest = self.prepare(with_tests=True)
        self.assertTrue(any(item["file"].startswith("tests/") for item in manifest["requests"]))

    def test_root_startup_and_package_files_are_in_review_scope(self):
        self.put("instrumentation.ts", "export async function register() {}\n")
        self.put("package.json", '{"name": "fixture"}\n')
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "root files")
        manifest = self.prepare()
        reviewed = {item["file"] for item in manifest["requests"]}
        self.assertTrue({"instrumentation.ts", "package.json"}.issubset(reviewed))

    def test_oversized_code_is_reported_as_uncovered(self):
        self.put("lib/large.mjs", "x" * 40_000)
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "large")
        manifest = self.prepare()
        self.assertTrue(any(item["file"] == "lib/large.mjs" for item in manifest["uncovered"]))

    def test_output_inside_original_repo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            review.prepare(self.repo, output=self.repo / "review")

    def test_output_checks_actual_repository_root_even_when_given_subdirectory(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            review.prepare(self.repo / "lib", output=self.repo / "review")

    def test_offline_mode_cannot_call_transport_even_with_key(self):
        self.prepare()
        transport = mock.Mock(side_effect=AssertionError("network forbidden"))
        result = review.execute(self.run_path, key=KEY, transport=transport)
        self.assertEqual(result["network_calls"], 0)
        transport.assert_not_called()

    def test_env_file_values_are_literal_and_environment_overrides(self):
        env = self.base / "review.env"
        marker = self.base / "must-not-exist"
        literal = '$(touch ' + str(marker) + ')#literal'
        env.write_text('TYPESAFE_API_KEY="' + literal + '"\nJEV_REVIEW_BUDGET_USD=0.05\nJEV_REVIEW_MAX_REQUESTS=5\n')
        settings = review.load_settings(env, environ={})
        self.assertEqual(settings["TYPESAFE_API_KEY"], literal)
        self.assertFalse(marker.exists())
        self.assertEqual(review.load_settings(env, environ={"TYPESAFE_API_KEY": KEY})["TYPESAFE_API_KEY"], KEY)

    def test_env_file_rejects_malformed_input_without_exposing_value(self):
        env = self.base / "review.env"
        env.write_text('TYPESAFE_API_KEY="' + KEY + '\n')
        with self.assertRaises(ValueError) as failure:
            review.load_settings(env, environ={})
        self.assertNotIn(KEY, str(failure.exception))

    def test_cli_live_uses_env_defaults_but_explicit_limits_win(self):
        settings = {"TYPESAFE_API_KEY": KEY, "JEV_REVIEW_BUDGET_USD": "0.05", "JEV_REVIEW_MAX_REQUESTS": "5"}
        with mock.patch.object(review, "load_settings", return_value=settings), mock.patch.object(review, "execute", return_value={}) as execute, contextlib.redirect_stdout(io.StringIO()):
            review.main(["run", str(self.run_path), "--live"])
            self.assertEqual(execute.call_args.kwargs["limit"], 5)
            self.assertEqual(execute.call_args.kwargs["budget"], "0.05")
            self.assertEqual(execute.call_args.kwargs["key"], KEY)
            review.main(["run", str(self.run_path), "--live", "--budget-usd", "0.01", "--max-requests", "1"])
            self.assertEqual(execute.call_args.kwargs["limit"], 1)
            self.assertEqual(execute.call_args.kwargs["budget"], "0.01")

    def test_offline_cli_does_not_read_env_file(self):
        with mock.patch.object(review, "load_settings", side_effect=AssertionError("must not read secrets")), mock.patch.object(review, "execute", return_value={}) as execute, contextlib.redirect_stdout(io.StringIO()):
            review.main(["run", str(self.run_path)])
            self.assertFalse(execute.call_args.kwargs["live"])

    def test_live_requires_key_budget_and_request_limit(self):
        self.prepare()
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
                review.execute(self.run_path, live=True, budget=1, limit=1)
        with self.assertRaisesRegex(ValueError, "budget"):
            review.execute(self.run_path, live=True, key=KEY)

    def test_live_response_is_saved_and_reexecution_is_cached(self):
        self.prepare()
        transport = mock.Mock(side_effect=lambda body, key: review.encoded(answer(body)))
        item, _ = self.first()
        self.assertEqual(self.live(transport, identifiers=[item["id"]])["sent"], 1)
        self.assertEqual(self.live(transport, identifiers=[item["id"]])["sent"], 0)
        self.assertEqual(transport.call_count, 1)
        self.assertNotIn(KEY, "".join(path.read_text() for path in self.run_path.rglob("*") if path.is_file()))

    def test_timeout_is_recorded_and_never_automatically_resent(self):
        self.prepare()
        item, _ = self.first()
        transport = mock.Mock(side_effect=TimeoutError("unconfirmed"))
        self.live(transport, identifiers=[item["id"]])
        self.live(transport, identifiers=[item["id"]])
        self.assertEqual(transport.call_count, 1)
        attempt = review.read_json(self.run_path / "attempts" / (item["id"] + ".json"))
        self.assertEqual(attempt["status"], "unknown")
        self.assertEqual(review.accounted_cost(self.run_path), review.REQUEST_TOKEN_RESERVE * review.PRICE)

    def test_http_error_stops_without_automatic_retry(self):
        self.prepare()
        transport = mock.Mock(side_effect=urllib.error.HTTPError(review.API, 402, "no credit", {}, None))
        self.live(transport)
        self.assertEqual(transport.call_count, 1)
        self.assertIn("HTTP 오류 1", (self.run_path / "REPORT.md").read_text())

    def test_budget_reserves_next_maximum_request_before_sending(self):
        self.prepare()
        transport = mock.Mock(side_effect=AssertionError("budget forbids calls"))
        result = review.execute(self.run_path, live=True, budget="0.00001", limit=3, key=KEY, transport=transport)
        self.assertEqual(result["sent"], 0)
        transport.assert_not_called()

    def test_valid_received_result_recovers_after_interrupted_recording(self):
        self.prepare()
        item, body = self.first()
        review.write_json(self.run_path / "attempts" / (item["id"] + ".json"), {"id": item["id"], "status": "sending"})
        review.write_json(self.run_path / "responses" / (item["id"] + ".received"), answer(body))
        transport = mock.Mock(side_effect=AssertionError("received result must be recovered"))
        result = self.live(transport, identifiers=[item["id"]])
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(review.read_json(self.run_path / "attempts" / (item["id"] + ".json"))["status"], "complete")
        transport.assert_not_called()

    def test_malformed_received_data_is_kept_and_not_resent(self):
        self.prepare()
        item, _ = self.first()
        transport = mock.Mock(return_value=b'{"wrong": "shape"}')
        self.live(transport, identifiers=[item["id"]])
        self.live(transport, identifiers=[item["id"]])
        self.assertEqual(transport.call_count, 1)
        self.assertTrue((self.run_path / "responses" / (item["id"] + ".received")).exists())

    def test_response_rejects_missing_answers_invalid_probabilities_and_unknown_options(self):
        self.prepare()
        _, body = self.first()
        valid = answer(body)
        review.validate_response(valid, body["questions"])
        for kind in ("missing", "nan", "option", "not_maximum", "usage", "model"):
            value = copy.deepcopy(valid)
            first = next(iter(value["answers"]))
            if kind == "missing":
                value["answers"].pop(first)
            elif kind == "nan":
                value["answers"][first]["confidence"] = float("nan")
            elif kind == "option":
                value["answers"][first]["probabilities"]["invented"] = 0.1
            elif kind == "not_maximum":
                value["answers"][first]["choice"] = "violation"
            elif kind == "usage":
                value["usage"]["input_tokens"] = True
            else:
                value["model"] = "unexpected-model-version"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                review.validate_response(value, body["questions"])

    def test_changed_request_cannot_be_sent(self):
        self.prepare()
        item, body = self.first()
        body["state"]["extra"] = "tampered"
        review.write_json(self.run_path / "requests" / (item["id"] + ".json"), body)
        transport = mock.Mock()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.live(transport, identifiers=[item["id"]])
        transport.assert_not_called()

    def test_manifest_rejects_path_traversal_and_duplicate_request_ids(self):
        self.prepare()
        path = self.run_path / "manifest.json"
        original = review.read_json(path)
        for kind in ("path", "duplicate"):
            manifest = copy.deepcopy(original)
            if kind == "path":
                manifest["requests"][0]["id"] = "../../outside"
            else:
                manifest["requests"].append(copy.deepcopy(manifest["requests"][0]))
            review.write_json(path, manifest)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "identifier"):
                review.execute(self.run_path)

    def test_report_labels_candidates_as_unconfirmed(self):
        self.prepare()
        item, _ = self.first()
        self.live(lambda body, key: review.encoded(answer(body, choice="violation")), identifiers=[item["id"]])
        text = (self.run_path / "REPORT.md").read_text()
        self.assertIn("**미확인**", text)
        self.assertIn("확인된 결함이 아니며", text)

    def test_redirect_is_refused_before_credentials_can_be_forwarded(self):
        request = review.urllib.request.Request(review.API)
        with self.assertRaises(urllib.error.HTTPError):
            review.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://example.invalid")

    def test_second_process_cannot_enter_run_lock(self):
        self.prepare()
        with review.run_lock(self.run_path):
            with self.assertRaisesRegex(ValueError, "Another review process"):
                with review.run_lock(self.run_path):
                    self.fail("concurrent lock acquired")


if __name__ == "__main__":
    unittest.main()

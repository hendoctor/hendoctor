import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.chatgpt_stats import run


class RunTests(unittest.TestCase):
    def invoke(self, results, *, publish=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "accounts.json"
            config.write_text(json.dumps({"personal": {}, "work": {}}))
            argv = ["run.py", "--config", str(config), "--data", str(root / "usage.json")]
            if publish:
                argv.append("--publish")
            with patch("sys.argv", argv), patch.object(run, "run_step", side_effect=results) as step:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = run.main()
            return code, step.call_args_list

    def test_partial_failure_still_refreshes_other_account_and_publishes(self):
        code, calls = self.invoke([False, True, True], publish=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args[1][1], "personal")
        self.assertEqual(calls[1].args[1][1], "work")
        self.assertIn("--publish", calls[2].args[1])

    def test_default_does_not_write_github_and_publisher_receives_no_work_token(self):
        with patch.dict(run.os.environ, {"CHATGPT_WORK_ACCESS_TOKEN": "private-test-value", "CODEX_ACCESS_TOKEN": "other-test-value"}):
            code, calls = self.invoke([True, True, True])
        self.assertEqual(code, 0)
        self.assertNotIn("--publish", calls[2].args[1])
        self.assertNotIn("CHATGPT_WORK_ACCESS_TOKEN", calls[2].kwargs["env"])
        self.assertNotIn("CODEX_ACCESS_TOKEN", calls[2].kwargs["env"])

    def test_invalid_config_does_not_collect_or_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "accounts.json"
            config.write_text('{"token":"must-not-be-public"}')
            with patch("sys.argv", ["run.py", "--config", str(config), "--data", str(Path(directory) / "usage.json")]), patch.object(run, "run_step") as step:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(run.main(), 1)
                step.assert_not_called()


if __name__ == "__main__":
    unittest.main()

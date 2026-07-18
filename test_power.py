import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import power


OMNIUS_ENV_NAMES = [
    "OMNIUS_REST_READ_API_KEY",
    "OMNIUS_READ_API_KEY",
    "OMNIUS_REST_RUN_API_KEY",
    "OMNIUS_RUN_API_KEY",
    "OMNIUS_REST_ADMIN_API_KEY",
    "OMNIUS_ADMIN_API_KEY",
    "OMNIUS_REST_API_KEY",
    "OMNIUS_API_KEY",
    "OMNIUS_RATE_MODEL",
    "OMNIUS_RATE_TIMEOUT",
]


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class OmniusKeyTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {name: "" for name in OMNIUS_ENV_NAMES})
        self.env_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.omnius_home = Path(self.tmp.name)
        self.original_paths = (
            power.OMNIUS_HOME,
            power.OMNIUS_KEY_FILE,
            power.OMNIUS_DAEMON_ENV,
            power.OMNIUS_BOOTSTRAP_KEY_FILE,
            power.OMNIUS_RUNTIME_KEYS_FILE,
        )
        power.OMNIUS_HOME = self.omnius_home
        power.OMNIUS_KEY_FILE = self.omnius_home / "power-monitor.env"
        power.OMNIUS_DAEMON_ENV = self.omnius_home / "cygnus-daemon.env"
        power.OMNIUS_BOOTSTRAP_KEY_FILE = self.omnius_home / "api.key"
        power.OMNIUS_RUNTIME_KEYS_FILE = self.omnius_home / "keys.json"

    def tearDown(self):
        (
            power.OMNIUS_HOME,
            power.OMNIUS_KEY_FILE,
            power.OMNIUS_DAEMON_ENV,
            power.OMNIUS_BOOTSTRAP_KEY_FILE,
            power.OMNIUS_RUNTIME_KEYS_FILE,
        ) = self.original_paths
        self.tmp.cleanup()
        self.env_patch.stop()

    def test_reads_current_bootstrap_and_runtime_key_files(self):
        power.OMNIUS_BOOTSTRAP_KEY_FILE.write_text("bootstrap-key\n")
        power.OMNIUS_RUNTIME_KEYS_FILE.write_text(
            json.dumps(
                [
                    {"key": "read-key", "scope": "read", "revoked": None},
                    {"key": "run-key", "scope": "run", "revoked": None},
                    {"key": "admin-key", "scope": "admin", "revoked": None},
                    {"key": "revoked-key", "scope": "admin", "revoked": "yes"},
                ]
            )
        )

        self.assertEqual(power._read_bootstrap_api_key(), "bootstrap-key")
        self.assertEqual(power._runtime_key_candidates(), ["admin-key", "run-key"])
        self.assertEqual(
            power._runtime_key_candidates(include_read=True),
            ["admin-key", "run-key", "read-key"],
        )

    def test_reads_legacy_env_files_with_export_and_quotes(self):
        power.OMNIUS_DAEMON_ENV.write_text("export OMNIUS_RUN_API_KEY='daemon-key'\n")
        power.OMNIUS_KEY_FILE.write_text('OMNIUS_API_KEY="stored-key"\n')

        self.assertEqual(power._read_daemon_run_key(), "daemon-key")
        self.assertEqual(power._stored_key(), "stored-key")


class OmniusChatTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"OMNIUS_RUN_API_KEY": "run-key", "OMNIUS_RATE_TIMEOUT": "12"},
        clear=False,
    )
    @patch("power.httpx.post")
    def test_chat_uses_agent_loop_web_search_payload(self, mock_post):
        mock_post.return_value = FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "RATE=0.1239 UTILITY=Portland General Electric "
                                "SOURCE=https://example.com/rates"
                            )
                        }
                    }
                ]
            },
        )

        content, error = power._omnius_chat("auto", [{"role": "user", "content": "rate"}])

        self.assertIn("RATE=0.1239", content)
        self.assertEqual(error, "")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "auto")
        self.assertTrue(payload["agent_loop"])
        self.assertEqual(payload["include_daemon_tools"], ["read"])
        self.assertEqual(payload["prompt_template"], "factual-first")
        self.assertEqual(payload["timeout_s"], 12.0)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 22.0)

    def test_sanitizes_nested_backend_errors(self):
        message = power._omnius_error_message(
            {
                "error": "Backend request failed",
                "details": json.dumps(
                    {
                        "error": {
                            "message": (
                                "Key limit exceeded. Manage it using "
                                "https://openrouter.ai/workspaces/default/keys/abc123"
                            )
                        }
                    }
                ),
            }
        )

        self.assertIn("Backend request failed", message)
        self.assertIn("OpenRouter key settings", message)
        self.assertNotIn("abc123", message)


class OmniusRateDiscoveryTests(unittest.TestCase):
    def test_parses_one_line_rate_response_and_normalises_cents(self):
        rate, source, url = power._parse_omnius_rate_response(
            (
                "RATE=14.75 UTILITY=Portland General Electric "
                "SOURCE=https://portlandgeneral.com/rates."
            ),
            "Portland, Oregon",
        )

        self.assertAlmostEqual(rate, 0.1475)
        self.assertEqual(source, "Portland General Electric (Omnius)")
        self.assertEqual(url, "https://portlandgeneral.com/rates")

    @patch("power._omnius_chat")
    @patch("power._omnius_rate_models", return_value=["old-local", "auto"])
    def test_discovers_rate_after_invalid_model_fallback(self, _mock_models, mock_chat):
        mock_chat.side_effect = [
            (None, "old-local is not a valid model ID"),
            (
                "RATE=0.1239 UTILITY=Portland General Electric SOURCE=https://example.com",
                "",
            ),
        ]

        rate, source, url = power._discover_rate_via_omnius("Portland, Oregon")

        self.assertAlmostEqual(rate, 0.1239)
        self.assertEqual(source, "Portland General Electric (Omnius)")
        self.assertEqual(url, "https://example.com")
        self.assertEqual(mock_chat.call_count, 2)

    @patch("power._ensure_omnius", return_value=False)
    @patch("power._detect_location", return_value="Portland, Oregon")
    def test_discover_rate_falls_back_to_builtin_with_omnius_error(
        self, _mock_location, _mock_ensure
    ):
        rate, source, url = power.discover_rate()

        self.assertAlmostEqual(rate, 0.1239)
        self.assertIn("Portland, OR (built-in)", source)
        self.assertIn("Omnius: omnius unavailable or not authorized", source)
        self.assertEqual(url, "")


if __name__ == "__main__":
    unittest.main()

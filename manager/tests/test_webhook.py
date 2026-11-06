import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["PROBER_RELOAD_URL"] = ""
os.environ.setdefault("BACKUP_DIR", tempfile.mkdtemp(prefix="smokeping-backups-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app import ConfigStore, WebhookInput, WebhookSettings, WebhookWatcher  # noqa: E402

CONFIG = """routers:
  - name: ne8k
    address: 10.0.0.1:22
    username: smokeping
    password_file: /etc/smokeping_prober/ne8k.pass
    insecure_skip_host_key: true
    links:
      - name: isp-a
        vpn_instance: upstream-1
        source: 192.0.2.1
      - name: isp-b
        source: 198.51.100.1
targets:
  - host: 8.8.8.8
    router: ne8k
    interval: 1m
    labels: {title: G, category: DNS, alerts_enabled: "true"}
"""


def summary(loss, samples=10):
    return {"samples": samples, "loss": loss, "latency": 0 if loss >= 100 else 20.0, "jitter": 0 if loss >= 100 else 1.0}


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        self.settings = WebhookSettings(root / "webhook.json")
        self.settings.save(WebhookInput(enabled=True, url="http://n8n/webhook", auth_header="X-Token", auth_value="s3cret"))
        self.prometheus = MagicMock()
        self.watcher = WebhookWatcher(ConfigStore(root / "config.yaml"), self.prometheus, self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def metrics(self, isp_a, isp_b=0, local=None):
        values = {"samples": None, "loss": None, "latency": None, "jitter": None}
        if local is not None:
            values.update(summary(local, 60))
        values["links"] = [{"name": "isp-a", **summary(isp_a)}, {"name": "isp-b", **summary(isp_b)}]
        self.prometheus.metrics.return_value = ({("8.8.8.8", "G", "DNS"): values}, "")

    def test_fires_once_per_link_then_recovers(self):
        with patch("app.requests.post") as post:
            self.metrics(isp_a=100)
            self.watcher.check(now=1000)
            self.watcher.check(now=1015)  # still failing: no repeat
            self.assertEqual(1, post.call_count)
            self.prometheus.metrics.assert_called_with("60s")
            body = post.call_args.kwargs["json"]
            self.assertEqual({"X-Token": "s3cret"}, post.call_args.kwargs["headers"])
            self.assertEqual("loss_high", body["event"])
            self.assertEqual("remote", body["probe"]["type"])
            self.assertEqual({"name": "ne8k", "address": "10.0.0.1:22"}, body["probe"]["router"])
            self.assertEqual("upstream-1", body["probe"]["link"]["vpn_instance"])
            self.assertEqual("192.0.2.1", body["probe"]["link"]["source"])
            self.assertEqual({"samples": 10, "loss_percent": 100, "latency_ms": None, "jitter_ms": None}, body["metrics"])
            self.assertEqual([("isp-a", True), ("isp-b", False)], [(s["link"], s["failing"]) for s in body["all_series"]])
            self.assertEqual("true", body["target"]["labels"]["alerts_enabled"])
            self.assertNotIn("password_file", json.dumps(body))

            self.metrics(isp_a=10)  # between recovery (5) and trigger (20): still open
            self.watcher.check(now=1030)
            self.assertEqual(1, post.call_count)

            self.metrics(isp_a=0)
            self.watcher.check(now=1100)
            self.assertEqual(2, post.call_count)
            body = post.call_args.kwargs["json"]
            self.assertEqual("recovered", body["event"])
            self.assertEqual(100, body["incident"]["duration_seconds"])

    def test_local_series_and_failed_post_is_retried(self):
        with patch("app.requests.post") as post:
            post.return_value.raise_for_status.side_effect = RuntimeError("n8n down")
            self.metrics(isp_a=0, local=50)
            self.watcher.check(now=1000)
            post.return_value.raise_for_status.side_effect = None
            self.watcher.check(now=1015)
            self.assertEqual(2, post.call_count)
            self.watcher.check(now=1030)
            self.assertEqual(2, post.call_count)
            body = post.call_args.kwargs["json"]
            self.assertEqual({"type": "local", "router": None, "link": None}, body["probe"])

    def test_disabled_or_no_samples_does_nothing(self):
        with patch("app.requests.post") as post:
            self.metrics(isp_a=100)
            self.prometheus.metrics.return_value[0][("8.8.8.8", "G", "DNS")]["links"][0]["samples"] = 0
            self.watcher.check(now=1000)
            self.settings.save(WebhookInput(enabled=False, url="http://n8n/webhook"))
            self.metrics(isp_a=100)
            self.watcher.check(now=1015)
            post.assert_not_called()

    def test_settings_keep_secret_and_validate(self):
        saved = self.settings.save(WebhookInput(enabled=True, url="https://n8n/x", auth_header="X-Token"))
        self.assertEqual("s3cret", saved["auth_value"])
        public = WebhookSettings.public(self.settings.load())
        self.assertNotIn("auth_value", public)
        self.assertTrue(public["auth_value_set"])
        self.assertEqual(0o600, self.settings.path.stat().st_mode & 0o777)
        self.assertEqual("", self.settings.save(WebhookInput(url="https://n8n/x"))["auth_value"])
        with self.assertRaises(ValidationError):
            WebhookInput(enabled=True)
        with self.assertRaises(ValidationError):
            WebhookInput(url="ftp://x")
        with self.assertRaises(ValidationError):
            WebhookInput(loss_percent=10, recovery_loss_percent=10)


if __name__ == "__main__":
    unittest.main()

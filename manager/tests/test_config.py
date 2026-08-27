import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PROBER_RELOAD_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import ConfigStore, TargetInput, target_id  # noqa: E402


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.yaml"
        self.store = ConfigStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @patch("app.reload_prober", return_value="")
    def test_create_toggle_update_delete(self, _reload):
        target, _ = self.store.create(TargetInput(title="Cloudflare", host="1.1.1.1", category="DNS"))
        self.assertEqual("ip4", target["network"])
        self.assertTrue(target["alerts_enabled"])

        target, _ = self.store.toggle(target["id"], False)
        self.assertFalse(target["alerts_enabled"])

        old_id = target["id"]
        target, _ = self.store.update(old_id, TargetInput(title="Cloudflare IPv6", host="2606:4700:4700::1111", category="DNS"))
        self.assertEqual("ip6", target["network"])
        self.assertNotEqual(old_id, target["id"])

        self.store.delete(target["id"])
        self.assertEqual([], self.store.list())

    @patch("app.reload_prober", return_value="")
    def test_preserves_unmanaged_top_level_keys(self, _reload):
        self.path.write_text("global:\n  owner: noc\ntargets: []\n", encoding="utf-8")
        self.store.create(TargetInput(title="Google", host="8.8.8.8", category="DNS"))
        self.assertIn("global:", self.path.read_text(encoding="utf-8"))

    def test_target_id_is_stable(self):
        target = {"host": "1.1.1.1", "labels": {"title": "Cloudflare", "category": "DNS"}}
        self.assertEqual(target_id(target), target_id(target))


if __name__ == "__main__":
    unittest.main()

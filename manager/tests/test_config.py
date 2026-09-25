import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PROBER_RELOAD_URL"] = ""
os.environ.setdefault("BACKUP_DIR", tempfile.mkdtemp(prefix="smokeping-backups-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import ConfigStore, PrometheusClient, RouterInput, TargetInput, metric_status, target_id  # noqa: E402


def ne8k(**overrides):
    data = {
        "name": "ne8k",
        "address": "201.131.152.1:61341",
        "username": "smokeping",
        "password_file": "/etc/smokeping_prober/ne8k.pass",
        "known_hosts": "/etc/smokeping_prober/known_hosts",
        "links": [
            {"name": "viams", "vpn_instance": "upstream-1", "source": "45.174.220.14", "source6": "2804:5bc8:f000::4"},
            {"name": "brdigital", "vpn_instance": "upstream-2", "source": "201.16.216.77", "source6": "2804:342::2"},
        ],
    }
    data.update(overrides)
    return RouterInput(**data)


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

    @patch("app.reload_prober", return_value="")
    def test_router_crud_and_remote_target(self, _reload):
        router, _ = self.store.create_router(ne8k())
        self.assertEqual(2, len(router["links"]))
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("routers:"), text.index("targets:"))

        target, _ = self.store.create(
            TargetInput(title="Google 1 - 8.8.8.8", host="8.8.8.8", category="DNS", router="ne8k", interval="1m")
        )
        self.assertEqual("ne8k", target["router"])
        self.assertNotIn("links:", self.path.read_text(encoding="utf-8").split("targets:")[1])

        # Same host/category pinged locally is a different target.
        local, _ = self.store.create(TargetInput(title="Google 1 - 8.8.8.8", host="8.8.8.8", category="DNS"))
        self.assertNotEqual(local["id"], target["id"])

        with self.assertRaises(HTTPException) as ctx:
            self.store.delete_router("ne8k")
        self.assertEqual(409, ctx.exception.status_code)

        # Renaming cascades to the targets that use the router.
        self.store.update_router("ne8k", ne8k(name="ne8k-core"))
        remote = [t for t in self.store.list() if t["router"]]
        self.assertEqual(["ne8k-core"], [t["router"] for t in remote])
        self.assertEqual(1, self.store.routers()[0]["targets"])

        self.store.delete(remote[0]["id"])
        self.store.delete_router("ne8k-core")
        self.assertEqual([], self.store.routers())
        self.assertNotIn("routers:", self.path.read_text(encoding="utf-8"))

    @patch("app.reload_prober", return_value="")
    def test_remote_target_validation(self, _reload):
        with self.assertRaises(HTTPException):
            self.store.create(TargetInput(title="X", host="8.8.8.8", category="DNS", router="missing"))
        self.store.create_router(ne8k(links=[{"name": "isp-a", "source": "192.0.2.1"}]))
        with self.assertRaises(HTTPException) as ctx:
            self.store.create(TargetInput(title="X", host="2001:4860:4860::8888", category="DNS", router="ne8k"))
        self.assertIn("source6", ctx.exception.detail)

    def test_router_input_validation(self):
        self.assertEqual("10.0.0.1:22", ne8k(address="10.0.0.1").address)
        self.assertEqual("[2001:db8::1]:2222", ne8k(address="[2001:db8::1]:2222").address)
        with self.assertRaises(ValidationError):
            ne8k(address="2001:db8::1")
        with self.assertRaises(ValidationError):
            ne8k(private_key_file="/etc/key")  # both auth methods
        with self.assertRaises(ValidationError):
            ne8k(known_hosts="")
        self.assertTrue(ne8k(known_hosts="", insecure_skip_host_key=True).insecure_skip_host_key)
        with self.assertRaises(ValidationError):
            ne8k(links=[{"name": "a", "source": "2001:db8::1"}])
        with self.assertRaises(ValidationError):
            ne8k(links=[{"name": "a", "source": "192.0.2.1"}, {"name": "a", "source": "192.0.2.2"}])

    def test_metrics_split_remote_links(self):
        def row(value, link="", host="8.8.8.8"):
            return {"metric": {"host": host, "title": "G", "category": "DNS", "link": link}, "value": [0, str(value)]}

        responses = {
            "smokeping_requests_total": [row(100), row(10, "viams"), row(10, "brdigital")],
            "seconds_count": [row(100), row(10, "viams"), row(5, "brdigital")],
            "seconds_sum": [row(1), row(0.1, "viams"), row(0.1, "brdigital")],
        }

        def query(expression):
            if "histogram_quantile" in expression:
                return []
            return next(rows for name, rows in responses.items() if name in expression)

        client = PrometheusClient()
        with patch.object(client, "query", side_effect=query):
            metrics, error = client.metrics()
        self.assertEqual("", error)
        self.assertEqual(0, metrics[("8.8.8.8", "G", "DNS", False)]["loss"])
        remote = metrics[("8.8.8.8", "G", "DNS", True)]
        self.assertEqual(25, remote["loss"])
        self.assertEqual({"viams": 0, "brdigital": 50}, {link["name"]: link["loss"] for link in remote["links"]})
        self.assertEqual("critical", metric_status(remote))

    @patch("app.reload_prober", return_value="")
    def test_tos_is_written_unquoted(self, _reload):
        self.path.write_text("targets:\n  - host: 1.1.1.1\n    tos: 0x00\n    labels: {title: A, category: DNS}\n", encoding="utf-8")
        self.assertEqual("0x00", self.store.list()[0]["tos"])
        self.store.create(TargetInput(title="Google", host="8.8.8.8", category="DNS", tos="0xB8"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("tos: 0xb8\n", text)
        self.assertNotIn("'0x", text)
        self.assertEqual(["0x00", "0xb8"], [t["tos"] for t in self.store.list()])

    def test_target_id_is_stable(self):
        target = {"host": "1.1.1.1", "labels": {"title": "Cloudflare", "category": "DNS"}}
        self.assertEqual(target_id(target), target_id(target))


if __name__ == "__main__":
    unittest.main()

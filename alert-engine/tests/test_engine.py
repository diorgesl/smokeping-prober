import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import Engine, MTRWorker, Measurement, Settings, duration_text  # noqa: E402


class EngineLogicTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            db_path=str(Path(self.temp.name) / "test.db"),
            dry_run=True,
            mtr_enabled=False,
            baseline_min_samples=1,
            confirmations=3,
            recovery_confirmations=3,
            min_samples=240,
        )
        self.engine = Engine(settings)

    def tearDown(self):
        self.temp.cleanup()

    def measurement(self, loss=0, latency=20):
        return Measurement("1.1.1.1", "Cloudflare", "DNS", 300, loss, latency)

    def test_three_confirmations_open_incident(self):
        now = 1_000_000
        self.assertIsNone(self.engine.process(self.measurement(loss=10), now))
        self.assertIsNone(self.engine.process(self.measurement(loss=10), now + 60))
        decision = self.engine.process(self.measurement(loss=10), now + 120)
        self.assertIsNotNone(decision)
        self.assertEqual("initial", decision.kind)

    def test_three_normal_evaluations_recover(self):
        now = 1_000_000
        for offset in (0, 60, 120):
            self.engine.process(self.measurement(loss=10), now + offset)
        self.assertIsNone(self.engine.process(self.measurement(), now + 180))
        self.assertIsNone(self.engine.process(self.measurement(), now + 240))
        decision = self.engine.process(self.measurement(), now + 300)
        self.assertEqual("recovered", decision.kind)

    def test_latency_requires_absolute_and_percent_limits(self):
        row = self.engine.store.get(self.measurement())
        self.engine.store.update("1.1.1.1", baseline_latency=20, baseline_samples=10)
        bad, _, latency_bad = self.engine.anomaly(self.measurement(latency=31), 20, 10)
        self.assertTrue(bad)
        self.assertTrue(latency_bad)
        bad, _, _ = self.engine.anomaly(self.measurement(latency=29), 20, 10)
        self.assertFalse(bad)

    def test_duration_format(self):
        self.assertEqual("7 minutos", duration_text(7 * 60))
        self.assertEqual("2h05", duration_text(125 * 60))

    def test_progressive_reminders(self):
        started = 1_000_000
        self.assertEqual(started + 1200, self.engine._next_reminder(started, started + 601))
        self.assertEqual(started + 1800, self.engine._next_reminder(started, started + 1201))
        self.assertEqual(started + 3600, self.engine._next_reminder(started, started + 1801))
        self.assertEqual(started + 10800, self.engine._next_reminder(started, started + 7201))

    def test_mtr_image_contains_every_hop(self):
        hubs = [
            {"count": i, "host": f"192.0.2.{i}", "Loss%": 0, "Snt": 10,
             "Avg": i, "Best": i - 0.1, "Wrst": i + 0.2}
            for i in range(1, 16)
        ]
        image = MTRWorker._render({"report": {"hubs": hubs}}, self.measurement())
        self.assertGreater(len(image), 1000)


if __name__ == "__main__":
    unittest.main()

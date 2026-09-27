from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


HARNESS = Path(__file__).with_name("carrier_turn_harness.mjs")


class CarrierTurnTests(unittest.TestCase):
    def test_mocked_browser_turn_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "private"
            result = subprocess.run(
                ["node", str(HARNESS), str(private_root)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["schema"], "lazy-carrier-turn-test/v2")
            self.assertEqual(receipt["testsPassing"], 18)
            self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)
            artifacts = list(private_root.glob("*.response.txt"))
            self.assertEqual(len(artifacts), 2)
            for artifact in artifacts:
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

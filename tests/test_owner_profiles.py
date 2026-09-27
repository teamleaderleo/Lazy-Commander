from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import owner_profiles


SOURCE = """#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--base-head', required=True)
p.add_argument('--task-class', required=True)
p.add_argument('--input', required=True)
p.add_argument('--task-ref', required=True)
p.add_argument('--previous', required=True)
p.add_argument('--output', required=True)
a=p.parse_args()
payload=json.dumps({'head': a.base_head, 'taskClass': a.task_class, 'taskRef': a.task_ref}, sort_keys=True).encode()+b'\\n'
fd=os.open(Path(a.output), os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as f: f.write(payload)
print(json.dumps({'ok': True}))
"""


class OwnerProfileTests(unittest.TestCase):
    def repository(self, root: Path) -> tuple[Path, Path, str]:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:teamleaderleo/example.git"],
            check=True,
        )
        tools = root / "tools"
        tools.mkdir()
        source = tools / "snapshot.py"
        source.write_text(SOURCE, encoding="utf-8")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        data = root / "input.json"
        data.write_text("{}\n", encoding="utf-8")
        profiles_dir = root / ".lazy"
        profiles_dir.mkdir()
        profile_path = profiles_dir / "observation-profiles.json"
        profile = {
            "schema": "lazy-owner-observation-profiles/v1",
            "repository": "teamleaderleo/example",
            "profiles": [
                {
                    "id": "snapshot",
                    "entrypoint": "tools/snapshot.py",
                    "sourceSha256": source_hash,
                    "parameters": [
                        {"name": "base-head", "flag": "--base-head", "kind": "git-oid"},
                        {
                            "name": "task-class",
                            "flag": "--task-class",
                            "kind": "enum",
                            "values": ["portfolio", "workspace-lifecycle"],
                        },
                        {
                            "name": "input",
                            "flag": "--input",
                            "kind": "absolute-file",
                            "roots": ["{repository}"],
                        },
                        {"name": "task-ref", "flag": "--task-ref", "kind": "safe-ref"},
                        {"name": "previous", "flag": "--previous", "kind": "sha256"},
                    ],
                    "output": {"flag": "--output", "filename": "result.json"},
                }
            ],
        }
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        return profile_path, data, hashlib.sha256(profile_path.read_bytes()).hexdigest()

    def parameters(self, data: Path) -> list[str]:
        return [
            "base-head=" + "a" * 40,
            "task-class=portfolio",
            f"input={data}",
            "task-ref=task:one",
            "previous=" + "b" * 64,
        ]

    def test_exact_profile_executes_to_private_fixed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            profile, data, profile_sha = self.repository(root)
            invocation = owner_profiles.compile_invocation(
                profile_path=profile,
                profile_id="snapshot",
                parameter_values=self.parameters(data),
                output_dir=root / "private-output",
                expected_profile_sha256=profile_sha,
            )
            receipt = owner_profiles.execute(invocation)
            self.assertEqual(receipt["profileSha256"], profile_sha)
            self.assertFalse(receipt["rawContentEmitted"])
            output = root / "private-output"
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "result.json").stat().st_mode), 0o600)
            self.assertNotIn(str(root), json.dumps(receipt))

    def test_source_or_profile_drift_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            profile, data, profile_sha = self.repository(root)
            (root / "tools" / "snapshot.py").write_text(SOURCE + "\n# drift\n", encoding="utf-8")
            with self.assertRaisesRegex(owner_profiles.ProfileError, "source hash"):
                owner_profiles.compile_invocation(
                    profile_path=profile,
                    profile_id="snapshot",
                    parameter_values=self.parameters(data),
                    output_dir=root / "out",
                    expected_profile_sha256=profile_sha,
                )
            (root / "tools" / "snapshot.py").write_text(SOURCE, encoding="utf-8")
            profile.write_text(profile.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(owner_profiles.ProfileError, "drifted"):
                owner_profiles.compile_invocation(
                    profile_path=profile,
                    profile_id="snapshot",
                    parameter_values=self.parameters(data),
                    output_dir=root / "out",
                    expected_profile_sha256=profile_sha,
                )

    def test_unknown_duplicate_and_outside_parameters_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            profile, data, profile_sha = self.repository(root)
            cases = (
                self.parameters(data) + ["unknown=value"],
                self.parameters(data) + ["task-ref=task:two"],
                [*self.parameters(data)[:2], "input=/etc/hosts", *self.parameters(data)[3:]],
            )
            for values in cases:
                with self.subTest(values=values), self.assertRaises(owner_profiles.ProfileError):
                    owner_profiles.compile_invocation(
                        profile_path=profile,
                        profile_id="snapshot",
                        parameter_values=values,
                        output_dir=root / "out",
                        expected_profile_sha256=profile_sha,
                    )

    def test_output_reuse_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            profile, data, profile_sha = self.repository(root)
            output = root / "existing"
            output.mkdir()
            with self.assertRaisesRegex(owner_profiles.ProfileError, "already exists"):
                owner_profiles.compile_invocation(
                    profile_path=profile,
                    profile_id="snapshot",
                    parameter_values=self.parameters(data),
                    output_dir=output,
                    expected_profile_sha256=profile_sha,
                )


if __name__ == "__main__":
    unittest.main()

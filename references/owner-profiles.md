# Checked owner observation profiles

Owner profiles let a repository declare a narrow read-only projection whose
semantics cannot be inferred safely from a generic Python command line. They do
not make arbitrary repository scripts observation-only.

The only accepted manifest location is repository-root
`.lazy/observation-profiles.json`. Lazy verifies the exact GitHub origin and
root, hashes the manifest and `tools/` entrypoint through no-follow regular-file
reads, validates a closed ordered parameter grammar, and fixes one output
filename inside a new mode-0700 directory. Results must be regular, at most 1
MiB, and mode 0600.

Supported parameter types are:

- `git-oid`: 40–64 lowercase hexadecimal characters;
- `sha256`: exactly 64 lowercase hexadecimal characters;
- `safe-ref`: a bounded opaque reference;
- `enum`: one checked manifest value;
- `absolute-file`: an existing no-follow regular file under an explicit root.

First validate and capture the manifest identity:

```sh
scripts/owner_profiles.py check \
  --profiles REPOSITORY/.lazy/observation-profiles.json \
  --profile PROFILE --output-dir PRIVATE_NEW_DIR \
  --param NAME=VALUE
```

Then put the exact returned `profileSha256` into the deferred command:

```sh
lazy defer --at 2026-09-01T08:00:00Z --cwd REPOSITORY -- \
  /absolute/lazy/scripts/owner_profiles.py run \
  --profiles REPOSITORY/.lazy/observation-profiles.json \
  --profile PROFILE --expected-profile-sha256 SHA256 \
  --output-dir PRIVATE_NEW_DIR --param NAME=VALUE
```

The request records the wrapper, manifest, expected digest, output target, and
all parameter values. At execution time the wrapper rebuilds the command from
the current no-follow manifest/source and refuses any drift. The resident
worker keeps its ordinary lease, attempt, failure, and settlement rules.

This boundary grants only execution of that checked observation. It does not
authorize GitHub mutation, browser messages, builds/tests, cleanup, schedule
creation, retries after ambiguous effects, or subsequent work based on the
result. Output interpretation and the next consequential action remain with
the owning workflow.

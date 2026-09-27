# Mechanism routing

Read this only after the admission gate in `SKILL.md` passes. Pick one path;
do not preload every linked contract.

| Need | Use |
| --- | --- |
| Known bounded command | direct tool |
| Unknown/noisy command | `lazy run` or `scripts/bounded_command.py` |
| Exact future observation | `lazy defer`; resume with `lazy worker`/`lazy tick` |
| Queue health | `lazy status` |
| Unknown browser/tool read | `lazy observe` |
| Browser SDK result | `scripts/browser_result_view.py` |
| Source/tree search | `scripts/safe_search.py` |
| Long Markdown / JSON | `scripts/context_view.py` / `scripts/json_view.py` |
| Repository-owned observation | `scripts/owner_profiles.py` |
| Codex activity / rollover | `scripts/codex_activity_view.py`; `scripts/compaction_thread_listener.py`; then the compaction advisor |
| Codex `exec --json` | Cultist `scripts/codex_exec_event_view.py` |
| Repeated Git/GitHub/Codex exec/test/build noise | installed hook; `lazy run -- COMMAND ARG...` when hooks are unavailable |
| Routing and capacity | `lazy route`; `lazy plan-lanes`; `lazy plan-controls` |
| Deferred carrier effect | `carrier_operation.py gate` |
| Multi-owner state | `lazy campaign`; accept only when required |
| Worker/carrier settlement | `lazy settle-worker` / `lazy settle-route` |

Use argv, exact `cwd`, hashes, generations, and private outputs. Never shell
sleep. Never mix observation and effect. Workspace hooks stay task-local and
decline approval-bearing sessions. For command admission, owner profiles,
carrier controls, or campaign recovery, follow the matching reference linked
from `SKILL.md`.

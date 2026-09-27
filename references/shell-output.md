# Shell output

`lc 'COMMAND'` captures one execution; `-C PATH` selects another directory. Install the user hook
and alias with `lazy-command install --user`; use `--workspace PATH` for an isolated install.

## Local notation

The header carries status and receipt ID once. Routine sizes and counts remain in `receipt.json`.
`FAIL N` is a nonzero exit; `OK` is exit zero. Diagnostic text never overrides the exit status.
Shared structure is defined inside each displayed block:

```text
prefix "/repo/src/worker.py:"
+ "81:    execute(job)"
+ "82:    settle(result)"
+ "83:    return result"
```

Append each JSON suffix to the JSON prefix to recover the original row. `[repeat ×N]` gives the
preceding line's total count; `repeat ×N · K lines` repeats the next K lines N times. Blocks preserve
order, indentation, and counts and are selected whole. Definitions never carry into another block.
Notation-shaped source rows are escaped as `literal "original row"`. Expand prefix/repeat blocks,
then decode one literal layer; recovered source text is never interpreted again.
Successful unittest progress shows pass/skip/expected-failure counts; raw output retains their order.

## Repeated observations

`lc --since ID 'SAME COMMAND'` validates a settled baseline with identical command text and directory,
executes once, and returns the command's exit status. It is not a cache or permission to repeat an effect.
Unchanged output becomes one line; changes use a bounded diff whose local header names both sides.
Each run retains its full ordinary view and raw output. `lc compare BEFORE AFTER` compares stored
receipts without execution. Large comparisons explicitly defer to stored expansion.

## Expansion

Omission hints lead to stored searches or line ranges. Narrow there before byte paging; do not
rerun a command just to see more of its output. Large whole JSON documents shed layout whitespace
only when every value then fits in the ordinary view; strings, key order, duplicate keys, and number
spellings stay exact.

- `lc s ID`: ordinary view.
- `lc s ID --lines START:END`: inclusive numbered lines.
- `lc s ID --find TEXT --context N`: literal UTF-8 matches in captured bytes and surrounding lines.
- `lc s -r ID --offset 0 --limit 7000`: bounded byte page; the next offset appears on stderr.

Focused views preserve indentation and blank lines, merge overlapping context, and state gaps and
next narrowing. Oversized lines point to byte recovery. Bare `lc s -r ID` emits all captured bytes;
use it only when the size is known safe or directing output to a file. Expansion never reruns commands.

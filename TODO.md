# TODO

Backlog for rig. `self.rig.yaml` takes the first unchecked item unless told otherwise:

```bash
uv run rig -f self.rig.yaml run --worktree "Do the next item in TODO.md"
```

Keep items small enough for one reviewed change. Add context under an item when it helps.

- [x] **`edit_file` tool**: replace an exact string in a file (`path`, `old`, `new`,
  optional `replace_all`), failing if `old` is missing or ambiguous. Hands currently
  rewrite whole files with `write_file`, which costs tokens and risks clobbering
  unrelated lines. Add it to BUILTIN_TOOLS, document it, and give it to coder hands in
  the templates and `self.rig.yaml`.
- [x] **Shift cost summary**: total input/output tokens per shift and an estimated USD
  cost (per-model prices in one table, unknown models shown as "n/a"). Print it at the
  end of `rig run` and store it in shift.json; show it in `rig logs`.
  Note: turns served by a server-side fallback model are priced at the hand's configured model.
- [ ] **`rig logs` shows the branch** for `--worktree` shifts (from shift.json).
- [ ] **Configurable ignore list** for `glob`/`search`: `search.ignore` in rig.yaml adds
  to or replaces the built-in IGNORED_DIRS (some projects keep sources in `build/`).
- [ ] **`.gitattributes`** so text files are committed with LF (silences CRLF warnings).

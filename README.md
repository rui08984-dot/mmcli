# mmcli

**An unofficial command-line client for the ModMind bridge.**

ModMind is a desktop workspace for building Minecraft mods. It exposes a local HTTP
bridge for external agents, and ships a stdio MCP server that wraps it. That wrapper
needs an MCP host; this is the same bridge, driven straight from a shell.

> Not affiliated with or endorsed by the ModMind project. `ModMind` is their name, not
> mine — this is an independent client for their publicly documented bridge contract.
> See [docs/bridge-contract.md](https://github.com/waterpail114514/ModMind-MCP/blob/main/docs/bridge-contract.md).

---

## Why this exists

The bridge contract is small: `POST http://127.0.0.1:<port>/tool` with an
`x-modmind-token` header and a `{action, input}` body. Driving it directly buys three
things the MCP path cannot:

1. **No per-call process.** The stdio adapter is spawned per MCP session by whatever
   host is driving it. A CLI call is one HTTP request and zero processes.
2. **Diagnosable failures.** Three failure modes account for nearly every confusing
   error in practice, and each gets a sentence saying what to do:

   | Symptom | What it actually means |
   |---|---|
   | `bridge.json` missing | No bridge is running. `mmcli project bind` starts one. |
   | HTTP 404 | The token was regenerated — `bridge.json` is left over from an earlier session. |
   | Connection refused | The bridge stopped; the file on disk is stale. |

3. **Scriptability.** `--json`, meaningful exit codes, and `--dry-run`.

## Requirements

- Python **3.10+** — standard library only, nothing to install.
- ModMind **1.4.x** (developed against 1.4.19 on Windows).

## Quick start

```bash
python mmcli.py init                    # write a config template next to the script
python mmcli.py project bind <project>  # bind ModMind to a project and start the bridge
python mmcli.py doctor                  # check what is actually going on
python mmcli.py list                    # the 98 tools the bridge exposes
```

`project bind` runs the documented startup command,
`ModMind.exe --mcp-bridge --project <project>`; if ModMind is already running the
request is forwarded to that instance.

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Processes, ports, bridge health, locks, and environment traps |
| `project bind / unbind / status` | Start and stop a bridge for a project |
| `list` / `describe` / `actions --verify` | The tool table, a tool's schema, and a check that the table still matches the installed server |
| `call <tool>` | Any of the 98 tools: `--json`, `--dry-run`, `--out`, `--yes` |
| `import-geo` / `import-anim` | Import Bedrock geometry and animation through the Blockbench bridge |
| `capture` / `bind-texture` / `state` | Render offscreen views, inject a texture, read the model state |
| `instance list / sync` | Install a built jar into a game instance, with a backup first |
| `launch` | Start a real game instance without a launcher GUI |
| `verify` | build → install → launch → screenshot, in one command |
| `reap` / `lock` | Clean up leaked forwarder processes and stale locks |

Run `python mmcli.py <command> --help` for the details of any one of them.

## Configuration

Everything machine-specific is empty by default; copy `mmcli.config.example.json` to
`mmcli.config.json` and fill in what you need. Only the first two are needed for most
work:

| Key | Meaning |
|---|---|
| `project_root` | The ModMind project directory. `bridge.json` is discovered under it. |
| `modmind_exe` | Path to `ModMind.exe`. Auto-detected from the usual install locations when empty. |
| `allowed_roots` | Opt-in: refuse file paths outside these roots. |
| `expect_namespace` | Opt-in: refuse write actions unless the bridge reports this namespace. Catches a bridge bound to the wrong project. |
| `require_files` | Opt-in: sentinel files that must exist in the project. |
| `versions_root`, `libraries_root`, `assets_root` | Only for `launch` / `instance` / `verify`. |
| `pcl_dir` | Optional. A launcher's directory, used to cross-check the generated launch command. |
| `build_libs` | Optional. Where `instance sync` looks for the jar to install. |
| `state_dir` | Where locks, logs and back-ups live. Defaults to `%LOCALAPPDATA%/mmcli`. |

`expect_namespace` plus `require_files` are worth setting: a bridge silently bound to
the wrong directory produces builds that appear to succeed and contain nothing.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Usage or configuration error |
| 3 | Bridge unavailable (missing, stale, or unreachable) |
| 4 | Project binding check failed |
| 5 | The action itself failed |
| 6 | Timed out waiting for the write lock |
| 7 | A guard refused the request |

## Tests

```bash
python mmcli_selftest.py        # 133 offline checks; needs no project and no bridge
python mmcli_mutation_test.py   # proves the selftest actually catches regressions
```

The selftest stands up a fake bridge in a temporary directory, so it runs on a bare
checkout. The mutation test plants known-bad behaviour and fails if the selftest stays
green — a check that cannot fail is not a check.

## Honest limits

- **Windows-first.** The core (bridge calls, project binding, model import) is pure
  Python and portable, but `shot` shells out to PowerShell and the process helpers use
  `taskkill`. On other platforms those commands say so rather than pretending.
- **UV mapping cannot be set** through the bridge's action set — no action writes
  `box_uv`/`uv_offset`. `import-geo` imports geometry, hierarchy, pivots, rotations and
  textures, and gets per-face UV in by creating the model with the `bedrock_block`
  format; `mmcli why-uv` prints the evidence.
- **The path guard is a heuristic, not a sandbox.** It checks parameters whose names
  look like paths. It reduces mistakes; it is not a security boundary. The bridge's own
  validation is.
- **The tool table is a snapshot.** `actions --verify` re-checks it against the
  installed server, so drift is detectable rather than silent.

## License

AGPL-3.0-only, matching the upstream project this talks to. See [LICENSE](LICENSE).

The tool list in `audit/tool_names.json` is derived from ModMind's published MCP
server. `ModMind` and its logo belong to the ModMind project; this repository is an
independent client and claims no affiliation.

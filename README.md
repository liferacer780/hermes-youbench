# 🏁 YouBench: benchmark models on *your* work

Every leaderboard tests someone else's tasks. **YouBench replays your own real Hermes tasks on
any models you choose and tells you which one is actually best for you, and what it costs.**

1. **Save** a task you just did with Hermes: `/youbench save --check "pytest -q"`
2. **Run** it on other models: `/youbench run anthropic/claude-sonnet-4.6 nousresearch/hermes-4-70b`
3. **Read the leaderboard**: pass rate, quality, cost per task, time and steps, live in the
   **YouBench** page of Hermes Desktop.

Example output (illustrative numbers):

```
| # | Model                       | Passed     | Quality | Cost / task | Avg time | Avg steps |
|---|-----------------------------|------------|---------|-------------|----------|-----------|
| 1 | nousresearch/hermes-4-70b   | 5/5 (100%) | 8.8/10  | $0.0041     | 38s      | 6         |
| 2 | anthropic/claude-sonnet-4.6 | 5/5 (100%) | 9.2/10  | $0.0610     | 41s      | 5         |
| 3 | some/cheap-model            | 2/5 (40%)  | 4.1/10  | $0.0009     | 22s      | 9         |

Best value: nousresearch/hermes-4-70b, within 10 points of the best at $0.0041 per task.
Switching would cut cost per task by about 93%. Use /model nousresearch/hermes-4-70b to switch.
```

## Why it needs a plugin (not a prompt or a skill)

* **Exact starting state.** A `pre_llm_call` hook snapshots your git workspace *before* the agent
  touches it, so a replay starts from precisely the files the original run saw, including
  uncommitted and untracked changes.
* **Isolated replays.** Each model gets its own unpacked copy of the project. A `pre_tool_call`
  guard pins every replay's file and terminal calls inside that copy, so replays can never
  edit your real repository.
* **Real execution, same safety.** Replays are Hermes subagents launched through the public
  subagent lifecycle API (the same path as `delegate_task`), in parallel, with Hermes's normal
  approvals, credentials, budgets and cost accounting.
* **Objective grading.** Your own check command (`pytest -q`, `npm test`, …) runs in each
  sandbox and decides pass/fail. Without one, a judge compares the replay's change to the
  change you accepted.
* **Real costs.** Tokens, cost and steps come from Hermes's own session records for each replay.

## Install

```bash
hermes plugins install youbench        # from the catalog, once listed
hermes plugins enable youbench
```

Or from this repository:

```bash
git clone https://github.com/liferacer780/hermes-youbench ~/.hermes/plugins/youbench
hermes plugins enable youbench
```

Restart Hermes. In **Hermes Desktop**, turn on **YouBench** under *Capabilities → Plugins* to get
the **YouBench** page in the sidebar and a live status-bar chip while a benchmark runs.

Requirements: Hermes Agent ≥ 0.23.0 and `git` on your PATH. Tasks must run inside a git
repository (any folder with `git init` works).

## Use

| Command | What it does |
|---|---|
| `/youbench save [name] [--check "<cmd>"]` | Save the task Hermes just finished as a benchmark case |
| `/youbench list` | List saved cases |
| `/youbench check <case> <cmd>` | Set or change the test command for a case |
| `/youbench run <model> [<model> …]` | Replay every case on these models and rank them |
| `/youbench report [run]` | Show the latest leaderboard |
| `/youbench delete <case>` | Remove a case |
| `hermes youbench list \| report [--html] \| delete <case>` | Same from a terminal |

You can also just ask: *"Which of these models is best for my work: X, Y, Z?"* Hermes calls the
`youbench_run` and `youbench_report` tools.

Model ids are whatever your provider expects (OpenRouter ids such as
`nousresearch/hermes-4-70b`). Use `model@provider` to replay on another configured provider.

### Settings (Desktop → Settings → Plugins → YouBench)

| Setting | Default | Meaning |
|---|---|---|
| `auto_capture` | `true` | Snapshot git workspaces before each turn so tasks can be saved |
| `max_snapshot_mb` | `50` | Largest workspace (compressed) that can be saved as a case |
| `default_models` | `[]` | Models used when `/youbench run` names none |
| `parallel` | `4` | Replays running at once (Hermes caps lifecycle children at 8) |

## Disclosure

* **Paid model calls.** Each replay is a full agent run on the model you choose, billed by that
  provider like any Hermes task (cases × models runs). Judge calls (when a case has no check
  command, or to score quality) use your active model through `ctx.llm`.
* **Shell commands.** A case's check command runs in each replay's sandbox through Hermes's
  `terminal` tool, so your approval settings apply. Replays themselves run terminal and file
  tools inside their sandbox.
* **Reads outside its own data.** Reads your git workspace to snapshot it (writing only
  unreferenced git objects, never touching your index, branches, stash or refs), and reads
  Hermes's `state.db` read-only for each replay's cost, tokens and tool counts.
* **Storage.** Saved tasks (prompt + compressed starting files + the reference diff), replay
  sandboxes and reports live under `<HERMES_HOME>/plugin-data/youbench/`. Delete a case with
  `/youbench delete`. Replay sandboxes can be removed by deleting `plugin-data/youbench/runs/`.
* **No network calls of its own, no telemetry, no stored credentials, no self-updater.**

## How it works

```
pre_llm_call ──► snapshot workspace tree (temp index, no side effects) ─┐
post_llm_call ─► mark turn finished                                     │
/youbench save ─► archive start tree + reference diff + original cost ◄─┘
youbench_run ──► for each case × model:
                   unpack sandbox ─► ctx.subagent_lifecycle.launch(model=…)
                   subagent_start ─► map child session → sandbox
                   pre_tool_call ──► pin paths/workdir to the sandbox
                   finished ──────► diff ─► check command ─► judge ─► state.db usage
               ──► leaderboard (chat + Desktop page + HTML report)
```

## Development

```bash
ln -s "$PWD" ~/.hermes/plugins/youbench
hermes plugins enable youbench
hermes plugins validate . --install-deps
cd tests && python -m pytest -q -p no:cacheprovider --import-mode=importlib --rootdir=. -c /dev/null
```

MIT licensed.

"""Tool schemas: what the model reads to decide when to call YouBench."""

YOUBENCH_RUN = {
    "name": "youbench_run",
    "description": (
        "Benchmark models on the user's OWN saved tasks. Replays every saved YouBench case (real tasks "
        "the user did earlier, from their exact starting files) on each model, in isolated sandboxes, "
        "grades each attempt (the user's check command or a judge), and returns a leaderboard of pass "
        "rate, quality, cost per task, time and steps. Use when the user asks which model is best/cheapest "
        "for their work, wants to compare models, or says 'run youbench'. Waits up to wait_seconds; if "
        "replays are still running, call youbench_report with the returned run_id to collect the rest."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "models": {
                "type": "array", "items": {"type": "string"},
                "description": ("Model ids exactly as the user's provider expects (e.g. 'anthropic/claude-sonnet-4.6', "
                                "'nousresearch/hermes-4-70b'). Append '@provider' to use another configured provider. "
                                "Omit to use the configured default_models."),
            },
            "cases": {
                "type": "array", "items": {"type": "string"},
                "description": "Case ids or names to replay. Omit to replay all saved cases.",
            },
            "limit": {"type": "integer", "minimum": 1, "description": "Replay at most this many cases (newest first)."},
            "wait_seconds": {"type": "integer", "minimum": 10, "maximum": 540, "default": 300,
                             "description": "How long to wait for results before returning partial progress."},
        },
        "required": [],
    },
}

YOUBENCH_REPORT = {
    "name": "youbench_report",
    "description": (
        "Collect and show a YouBench leaderboard. Keeps launching and grading queued replays of a running "
        "benchmark until wait_seconds elapse. Call repeatedly until it says the run is complete, then "
        "present the leaderboard table to the user exactly as returned and give one recommendation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Run id from youbench_run. Omit for the latest run."},
            "wait_seconds": {"type": "integer", "minimum": 5, "maximum": 540, "default": 240},
        },
        "required": [],
    },
}

YOUBENCH_CASES = {
    "name": "youbench_cases",
    "description": "List the user's saved YouBench cases (their real past tasks that models can be benchmarked on).",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

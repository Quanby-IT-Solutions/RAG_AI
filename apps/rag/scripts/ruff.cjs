#!/usr/bin/env node
// Turbo forwards eslint/prettier-specific pass-through args (--cache,
// --cache-location, --write) to every workspace lint/format script. ruff
// rejects unknown flags, so this shim runs a fixed ruff command per mode and
// ignores anything Turbo appended.
const { spawnSync } = require("node:child_process")

const commands = {
	check: "uv run ruff check app tests",
	format: "uv run ruff format --check app tests",
	"format:fix": "uv run ruff format app tests",
}

const command = commands[process.argv[2]]
if (!command) {
	console.error(`Unknown ruff shim mode: ${process.argv[2]}`)
	process.exit(1)
}

const result = spawnSync(command, { stdio: "inherit", shell: true })
process.exit(result.status ?? 1)

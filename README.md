# mKBI — mini Kernel Bound Intelligence

A small, hackable LLM agent framework. mKBI accepts natural language requests, routes them through a two-stage LLM pipeline,
optionally runs static analysis on the output, and executes the result in a subprocess, returning the output to the initial caller.
Skills are simple and easy to define: pick an execution method, give some examples, and you're all set.

---

## Table of Contents

- [Overview](#overview)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running from the CLI](#running-from-the-cli)
- [Skills](#skills)
  - [Skill File Format](#skill-file-format)
  - [Meta Block](#meta-block)
  - [Interpreter and Fabricator History](#interpreter-and-fabricator-history)
  - [Writing a New Skill](#writing-a-new-skill)
  - [Supported Executors](#supported-executors)
  - [Static Analysis](#static-analysis)
- [HTTP API](#http-api)
  - [Running the API Server](#running-the-api-server)
  - [Authentication](#authentication)
  - [Endpoints](#endpoints)
  - [Request and Response Schemas](#request-and-response-schemas)
  - [Examples](#examples)

---

## Overview

mKBI runs a two-model execution chain for every task:

1. **Interpreter** — receives the user's natural language request along with the skill's interpreter history (system prompt and few-shot examples). Produces code that addresses the request.
2. **Fabricator** — receives a combined `[KCR]` prompt containing the original request and the Interpreter's output. Cleans, validates, and formats the code into a directly executable script. Prepends the appropriate shebang.
3. **Static analysis** (optional) — runs a configured linter on the generated script. If it reports errors, execution is aborted.
4. **Execution** — runs the script in a subprocess with a configurable timeout. The process runs in its own session group so it can be cleanly killed on timeout.

---

## Project Structure

```
mKBI/
├── mKBI.py           # Core service: LLMService class and execution pipeline
├── mKBI_api.py       # FastAPI wrapper exposing the pipeline as an HTTP service
├── mKBI.toml         # Configuration file
└── skills/
    ├── bash.json     # Bash skill (default)
    └── python.json   # Python skill
```

---

## Installation

Python 3.11 or later is required.

```bash
pip install openai fastapi uvicorn python-dotenv
```

`shellcheck` is required for static analysis of bash skills. Install it via your system package manager (`apt install shellcheck`, `brew install shellcheck`, etc.). If it is not present on `PATH`, the analysis step is skipped with a warning and execution continues.

Clone the repo and place your API key either in `mKBI.toml` or as an environment variable:

```bash
git clone https://github.com/cbigger/mKBI.git
cd mKBI
export LLM_API_KEY=your_key_here
```

---

## Configuration

All configuration lives in `mKBI.toml`. The path can be overridden via the `MKBI_CONFIG` environment variable.

```toml
[api]
key = "YOUR_API_KEY_HERE"   # or leave as placeholder and use LLM_API_KEY env var
base_url = "https://openrouter.ai/api/v1"

[skills]
dir = "skills"              # path to the skills directory, relative to cwd

[service]
model = "openrouter/auto"   # model string passed to the API
default_skill = "bash"      # skill used by /execute and /interpret (backward compat)

[interpreter]
temperature = 0.7
top_p = 1.0
context_length = 4000

[fabricator]
temperature = 1.0
top_p = 0.4
context_length = 16000

[execution]
timeout = 30                # seconds before the subprocess is killed
```

**API key resolution order:**
1. `[api] key` in `mKBI.toml`, if it is not the placeholder string
2. `LLM_API_KEY` environment variable
3. `LLM_API_KEY` in a `.env` file in the working directory
4. Fatal error

The `base_url` can point to any OpenAI-compatible endpoint. OpenRouter is the default, but you can point it directly at the OpenAI API, a local Ollama instance, or any other compatible provider.

---

## Running from the CLI

```bash
# Run a task using the default skill
python3 mKBI.py "list all running processes sorted by memory"

# Run a task using a specific skill
python3 mKBI.py --skill python "what is today's date and time"

# Return only stdout (useful for piping)
python3 mKBI.py --output-only "show disk usage for the home directory"

# Both flags together
python3 mKBI.py --skill python --output-only "calculate the first 20 fibonacci numbers"
```

The full result dict is printed by default. With `--output-only`, only stdout from the executed script is printed.

---

## Skills

A skill is a single JSON file in the skills directory. The filename stem is the skill name (`bash.json` -> `bash`, `python.json` -> `python`). All skills are loaded at startup. New skills are picked up by restarting the process or calling `POST /skills/reload` via the API.

### Skill File Format

```json
{
  "meta": {
    "executor": "bash",
    "file_extension": ".sh",
    "static_analysis": "shellcheck"
  },
  "interpreter": [
    { "role": "system", "content": "..." },
    { "role": "user",   "content": "..." },
    { "role": "assistant", "content": "..." }
  ],
  "fabricator": [
    { "role": "system", "content": "..." },
    { "role": "user",   "content": "..." },
    { "role": "assistant", "content": "..." }
  ]
}
```

### Meta Block

| Field | Type | Description |
|---|---|---|
| `executor` | string | The runtime used to execute the generated script. See supported executors below. |
| `file_extension` | string | Extension for the temp file written before execution (e.g. `.sh`, `.py`). |
| `static_analysis` | string or null | Linter to run before execution. `null` skips analysis. |

### Interpreter and Fabricator History

Both `interpreter` and `fabricator` arrays are prepended to every API call made by their respective stage. They serve two purposes:

- The `system` role message defines the agent's persona and constraints for that skill.
- The subsequent `user` / `assistant` pairs are few-shot examples that teach the model the expected input/output format.

The Fabricator receives a combined prompt of the form:

```
<original user request> [KCR] <interpreter output>
```

The `[KCR]` tag (KBI Code Requisition) is the handoff signal. The Fabricator's examples should use this same format in their `user` messages so the model learns to expect it.

### Writing a New Skill

1. Create a new file in the `skills/` directory, e.g. `skills/node.json`.
2. Fill in the `meta` block with the appropriate executor, extension, and analysis tool (or `null`).
3. Write a `system` message for the Interpreter that defines its language specialty and any constraints (e.g. "use only the standard library").
4. Write a `system` message for the Fabricator that instructs it to output a clean, directly executable script with the correct shebang. Explicitly tell it to produce no markdown fencing or explanation.
5. Add at least four or five `user` / `assistant` few-shot pairs to each history array. Good examples cover the range of tasks the skill is likely to handle. The Fabricator examples should show improvement over the raw Interpreter output — correcting style, adding error handling, formatting output.

A minimal new skill looks like this:

```json
{
  "meta": {
    "executor": "node",
    "file_extension": ".js",
    "static_analysis": null
  },
  "interpreter": [
    {
      "role": "system",
      "content": "You are a KBI Interpreter unit specializing in Node.js. When a user requests a task, respond with Node.js code using only built-in modules. You are not responsible for returning code output to the user directly."
    }
  ],
  "fabricator": [
    {
      "role": "system",
      "content": "You are a KBI Fabricator unit. You receive [KCR] prompts and produce clean, executable Node.js scripts. Output only the raw script with no markdown fencing. Add console.log() for plain text requests."
    },
    {
      "role": "user",
      "content": "What Node.js version is running? [KCR] console.log(process.version)"
    },
    {
      "role": "assistant",
      "content": "console.log('Node.js version:', process.version);"
    }
  ]
}
```

### Supported Executors

The following executor names are recognised out of the box:

| Name | Command |
|---|---|
| `bash` | `bash {script}` |
| `python3` | `python3 {script}` |
| `python` | `python {script}` |
| `node` | `node {script}` |
| `ruby` | `ruby {script}` |
| `perl` | `perl {script}` |

To add a new executor, add an entry to `_EXECUTOR_CMDS` in `mKBI.py`:

```python
_EXECUTOR_CMDS: dict[str, list[str]] = {
    ...
    "deno": ["deno", "run", "{script}"],
}
```

### Static Analysis

The following analysis tools are recognised:

| Name | Command |
|---|---|
| `shellcheck` | `shellcheck {script}` |

If the named binary is not found on `PATH`, the step is skipped with a warning and execution proceeds. If analysis runs and reports errors, execution is aborted and the error output is returned in the result.

To add a new analysis tool, add an entry to `_STATIC_ANALYSIS_CMDS` in `mKBI.py`:

```python
_STATIC_ANALYSIS_CMDS: dict[str, list[str]] = {
    ...
    "pyflakes": ["pyflakes", "{script}"],
}
```

---

## HTTP API

### Running the API Server

```bash
uvicorn mKBI_api:app --host 0.0.0.0 --port 8000
```

Or directly:

```bash
python3 mKBI_api.py
```

The host and port can be configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `MKBI_CONFIG` | `mKBI.toml` | Path to the config file |
| `MKBI_HOST` | `0.0.0.0` | Bind address |
| `MKBI_PORT` | `8000` | Bind port |
| `MKBI_TOKEN` | *(unset)* | Bearer token for auth. If unset, auth is disabled. |

Interactive API docs are available at `http://localhost:8000/docs` once the server is running.

### Authentication

If `MKBI_TOKEN` is set, all `POST` endpoints require a bearer token:

```
Authorization: Bearer <your_token>
```

`GET` endpoints (`/health`, `/skills`) do not require authentication.

### Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | No | Liveness check. Returns model name, default skill, and uptime. |
| GET | `/skills` | No | List all loaded skills with their executor and analysis tool. |
| POST | `/skills/reload` | Yes | Rescan the skills directory and reload all skill definitions. |
| POST | `/skills/{skill}/execute` | Yes | Run the full pipeline for a named skill. |
| POST | `/skills/{skill}/interpret` | Yes | Run the Interpreter stage only for a named skill. No execution. |
| POST | `/execute` | Yes | Full pipeline using `default_skill` from config. |
| POST | `/interpret` | Yes | Interpreter only using `default_skill` from config. |

### Request and Response Schemas

**POST /skills/{skill}/execute** and **POST /execute**

Request body:
```json
{
  "request": "list files in the current directory sorted by size",
  "output_only": false
}
```

`output_only: true` returns a stripped response containing only stdout. Useful when the caller only needs the script's output and not pipeline metadata.

Full response (`output_only: false`):
```json
{
  "skill": "bash",
  "interpreter_response": "ls -lS .",
  "fabricator_response": "#!/bin/bash\nls -lhS .",
  "script": "#!/bin/bash\nls -lhS .",
  "shellcheck_passed": true,
  "shellcheck_output": "",
  "execution": {
    "stdout": "total 48\n-rw-r--r-- 1 user user 12400 ...",
    "stderr": "",
    "returncode": 0,
    "timed_out": false
  },
  "error": null,
  "elapsed_seconds": 2.341
}
```

Output-only response (`output_only: true`):
```json
{
  "output": "total 48\n-rw-r--r-- 1 user user 12400 ...",
  "elapsed_seconds": 2.341
}
```

**POST /skills/{skill}/interpret** and **POST /interpret**

Request body:
```json
{
  "request": "how do I list hidden files?"
}
```

Response:
```json
{
  "response": "ls -a ~/",
  "elapsed_seconds": 0.812
}
```

**GET /skills**

Response:
```json
[
  { "name": "bash",   "executor": "bash",    "analysis": "shellcheck" },
  { "name": "python", "executor": "python3", "analysis": null }
]
```

**GET /health**

Response:
```json
{
  "status": "ok",
  "model": "openrouter/auto",
  "default_skill": "bash",
  "uptime_seconds": 142.5
}
```

### Examples

```bash
# Health check
curl http://localhost:8000/health

# List skills
curl http://localhost:8000/skills

# Execute a bash task
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{"request": "show disk usage for the home directory"}'

# Execute using the python skill explicitly
curl -X POST http://localhost:8000/skills/python/execute \
  -H "Content-Type: application/json" \
  -d '{"request": "print the first 10 prime numbers"}'

# Get only stdout
curl -X POST http://localhost:8000/skills/python/execute \
  -H "Content-Type: application/json" \
  -d '{"request": "what is todays date", "output_only": true}'

# Interpret only (no execution)
curl -X POST http://localhost:8000/interpret \
  -H "Content-Type: application/json" \
  -d '{"request": "check if nginx is running"}'

# Reload skills after adding a new skill file (with auth enabled)
curl -X POST http://localhost:8000/skills/reload \
  -H "Authorization: Bearer your_token_here"
```

#!/usr/bin/env python3
"""
(mini) Kernel Bound Intelligence
Miniature task execution system

Configuration is loaded from a TOML file (default: mKBI.toml).
API key resolution order:
  1. TOML config file (if not placeholder)
  2. Environment variable LLM_API_KEY
  3. .env file (LLM_API_KEY)
  4. Bail with error

Conversation history is loaded from a JSON file specified by
config [history] path. The file must contain a top-level object
with "interpreter" and/or "fabricator" keys, each holding a list
of {role, content} messages. These are prepended to every call
made by the respective method.

execute_task() runs the full dual-model chain:
  user request -> Interpreter -> Fabricator -> shellcheck -> subprocess
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from dotenv import load_dotenv
import openai


_PLACEHOLDER = "YOUR_API_KEY_HERE"


def load_config(config_path: str = "mKBI.toml") -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"[mKBI] Config file not found: {config_path}")
        sys.exit(1)
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_api_key(config: dict) -> str:
    # 1. Config file
    key = config.get("api", {}).get("key", "")
    if key and key != _PLACEHOLDER:
        return key

    # 2. Environment / .env
    load_dotenv()
    key = os.getenv("LLM_API_KEY", "")
    if key:
        print("[mKBI] LLM_API_KEY found in environment.")
        return key

    print("[mKBI] No valid API key found. Set LLM_API_KEY in environment or provide it in the config file.")
    sys.exit(1)


def make_client(config: dict) -> openai.OpenAI:
    return openai.OpenAI(
        api_key=resolve_api_key(config),
        base_url=config["api"]["base_url"],
    )


def load_history(config: dict) -> dict:
    """
    Load conversation history from the JSON file specified in [history] path.
    Returns a dict with "interpreter" and "fabricator" keys (each a list of
    messages, defaulting to empty list if the key is absent).
    If no history path is configured or the file is missing, returns empty lists.
    """
    history_path = config.get("history", {}).get("path", "")
    if not history_path:
        return {"interpreter": [], "fabricator": []}

    path = Path(history_path)
    if not path.exists():
        print(f"[mKBI] History file not found: {history_path} — starting with empty history.")
        return {"interpreter": [], "fabricator": []}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        "interpreter": data.get("interpreter", []),
        "fabricator": data.get("fabricator", []),
    }



def _run_shellcheck(script_path: str) -> tuple[bool, str]:
    """
    Run shellcheck against script_path.
    Returns (passed: bool, output: str).
    If shellcheck is not installed, returns (True, warning_message) so execution
    can still proceed — the caller should surface the warning.
    """
    if not shutil.which("shellcheck"):
        return True, "[mKBI] shellcheck not found on PATH — skipping static analysis."

    result = subprocess.run(
        ["shellcheck", script_path],
        capture_output=True,
        text=True,
    )
    passed = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return passed, output


def _execute_script(script_path: str, timeout: int) -> dict:
    """
    Execute a shell script in a separate process group.
    Returns a dict with keys: stdout, stderr, returncode, timed_out.
    On timeout the process group is killed before returning.
    """
    try:
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,   # own process group — clean kill on timeout
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        # process group is automatically killed by Python after TimeoutExpired
        return {
            "stdout": (e.stdout or b"").decode(errors="replace"),
            "stderr": (e.stderr or b"").decode(errors="replace"),
            "returncode": None,
            "timed_out": True,
        }


class LLMService:
    """
    Unified service class. Loads all parameters from a TOML config.

    Public methods:
      create_chat(messages)  — Interpreter: problem-solving call
      fabricate(messages)    — Fabricator: code-hardening call
      execute_task(request)  — Full chain: Interpreter -> Fabricator -> shellcheck -> exec
    """

    def __init__(self, config_path: str = "mKBI.toml"):
        self.config = load_config(config_path)
        self.model = self.config["service"]["model"]
        self.hot_load = self.config["service"]["hot_load"]

        interp = self.config["interpreter"]
        self.interp_temperature = interp["temperature"]
        self.interp_top_p = interp["top_p"]
        self.interp_context_length = interp["context_length"]
        self.fab_driver = interp["fab_driver"]

        fab = self.config["fabricator"]
        self.fab_temperature = fab["temperature"]
        self.fab_top_p = fab["top_p"]
        self.fab_context_length = fab["context_length"]

        exec_cfg = self.config.get("execution", {})
        self.exec_timeout = exec_cfg.get("timeout", 30)

        history = load_history(self.config)
        self.interp_history: list = history["interpreter"]
        self.fab_history: list = history["fabricator"]

    def _call(self, messages: list, temperature: float, top_p: float) -> str:
        client = make_client(self.config)
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            stream=False,
        )
        try:
            return response.choices[0].message.content
        except KeyError:
            return ""
        except Exception as e:
            print(e)
            sys.exit(1)

    def create_chat(self, messages: list) -> str:
        full_messages = self.interp_history + messages
        return self._call(full_messages, self.interp_temperature, self.interp_top_p)

    def fabricate(self, messages: list) -> str:
        full_messages = self.fab_history + messages
        return self._call(full_messages, self.fab_temperature, self.fab_top_p)

    def execute_task(self, user_request: str, output_only: bool = False) -> dict | str:
        """
        Run the full dual-model execution chain for a user request.

        Stages:
          1. Interpreter  — reasons about the request, produces a bash solution
          2. Fabricator   — receives "<request> [KCR] <interpreter output>", validates
                            and formats the code into clean, executable bash
          3. shellcheck   — static analysis; aborts execution if errors are found
          4. subprocess   — executes the script in a separate process group

        If output_only=True, returns only the stdout of the execution (or the
        error string if the chain was aborted before reaching that stage).

        Otherwise returns a dict with keys:
          interpreter_response  str
          fabricator_response   str
          script                str
          shellcheck_passed     bool
          shellcheck_output     str
          execution             dict | None  (stdout, stderr, returncode, timed_out)
          error                 str | None   (set if the chain was aborted early)
        """
        result = {
            "interpreter_response": "",
            "fabricator_response": "",
            "script": "",
            "shellcheck_passed": False,
            "shellcheck_output": "",
            "execution": None,
            "error": None,
        }

        # --- Stage 1: Interpreter ---
        print(f"[mKBI] Interpreter processing request...")
        interp_response = self.create_chat([
            {"role": "user", "content": user_request}
        ])
        result["interpreter_response"] = interp_response
        print(f"[mKBI] Interpreter response:\n{interp_response}\n")

        # --- Stage 2: Fabricator ---
        # Format: <user request> [KCR] <interpreter response>
        kcr_message = f"{user_request} [KCR] {interp_response}"
#        print(f"[mKBI] Fabricator processing KCR...")
        fab_response = self.fabricate([
            {"role": "user", "content": kcr_message}
        ])
        result["fabricator_response"] = fab_response

        script = fab_response
        result["script"] = script
#        print(f"[mKBI] Fabricator script:\n{script}\n")

        if not script:
            result["error"] = "Fabricator returned an empty script."
            return result

        # --- Stage 3: shellcheck ---
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sh",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(script)
            tmp_path = tmp.name

        try:
            sc_passed, sc_output = _run_shellcheck(tmp_path)
            result["shellcheck_passed"] = sc_passed
            result["shellcheck_output"] = sc_output

            if not sc_passed:
                print(f"[mKBI] shellcheck failed:\n{sc_output}")
                result["error"] = "shellcheck reported errors — execution aborted."
                return result

            if sc_output:
                print(f"[mKBI] shellcheck: {sc_output}")

            # --- Stage 4: Execute ---
            print(f"[mKBI] Executing script (timeout={self.exec_timeout}s)...")
            exec_result = _execute_script(tmp_path, self.exec_timeout)
            result["execution"] = exec_result

            if exec_result["timed_out"]:
                result["error"] = f"Execution timed out after {self.exec_timeout}s."
                print(f"[mKBI] {result['error']}")
            else:
                print(f"[mKBI] Execution complete (rc={exec_result['returncode']})")

        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if output_only:
            if result["error"]:
                return result["error"]
            exec_result = result["execution"]
            return exec_result["stdout"] if exec_result else ""

        return result


if __name__ == "__main__":
    import pprint

    args = sys.argv[1:]
    output_only = "--output-only" in args
    if output_only:
        args = [a for a in args if a != "--output-only"]

    request = " ".join(args) if args else "Open firefox to redddit pls"

    print(f"[mKBI] Task: {request}\n")
    service = LLMService()
    outcome = service.execute_task(request, output_only=output_only)

    if output_only:
        print(outcome)
    else:
        print("\n--- RESULT ---")
        pprint.pprint(outcome)

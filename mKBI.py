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

Skills are loaded from the directory specified by config [skills] dir.
Each .json file in that directory is a named skill (filename stem).
Skills must have the structure:
  {
    "meta": {
      "executor":        "<bash|python3|...>",
      "file_extension":  "<.sh|.py|...>",
      "static_analysis": "<shellcheck|null>"
    },
    "interpreter": [ {role, content}, ... ],
    "fabricator":  [ {role, content}, ... ]
  }

execute_task() runs the full dual-model chain:
  user request -> Interpreter -> Fabricator -> static analysis -> subprocess
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


# ---------------------------------------------------------------------------
# Config / key / client
# ---------------------------------------------------------------------------

def load_config(config_path: str = "mKBI.toml") -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"[mKBI] Config file not found: {config_path}")
        sys.exit(1)
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_api_key(config: dict) -> str:
    key = config.get("api", {}).get("key", "")
    if key and key != _PLACEHOLDER:
        return key
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


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def load_skills(config: dict) -> dict[str, dict]:
    """
    Scan the skills directory and load every .json file as a named skill.
    Returns a dict mapping skill name -> skill data dict.
    Each skill dict has keys: meta, interpreter, fabricator.
    """
    skills_dir = Path(config.get("skills", {}).get("dir", "skills"))
    if not skills_dir.exists():
        print(f"[mKBI] Skills directory not found: {skills_dir}")
        sys.exit(1)

    skills: dict[str, dict] = {}
    for skill_file in sorted(skills_dir.glob("*.json")):
        name = skill_file.stem
        with open(skill_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        skills[name] = {
            "meta":        data.get("meta", {}),
            "interpreter": data.get("interpreter", []),
            "fabricator":  data.get("fabricator", []),
        }
        print(f"[mKBI] Loaded skill: {name} (executor={data.get('meta', {}).get('executor', 'unknown')})")

    if not skills:
        print(f"[mKBI] No skill files found in {skills_dir}")
        sys.exit(1)

    return skills


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

_STATIC_ANALYSIS_CMDS: dict[str, list[str]] = {
    "shellcheck": ["shellcheck", "{script}"],
}


def _run_static_analysis(tool: str | None, script_path: str) -> tuple[bool, str]:
    if not tool:
        return True, ""

    cmd_template = _STATIC_ANALYSIS_CMDS.get(tool)
    if cmd_template is None:
        return True, f"[mKBI] Unknown static analysis tool '{tool}' — skipping."

    cmd = [part.replace("{script}", script_path) for part in cmd_template]
    binary = cmd[0]

    if not shutil.which(binary):
        return True, f"[mKBI] {binary} not found on PATH — skipping static analysis."

    result = subprocess.run(cmd, capture_output=True, text=True)
    passed = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return passed, output


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_EXECUTOR_CMDS: dict[str, list[str]] = {
    "bash":    ["bash",    "{script}"],
    "python3": ["python3", "{script}"],
    "python":  ["python",  "{script}"],
    "node":    ["node",    "{script}"],
    "ruby":    ["ruby",    "{script}"],
    "perl":    ["perl",    "{script}"],
}


def _execute_script(executor: str, script_path: str, timeout: int) -> dict:
    cmd_template = _EXECUTOR_CMDS.get(executor)
    if cmd_template is None:
        return {
            "stdout": "",
            "stderr": f"Unknown executor: {executor}",
            "returncode": -1,
            "timed_out": False,
        }

    cmd = [part.replace("{script}", script_path) for part in cmd_template]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
        )
        return {
            "stdout":     result.stdout,
            "stderr":     result.stderr,
            "returncode": result.returncode,
            "timed_out":  False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout":     (e.stdout or b"").decode(errors="replace"),
            "stderr":     (e.stderr or b"").decode(errors="replace"),
            "returncode": None,
            "timed_out":  True,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class LLMService:
    """
    Unified service class. Loads all parameters from a TOML config.

    Public methods:
      list_skills()                        — names of registered skills
      reload_skills()                      — rescan skills directory
      create_chat(messages, skill)         — Interpreter call for a skill
      fabricate(messages, skill)           — Fabricator call for a skill
      execute_task(request, skill, ...)    — Full chain for a skill
    """

    def __init__(self, config_path: str = "mKBI.toml"):
        self.config = load_config(config_path)
        self.model = self.config["service"]["model"]
        self.default_skill = self.config["service"].get("default_skill", "bash")

        interp = self.config["interpreter"]
        self.interp_temperature = interp["temperature"]
        self.interp_top_p = interp["top_p"]
        self.interp_context_length = interp["context_length"]

        fab = self.config["fabricator"]
        self.fab_temperature = fab["temperature"]
        self.fab_top_p = fab["top_p"]
        self.fab_context_length = fab["context_length"]

        exec_cfg = self.config.get("execution", {})
        self.exec_timeout = exec_cfg.get("timeout", 30)

        # API client is created once at init and kept in memory
        self.client = make_client(self.config)

        self.skills = load_skills(self.config)

    # ------------------------------------------------------------------
    # Skills management
    # ------------------------------------------------------------------

    def list_skills(self) -> list[dict]:
        return [
            {
                "name":      name,
                "executor":  skill["meta"].get("executor", "unknown"),
                "analysis":  skill["meta"].get("static_analysis"),
            }
            for name, skill in self.skills.items()
        ]

    def reload_skills(self) -> list[dict]:
        self.skills = load_skills(self.config)
        return self.list_skills()

    def _get_skill(self, skill_name: str) -> dict:
        if skill_name not in self.skills:
            raise ValueError(f"Unknown skill: '{skill_name}'. Available: {list(self.skills)}")
        return self.skills[skill_name]

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _call(self, messages: list, temperature: float, top_p: float) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            stream=False,
        )
        try:
            return response.choices[0].message.content
        except (KeyError, IndexError):
            return ""
        except Exception as e:
            print(e)
            sys.exit(1)

    def create_chat(self, messages: list, skill: str | None = None) -> str:
        skill_name = skill or self.default_skill
        history = self._get_skill(skill_name)["interpreter"]
        return self._call(history + messages, self.interp_temperature, self.interp_top_p)

    def fabricate(self, messages: list, skill: str | None = None) -> str:
        skill_name = skill or self.default_skill
        history = self._get_skill(skill_name)["fabricator"]
        return self._call(history + messages, self.fab_temperature, self.fab_top_p)

    # ------------------------------------------------------------------
    # Full execution chain
    # ------------------------------------------------------------------

    def execute_task(
        self,
        user_request: str,
        skill: str | None = None,
        output_only: bool = False,
    ) -> dict | str:
        """
        Run the full dual-model execution chain for a user request.

        Stages:
          1. Interpreter  — reasons about the request, produces code
          2. Fabricator   — validates and formats the code
          3. Static analysis — aborts if errors found (tool from skill meta)
          4. subprocess   — executes via the skill's executor

        Returns a result dict, or stdout string if output_only=True.
        """
        skill_name = skill or self.default_skill
        skill_data = self._get_skill(skill_name)
        meta = skill_data["meta"]
        executor = meta.get("executor", "bash")
        extension = meta.get("file_extension", ".sh")
        analysis_tool = meta.get("static_analysis")

        result = {
            "skill":                skill_name,
            "interpreter_response": "",
            "fabricator_response":  "",
            "script":               "",
            "shellcheck_passed":    False,
            "shellcheck_output":    "",
            "execution":            None,
            "error":                None,
        }

        # Stage 1: Interpreter
        print(f"[mKBI] Interpreter processing request (skill={skill_name})...")
        interp_response = self.create_chat(
            [{"role": "user", "content": user_request}],
            skill=skill_name,
        )
        result["interpreter_response"] = interp_response
        print(f"[mKBI] Interpreter response:\n{interp_response}\n")

        # Stage 2: Fabricator
        kcr_message = f"{user_request} [KCR] {interp_response}"
        fab_response = self.fabricate(
            [{"role": "user", "content": kcr_message}],
            skill=skill_name,
        )
        result["fabricator_response"] = fab_response
        result["script"] = fab_response

        if not fab_response:
            result["error"] = "Fabricator returned an empty script."
            return result["error"] if output_only else result

        # Stage 3: Static analysis
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=extension,
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(fab_response)
            tmp_path = tmp.name

        try:
            sc_passed, sc_output = _run_static_analysis(analysis_tool, tmp_path)
            result["shellcheck_passed"] = sc_passed
            result["shellcheck_output"] = sc_output

            if not sc_passed:
                print(f"[mKBI] Static analysis failed:\n{sc_output}")
                result["error"] = f"{analysis_tool} reported errors — execution aborted."
                return result["error"] if output_only else result

            if sc_output:
                print(f"[mKBI] Static analysis: {sc_output}")

            # Stage 4: Execute
            print(f"[mKBI] Executing script (executor={executor}, timeout={self.exec_timeout}s)...")
            exec_result = _execute_script(executor, tmp_path, self.exec_timeout)
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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    args = sys.argv[1:]
    output_only = "--output-only" in args
    if output_only:
        args = [a for a in args if a != "--output-only"]

    skill_arg = None
    if "--skill" in args:
        idx = args.index("--skill")
        skill_arg = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    request = " ".join(args) if args else "Open firefox to reddit pls"

    print(f"[mKBI] Task: {request}\n")
    service = LLMService()
    outcome = service.execute_task(request, skill=skill_arg, output_only=output_only)

    if output_only:
        print(outcome)
    else:
        print("\n--- RESULT ---")
        pprint.pprint(outcome)

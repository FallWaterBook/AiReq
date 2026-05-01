import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Job

logger = logging.getLogger("jobs")

TEST_COMMAND = getattr(settings, "AIREQ_TEST_COMMAND", [sys.executable, "manage.py", "check"])
CODEX_TEMPLATE_PLACEHOLDERS = (
    "{{TARGET_APP}}",
    "{{ALLOWED_FILES}}",
    "{{FORBIDDEN_FILES}}",
    "{{AS_IS}}",
    "{{TO_BE_REQUIRED}}",
    "{{TO_BE_OPTIONAL}}",
    "{{WHY}}",
    "{{TARGET_FILES_SOURCE_CODE}}",
)
AUTO_SOURCE_EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "staticfiles",
    "media",
}
AUTO_SOURCE_EXCLUDED_FILE_NAMES = {"db.sqlite3"}
AUTO_SOURCE_MAX_FILE_CHARS = 100000
AUTO_SOURCE_MAX_TOTAL_CHARS = 60000
AUTO_SOURCE_MAX_FILES = 5


def load_codex_rules() -> str:
    rules_path = Path(settings.CODEX_RULES_PATH)
    try:
        rules_text = rules_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to read CODEX_RULES.md: %s", rules_path)
        raise RuntimeError(f"failed to read rules file: {rules_path}") from exc

    if not rules_text.strip():
        logger.error("CODEX_RULES.md is empty: %s", rules_path)
        raise RuntimeError("rules file is empty")

    return rules_text


def load_codex_task_template() -> str:
    template_path = Path(settings.CODEX_TASK_TEMPLATE_PATH)
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to read CODEX_TASK_TEMPLATE.md: %s", template_path)
        raise RuntimeError(f"failed to read template file: {template_path}") from exc

    if not template_text.strip():
        logger.error("CODEX_TASK_TEMPLATE.md is empty: %s", template_path)
        raise RuntimeError("template file is empty")

    return template_text


def build_codex_prompt(
    user_prompt: str,
    target_files_source_code: str,
) -> str:
    try:
        template_text = load_codex_task_template()
        source_code_placeholder = "{{TARGET_FILES_SOURCE_CODE}}"

        context = {
            "{{TARGET_APP}}": "AiReq",
            "{{ALLOWED_FILES}}": "\n".join(
                [
                    "- You may edit any file under TARGET_REPO_DIR.",
                    "- Do not edit files outside the repository.",
                    "- Do not edit system directories such as .git, venv, node_modules, __pycache__.",
                    "- If the user specifies target files, prioritize them.",
                    "- Limit output to at most 3 files.",
                ]
            ),
            "{{FORBIDDEN_FILES}}": "- .venv/\n- db.sqlite3",
            "{{AS_IS}}": "POST /jobs receives a user request and can update up to three related files.",
            "{{TO_BE_REQUIRED}}": user_prompt,
            "{{TO_BE_OPTIONAL}}": "- Keep existing logging and error string style ([error] ...).",
            "{{WHY}}": user_prompt,
        }

        rendered = template_text
        for key, value in context.items():
            rendered = rendered.replace(key, value)

        unresolved_check_targets = tuple(
            token for token in CODEX_TEMPLATE_PLACEHOLDERS
            if token != source_code_placeholder
        )
        unresolved = [token for token in unresolved_check_targets if token in rendered]
        if unresolved:
            logger.error("Unresolved template placeholders: %s", unresolved)
            raise RuntimeError(f"unresolved placeholders: {', '.join(unresolved)}")

        rendered = rendered.replace(source_code_placeholder, target_files_source_code)

        return rendered
    except Exception:
        logger.exception("Failed to build codex prompt")
        raise


def _is_collectible_source_file(path: Path, base_dir: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.name in AUTO_SOURCE_EXCLUDED_FILE_NAMES:
        return False
    rel_parts = path.relative_to(base_dir).parts
    if any(part in AUTO_SOURCE_EXCLUDED_DIR_NAMES for part in rel_parts):
        return False
    return True


def _sanitize_relative_file_path(candidate: str, base_dir: Path) -> str | None:
    raw = (candidate or "").strip()
    if not raw:
        return None
    normalized = raw.replace("\\", "/").strip("/")
    if not normalized:
        return None
    path_obj = Path(normalized)
    if path_obj.is_absolute():
        return None
    if ".." in path_obj.parts:
        return None

    resolved = (base_dir / path_obj).resolve()
    if base_dir not in resolved.parents and resolved != base_dir:
        return None
    if not _is_collectible_source_file(resolved, base_dir):
        return None
    return str(resolved.relative_to(base_dir)).replace("\\", "/")


def extract_file_paths_from_prompt(prompt: str) -> list[str]:
    text = prompt or ""
    if not text.strip():
        return []

    base_dir = Path(settings.TARGET_REPO_DIR).resolve()
    found: list[str] = []
    seen: set[str] = set()

    # path-like tokens containing "/" or "\"
    candidates = re.findall(r"[a-zA-Z0-9_\-./\\]+", text)
    for token in candidates:
        normalized = _sanitize_relative_file_path(token, base_dir)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)
        if len(found) >= AUTO_SOURCE_MAX_FILES:
            break

    return found


def get_git_changed_file_paths() -> list[str]:
    base_dir = Path(settings.TARGET_REPO_DIR).resolve()
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(base_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if completed.returncode != 0:
            return []
    except Exception:
        logger.exception("Failed to list git changed files")
        return []

    found: list[str] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        normalized = _sanitize_relative_file_path(line.strip(), base_dir)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)
        if len(found) >= AUTO_SOURCE_MAX_FILES:
            break
    return found


def build_auto_target_files_source_code(prompt: str) -> str:
    base_dir = Path(settings.TARGET_REPO_DIR).resolve()
    selected_paths: list[str] = []
    seen: set[str] = set()

    def add_path(rel_path: str) -> bool:
        if rel_path in seen:
            return False
        seen.add(rel_path)
        selected_paths.append(rel_path)
        return len(selected_paths) < AUTO_SOURCE_MAX_FILES

    for rel_path in extract_file_paths_from_prompt(prompt):
        if not add_path(rel_path):
            break

    if len(selected_paths) < AUTO_SOURCE_MAX_FILES:
        for rel_path in get_git_changed_file_paths():
            if not add_path(rel_path):
                break

    sections: list[str] = []
    total_chars = 0
    for rel_path in selected_paths:
        abs_path = (base_dir / rel_path).resolve()
        if not _is_collectible_source_file(abs_path, base_dir):
            continue
        try:
            content = abs_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to read auto source file: %s", abs_path)
            continue

        if len(content) > AUTO_SOURCE_MAX_FILE_CHARS:
            continue
        if total_chars + len(content) > AUTO_SOURCE_MAX_TOTAL_CHARS:
            break

        sections.append("\n".join([f"## FILE: {rel_path}", "", content]))
        total_chars += len(content)

    return "\n\n---\n\n".join(sections)


def parse_ai_files_output(output_text: str) -> list[dict]:
    normalized = (output_text or "").strip()
    if not normalized:
        raise RuntimeError("OpenAI response was empty")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")

    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid AI output JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("AI output must be a JSON object")

    files = payload.get("files")
    if not isinstance(files, list):
        raise RuntimeError("AI output files must be a list")
    if len(files) > 3:
        raise RuntimeError("AI output files exceeds max size: 3")

    validated: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("each file entry must be an object")
        if set(item.keys()) != {"path", "content"}:
            raise RuntimeError("each file entry must contain only path and content")

        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise RuntimeError("path and content must be strings")

        validated.append({"path": path, "content": content})

    return validated


def validate_ai_file_path(path: str) -> Path:
    candidate = (path or "").strip()
    if not candidate:
        raise RuntimeError("path is required")

    path_obj = Path(candidate)
    if path_obj.is_absolute():
        raise RuntimeError(f"absolute path is not allowed: {candidate}")
    if ".." in path_obj.parts:
        raise RuntimeError(f"parent path is not allowed: {candidate}")
    resolved = (Path(settings.TARGET_REPO_DIR) / path_obj).resolve()
    base_dir = Path(settings.TARGET_REPO_DIR).resolve()
    if base_dir not in resolved.parents and resolved != base_dir:
        raise RuntimeError(f"path must be under BASE_DIR: {candidate}")

    return resolved


def apply_ai_files(files: list[dict]) -> dict:
    if not files:
        return {"success": False, "error": "no files to apply", "applied_files": []}

    original_sources: dict[Path, str] = {}
    path_map: list[tuple[Path, str]] = []

    try:
        for item in files:
            file_path = validate_ai_file_path(item["path"])
            content = item["content"]
            if not content or not content.strip():
                raise RuntimeError(f"content is empty: {item['path']}")
            if len(content.strip()) < 20:
                raise RuntimeError(f"content too small: {item['path']}")
            if len(content) > 100000:
                raise RuntimeError(f"content too large: {item['path']}")

            if file_path in original_sources:
                raise RuntimeError(f"duplicate file path in output: {item['path']}")

            if file_path.exists():
                original_sources[file_path] = file_path.read_text(encoding="utf-8")
            else:
                original_sources[file_path] = None
            if file_path.suffix == ".py":
                try:
                    compile(content, str(file_path), "exec")
                except Exception as exc:
                    raise RuntimeError(f"invalid python code for {item['path']}: {exc}") from exc

            path_map.append((file_path, content))

        for file_path, content in path_map:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        for file_path, _ in path_map:
            if file_path.suffix != ".py":
                continue
            compile_result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(file_path)],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            if compile_result.returncode != 0:
                message = (
                    compile_result.stderr.strip()
                    or compile_result.stdout.strip()
                    or "py_compile failed"
                )
                raise RuntimeError(f"py_compile failed for {file_path}: {message}")

        return {
            "success": True,
            "error": "",
            "applied_files": [
                str(path.relative_to(Path(settings.TARGET_REPO_DIR))) for path, _ in path_map
            ],
        }
    except Exception as exc:
        logger.exception("Failed to apply AI files atomically")
        for file_path, source in original_sources.items():
            try:
                if source is None:
                    file_path.unlink(missing_ok=True)
                else:
                    file_path.write_text(source, encoding="utf-8")
            except Exception:
                logger.exception("Failed to rollback file: %s", file_path)

        return {"success": False, "error": str(exc), "applied_files": []}


def compress_rules(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("-")
            or "禁止" in stripped
            or "Do not" in stripped
            or "must" in stripped.lower()
        ):
            lines.append(stripped)
    return "\n".join(lines)


def build_ai_input(
    codex_rules_text: str,
    codex_task_prompt: str,
    target_files_source_code: str,
    optional_contexts: list[str] | None = None,
) -> str:
    sections = [
        "You are a senior Python engineer.",
        "Follow CODEX_RULES strictly.",
        "Return ONLY valid JSON object output.",
        "Do not add explanations.",
        "Do not output markdown.",
        "Do not output diff.",
        "",
        "## CODEX_RULES",
        codex_rules_text,
        "",
        "## CODEX_TASK",
        codex_task_prompt,
        "",
        "## TARGET_FILES_SOURCE_CODE",
        target_files_source_code,
    ]

    contexts = [c for c in (optional_contexts or []) if c and c.strip()]
    if contexts:
        sections.extend(["", "## OPTIONAL_CONTEXTS", "\n\n".join(contexts)])

    return "\n".join(sections)


def run_ai_openai(ai_input: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    if not api_key:
        logger.error("OPENAI_API_KEY is not set")
        return "[error] OPENAI_API_KEY is not set"

    logger.info("OpenAI JSON generation start: model=%s", model)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model,
            input=ai_input,
        )

        output_text = getattr(response, "output_text", "") or ""
        files = parse_ai_files_output(output_text)
        apply_result = apply_ai_files(files)
        if not apply_result.get("success"):
            message = apply_result.get("error") or "failed to apply AI files"
            logger.error("apply_ai_files failed: %s", message)
            return f"[error] {message}"

        applied_files = apply_result.get("applied_files", [])
        logger.info("OpenAI JSON applied successfully: files=%s", applied_files)
        return "applied files: " + ", ".join(applied_files)
    except Exception as exc:
        logger.exception("OpenAI JSON apply flow failed")
        return f"[error] OpenAI API call failed: {exc}"


def run_ai_fix_loop(prompt: str, max_attempts: int = 3) -> dict:
    attempts_limit = max(1, min(max_attempts, 3))
    attempts: list[dict] = []
    optional_contexts: list[str] = []

    for attempt in range(1, attempts_limit + 1):
        codex_rules_text = compress_rules(load_codex_rules())
        target_files_source_code = build_auto_target_files_source_code(prompt)
        target_files_source_code = (
            target_files_source_code.strip()
            or "## NOTE\nNo existing source files provided. Create new files as needed."
        )
        codex_task_prompt = build_codex_prompt(prompt, target_files_source_code)
        ai_input = build_ai_input(
            codex_rules_text=codex_rules_text,
            codex_task_prompt=codex_task_prompt,
            target_files_source_code=target_files_source_code,
            optional_contexts=optional_contexts,
        )
        ai_result = run_ai_openai(ai_input)

        attempt_result: dict = {
            "attempt": attempt,
            "ai_result": ai_result,
        }
        attempts.append(attempt_result)

        if ai_result.startswith("[error]"):
            attempt_result["test"] = None
            break

        test_result = run_tests()
        attempt_result["test"] = test_result
        if test_result.get("success"):
            return {"success": True, "attempts": attempts}

        optional_contexts = [
            "\n".join(
                [
                    "Previous attempt failed.",
                    f"Command: {test_result.get('command', '')}",
                    "",
                    f"Return code: {test_result.get('returncode', '')}",
                    "",
                    f"STDOUT: {test_result.get('stdout', '')}",
                    "",
                    f"STDERR: {test_result.get('stderr', '')}",
                    "",
                    "Fix the failure while preserving the original user request.",
                ]
            )
        ]

    return {"success": False, "attempts": attempts}


def read_prompt(request: HttpRequest) -> str:
    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
        return (body.get("prompt") or "").strip()
    return (request.POST.get("prompt") or "").strip()


def is_json_request(request: HttpRequest) -> bool:
    accept = request.headers.get("Accept", "")
    return "application/json" in accept or (
        request.content_type and "application/json" in request.content_type
    )


def run_git_diff() -> tuple[dict | None, str | None]:
    target_dir = Path(settings.TARGET_REPO_DIR).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        return None, f"directory not found: {settings.TARGET_REPO_DIR}"

    diff_completed = subprocess.run(
        ["git", "diff", "--no-color"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if diff_completed.returncode != 0:
        return None, diff_completed.stderr.strip() or "git diff failed"

    stat_completed = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if stat_completed.returncode != 0:
        return None, stat_completed.stderr.strip() or "git diff --stat failed"

    changed_files_completed = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if changed_files_completed.returncode != 0:
        return None, changed_files_completed.stderr.strip() or "git diff --name-only failed"

    diff_text = diff_completed.stdout
    return {
        "directory": str(target_dir),
        "diff": diff_text,
        "is_clean": not diff_text.strip(),
        "stat": stat_completed.stdout,
        "changed_files": changed_files_completed.stdout,
    }, None


def run_tests() -> dict:
    command_parts = list(TEST_COMMAND)
    command_str = " ".join(command_parts)
    try:
        completed = subprocess.run(
            command_parts,
            cwd=str(Path(settings.TARGET_REPO_DIR)),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return {
            "success": completed.returncode == 0,
            "command": command_str,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        }
    except Exception as exc:
        logger.exception("Failed to run tests")
        return {
            "success": False,
            "command": command_str,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
        }


def get_git_diff_stat() -> dict:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "diff", "--stat"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        stdout = completed.stdout
        return {
            "success": completed.returncode == 0,
            "stdout": stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "has_diff": bool(stdout.strip()),
        }
    except Exception as exc:
        logger.exception("Failed to get git diff stat")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "has_diff": False,
        }


def git_commit(commit_message: str) -> dict:
    message = (commit_message or "").strip()
    if not message:
        return {
            "success": False,
            "stdout": "",
            "stderr": "commit_message is required",
            "returncode": 1,
            "committed": False,
        }

    diff_stat = get_git_diff_stat()
    if not diff_stat["success"]:
        return {
            "success": False,
            "stdout": diff_stat["stdout"],
            "stderr": diff_stat["stderr"],
            "returncode": diff_stat["returncode"],
            "committed": False,
        }

    if not diff_stat["has_diff"]:
        return {
            "success": False,
            "stdout": diff_stat["stdout"],
            "stderr": "no diff to commit",
            "returncode": 1,
            "committed": False,
        }

    try:
        add_result = subprocess.run(
            ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "add", "-A"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if add_result.returncode != 0:
            return {
                "success": False,
                "stdout": add_result.stdout,
                "stderr": add_result.stderr,
                "returncode": add_result.returncode,
                "committed": False,
            }

        commit_result = subprocess.run(
            ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "commit", "-m", message],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return {
            "success": commit_result.returncode == 0,
            "stdout": commit_result.stdout,
            "stderr": commit_result.stderr,
            "returncode": commit_result.returncode,
            "committed": commit_result.returncode == 0,
        }
    except Exception as exc:
        logger.exception("git_commit failed")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "committed": False,
        }


def get_current_branch() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if completed.returncode != 0:
            logger.error("Failed to get current branch: %s", completed.stderr)
            return ""
        return completed.stdout.strip()
    except Exception:
        logger.exception("Exception while getting current branch")
        return ""


def git_push() -> dict:
    branch = get_current_branch()
    is_protect_branch_push = getattr(settings, "PROTECTED_BRANCHES_PUSH", True)
    protect＿branch = getattr(settings, "PROTECTED_BRANCHES", {"main", "master", "develop"})

    if not branch:
        return {
            "success": False,
            "stdout": "",
            "stderr": "failed to detect current branch",
            "returncode": 1,
            "branch": "",
        }

    if is_protect_branch_push in True and branch in protect＿branch:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"push to protected branch is blocked: {branch}",
            "returncode": 1,
            "branch": branch,
        }

    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "push"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return {
            "success": completed.returncode == 0,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "branch": branch,
        }
    except Exception as exc:
        logger.exception("git_push failed")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "branch": branch,
        }


def validate_branch_name(branch_name: str) -> str | None:
    name = (branch_name or "").strip()
    if not name:
        return "invalid branch_name: empty"
    if name.startswith("/"):
        return "invalid branch_name: must not start with '/'"
    if name.startswith("-"):
        return "invalid branch_name: must not start with '-'"
    if ".." in name:
        return "invalid branch_name: must not contain '..'"
    if "\\" in name:
        return "invalid branch_name: must not contain '\\'"
    if name.endswith("/"):
        return "invalid branch_name: must not end with '/'"
    if name.endswith(".lock"):
        return "invalid branch_name: must not end with '.lock'"
    return None


def git_checkout_branch(branch_name: str, create: bool = False) -> dict:
    name = (branch_name or "").strip()
    validation_error = validate_branch_name(name)
    if validation_error:
        return {
            "success": False,
            "stdout": "",
            "stderr": validation_error,
            "returncode": 1,
            "branch": "",
        }
    try:
        command = ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "switch"]
        if create:
            command.append("-c")
        command.append(name)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if (
            create
            and completed.returncode != 0
            and "already exists" in (completed.stderr or "").lower()
        ):
            completed = subprocess.run(
                ["git", "-C", str(Path(settings.TARGET_REPO_DIR)), "switch", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        return {
            "success": completed.returncode == 0,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "branch": name,
        }
    except Exception as exc:
        logger.exception("git_checkout_branch failed")
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "branch": name,
        }


def parse_json_body(request: HttpRequest) -> dict:
    try:
        return json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["GET", "POST"])
def jobs_view(request: HttpRequest):
    if request.method == "GET":
        latest_job = Job.objects.order_by("-id").first()
        return render(
            request,
            "jobs/form.html",
            {
                "prompt": latest_job.prompt if latest_job else "",
                "result": latest_job.result if latest_job else None,
                "job": latest_job,
                "git_directory": "",
                "git_diff": None,
                "git_diff_stat": "",
                "git_changed_files": "",
                "error_message": None,
                "success_message": None,
                "current_branch": get_current_branch(),
            },
        )

    action = (request.POST.get("action") or "").strip()
    if action in {"switch_branch", "create_branch"}:
        latest_job = Job.objects.order_by("-id").first()
        latest_user_prompt = latest_job.prompt if latest_job else ""
        branch_name = (request.POST.get("branch_name") or "").strip()
        is_create = action == "create_branch"
        checkout_result = git_checkout_branch(branch_name, create=is_create)
        success = checkout_result.get("success")
        error_message = None if success else (checkout_result.get("stderr") or "git switch failed")
        success_message = (
            (
                f"新規ブランチを作成して切り替えました: {branch_name}"
                if is_create
                else f"既存ブランチへ切り替えました: {branch_name}"
            )
            if success
            else None
        )
        result_text = (
            checkout_result.get("stdout")
            or checkout_result.get("stderr")
            or ""
        )
        status = 200 if success else 400
        return render(
            request,
            "jobs/form.html",
            {
                "prompt": latest_user_prompt,
                "result": result_text,
                "job": latest_job,
                "git_directory": "",
                "git_diff": None,
                "git_diff_stat": "",
                "git_changed_files": "",
                "error_message": error_message,
                "success_message": success_message,
                "current_branch": get_current_branch(),
            },
            status=status,
        )

    prompt = read_prompt(request)
    if not prompt:
        if is_json_request(request):
            return JsonResponse({"error": "prompt is required"}, status=400)
        return HttpResponseBadRequest("prompt is required")
    job = Job.objects.create(prompt=prompt, status=Job.STATUS_QUEUED)
    logger.info("Job queued: id=%s prompt=%s", job.id, prompt)

    job.status = Job.STATUS_RUNNING
    job.save(update_fields=["status", "updated_at"])
    logger.info("Job running: id=%s", job.id)

    try:
        codex_rules_text = compress_rules(load_codex_rules())
        target_files_source_code = build_auto_target_files_source_code(prompt)
        target_files_source_code = (
            target_files_source_code.strip()
            or "## NOTE\nNo existing source files provided. Create new files as needed."
        )
        codex_task_prompt = build_codex_prompt(prompt, target_files_source_code)
        ai_input = build_ai_input(
            codex_rules_text=codex_rules_text,
            codex_task_prompt=codex_task_prompt,
            target_files_source_code=target_files_source_code,
            optional_contexts=[],
        )
        result = run_ai_openai(ai_input)
    except Exception as exc:
        logger.exception("Failed before OpenAI execution")
        result = f"[error] Failed to build final codex prompt: {exc}"

    job.status = Job.STATUS_DONE
    job.result = result
    job.save(update_fields=["status", "result", "test_passed", "updated_at"])
    logger.info("Job done: id=%s", job.id)

    if is_json_request(request):
        return JsonResponse(
            {
                "id": job.id,
                "status": job.status,
                "prompt": job.prompt,
                "result": job.result,
                "test_passed": job.test_passed,
            }
        )

    return render(
        request,
        "jobs/form.html",
        {
            "prompt": prompt,
            "result": job.result,
            "job": job,
            "git_directory": "",
            "git_diff": None,
            "git_diff_stat": "",
            "git_changed_files": "",
            "error_message": None,
            "success_message": None,
            "current_branch": get_current_branch(),
        },
    )


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["POST"])
def git_diff_view(request: HttpRequest):
    diff_result, diff_error = run_git_diff()
    latest_job = Job.objects.order_by("-id").first()
    latest_user_prompt = latest_job.prompt if latest_job else ""

    if is_json_request(request):
        if diff_error:
            return JsonResponse({"error": diff_error}, status=400)
        return JsonResponse(diff_result)

    if diff_error:
        return render(
            request,
            "jobs/form.html",
            {
                "prompt": latest_user_prompt,
                "result": None,
                "job": latest_job,
                "git_directory": settings.TARGET_REPO_DIR,
                "git_diff": None,
                "git_diff_stat": "",
                "git_changed_files": "",
                "error_message": diff_error,
                "success_message": None,
                "current_branch": get_current_branch(),
            },
            status=400,
        )

    return render(
        request,
        "jobs/form.html",
        {
            "prompt": latest_user_prompt,
            "result": None,
            "job": latest_job,
            "git_directory": diff_result["directory"],
            "git_diff": diff_result["diff"],
            "git_diff_stat": diff_result["stat"],
            "git_changed_files": diff_result["changed_files"],
            "error_message": None,
            "success_message": "Git Diffを取得しました",
            "current_branch": get_current_branch(),
        },
    )


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["POST"])
def job_test_view(request: HttpRequest, job_id: int):
    job = get_object_or_404(Job, id=job_id)
    test_result = run_tests()
    job.test_passed = bool(test_result.get("success"))
    job.save(update_fields=["test_passed", "updated_at"])
    return JsonResponse(
        {
            "job_id": job_id,
            "test_passed": job.test_passed,
            "test": test_result,
        }
    )


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["POST"])
def job_commit_view(request: HttpRequest, job_id: int):
    job = get_object_or_404(Job, id=job_id)
    body = parse_json_body(request)
    commit_message = (body.get("commit_message") or "").strip()
    if not commit_message:
        return JsonResponse({"error": "commit_message is required"}, status=400)

    commit_result = git_commit(commit_message)
    status = 200 if commit_result.get("success") else 400

    return JsonResponse(
        {
            "job_id": job_id,
            "test_passed": job.test_passed,
            "commit": commit_result,
        },
        status=status,
    )


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["POST"])
def job_push_view(request: HttpRequest, job_id: int):
    job = get_object_or_404(Job, id=job_id)
    require_test = getattr(settings, "AIREQ_REQUIRE_TEST_BEFORE_PUSH", True)

    if require_test and not job.test_passed:
        return JsonResponse({"error": "test must pass before push"}, status=400)

    push_result = git_push()
    status = 200 if push_result.get("success") else 400
    return JsonResponse({"job_id": job_id, "push": push_result}, status=status)


@csrf_exempt
@require_http_methods(["POST"])
def job_auto_fix_view(request: HttpRequest, job_id: int):
    job = get_object_or_404(Job, id=job_id)

    job.status = Job.STATUS_RUNNING
    job.save(update_fields=["status", "updated_at"])

    try:
        loop_result = run_ai_fix_loop(job.prompt, max_attempts=3)
    except Exception as exc:
        logger.exception("Failed to run auto-fix loop")
        loop_result = {"success": False, "attempts": [], "error": str(exc)}
    job.status = Job.STATUS_DONE
    job.result = json.dumps(loop_result, ensure_ascii=False)
    job.test_passed = bool(loop_result.get("success"))
    job.save(update_fields=["status", "result", "test_passed", "updated_at"])

    return JsonResponse(
        {
            "job_id": job_id,
            "status": job.status,
            "test_passed": job.test_passed,
            "auto_fix": loop_result,
        }
    )


@require_http_methods(["GET"])
def job_detail_view(request: HttpRequest, job_id: int):
    job = get_object_or_404(Job, id=job_id)
    return JsonResponse(
        {
            "id": job.id,
            "status": job.status,
            "prompt": job.prompt,
            "result": job.result,
            "test_passed": job.test_passed,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
    )

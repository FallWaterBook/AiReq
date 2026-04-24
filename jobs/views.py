import json
import logging
import os
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
TARGET_PYTHON_FILE = settings.BASE_DIR / "jobs" / "ai_target.py"
PROTECTED_BRANCHES = {"main", "master", "develop"}
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

ALLOWED_AI_EDIT_FILES = (
    "jobs/ai_target.py",
    "jobs/views.py",
    "templates/jobs/form.html",
)


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


def build_codex_prompt(user_prompt: str, target_files_source_code: str) -> str:
    try:
        template_text = load_codex_task_template()
        source_code_placeholder = "{{TARGET_FILES_SOURCE_CODE}}"

        context = {
            "{{TARGET_APP}}": "AiReq",
            "{{ALLOWED_FILES}}": "\n".join(f"- {path}" for path in ALLOWED_AI_EDIT_FILES),
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


def read_target_source_code(target_file_path: Path) -> str:
    try:
        return target_file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.exception("Failed to read target source code: %s", target_file_path)
        raise RuntimeError(f"failed to read target source code: {target_file_path}") from exc


def build_target_files_source_code(file_paths: list[str]) -> str:
    sections: list[str] = []
    for file_path in file_paths[:3]:
        abs_path = Path(settings.TARGET_REPO_DIR) / file_path
        try:
            code = abs_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.exception("Failed to read target file source: %s", abs_path)
            raise RuntimeError(f"failed to read target file source: {file_path}") from exc

        language = "python" if file_path.endswith(".py") else "text"
        sections.append(
            "\n".join(
                [
                    f"## FILE: {file_path}",
                    "",
                    f"```{language}",
                    code,
                    "```",
                ]
            )
        )

    return "\n\n".join(sections)


def parse_ai_files_output(output_text: str) -> list[dict]:
    normalized = (output_text or "").strip()
    if not normalized:
        raise RuntimeError("OpenAI response was empty")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    fence = "```"
    if fence in normalized:
        start = normalized.find(fence)
        end = normalized.rfind(fence)
        if end > start:
            inner = normalized[start + len(fence):end]
            if "\n" in inner:
                first_line, rest = inner.split("\n", 1)
                language = first_line.strip().lower()
                if language in {"", "python", "py", "json"}:
                    inner = rest
            normalized = inner.strip()

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
    if candidate not in ALLOWED_AI_EDIT_FILES:
        raise RuntimeError(f"path is not allowed: {candidate}")

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
            if len(content) > 20000:
                raise RuntimeError(f"content too large: {item['path']}")

            if file_path in original_sources:
                raise RuntimeError(f"duplicate file path in output: {item['path']}")

            original_sources[file_path] = file_path.read_text(encoding="utf-8")
            if file_path.suffix == ".py":
                try:
                    compile(content, str(file_path), "exec")
                except Exception as exc:
                    raise RuntimeError(f"invalid python code for {item['path']}: {exc}") from exc

            path_map.append((file_path, content))

        for file_path, content in path_map:
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
        target_files_source_code = build_target_files_source_code(list(ALLOWED_AI_EDIT_FILES))
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


def read_directory(request: HttpRequest) -> str:
    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
        return (body.get("directory") or "").strip()
    return (request.POST.get("directory") or "").strip()


def is_json_request(request: HttpRequest) -> bool:
    accept = request.headers.get("Accept", "")
    return "application/json" in accept or (
        request.content_type and "application/json" in request.content_type
    )


def resolve_target_directory(raw_directory: str) -> Path:
    raw_path = Path(raw_directory).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (settings.BASE_DIR / raw_path).resolve()


def run_git_diff() -> tuple[dict | None, str | None]:
    target_dir = Path(settings.TARGET_REPO_DIR).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        return None, f"directory not found: {settings.TARGET_REPO_DIR}"

    completed = subprocess.run(
        ["git", "diff", "--no-color"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    if completed.returncode != 0:
        return None, completed.stderr.strip() or "git diff failed"

    diff_text = completed.stdout
    return {
        "directory": str(target_dir),
        "diff": diff_text,
        "is_clean": diff_text == "",
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
    if not branch:
        return {
            "success": False,
            "stdout": "",
            "stderr": "failed to detect current branch",
            "returncode": 1,
            "branch": "",
        }

    if branch in PROTECTED_BRANCHES:
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
        return render(
            request,
            "jobs/form.html",
            {
                "prompt": "",
                "result": None,
                "job": None,
                "git_directory": "",
                "git_diff": None,
                "git_error": None,
            },
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
        target_files_source_code = build_target_files_source_code(list(ALLOWED_AI_EDIT_FILES))
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
    job.test_passed = False
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
            "git_error": None,
        },
    )


# 開発用のため一時的に csrf_exempt。Tailscale 前提。将来は認証/CSRF保護へ移行予定。
@csrf_exempt
@require_http_methods(["POST"])
def git_diff_view(request: HttpRequest):
    result, error = run_git_diff()

    if is_json_request(request):
        if error:
            return JsonResponse({"error": error}, status=400)
        return JsonResponse(result)

    if error:
        return render(
            request,
            "jobs/form.html",
            {
                "prompt": "",
                "result": None,
                "job": None,
                "git_directory": settings.TARGET_REPO_DIR,
                "git_diff": None,
                "git_error": error,
            },
            status=400,
        )

    return render(
        request,
        "jobs/form.html",
        {
            "prompt": "",
            "result": None,
            "job": None,
            "git_directory": result["directory"],
            "git_diff": result["diff"],
            "git_error": None,
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

    # コード変更が確定するため、test_passed をリセットする
    # （テスト後にコードが変わった場合の不整合防止）
    job.test_passed = False
    job.save(update_fields=["test_passed", "updated_at"])

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

    if not job.test_passed:
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

    loop_result = run_ai_fix_loop(job.prompt, max_attempts=3)
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

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
)


def extract_unified_diff(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            body = "\n".join(lines[1:-1]).strip()
            if body.lower().startswith("diff"):
                body_lines = body.splitlines()
                body = "\n".join(body_lines[1:]).strip() if len(body_lines) > 1 else ""
            return body

    return stripped


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


def build_codex_prompt(user_prompt: str) -> str:
    try:
        template_text = load_codex_task_template()

        context = {
            "{{TARGET_APP}}": "AiReq",
            "{{ALLOWED_FILES}}": "- jobs/ai_target.py",
            "{{FORBIDDEN_FILES}}": "- .venv/\n- db.sqlite3",
            "{{AS_IS}}": "POST /jobs receives a user request and updates one target Python file.",
            "{{TO_BE_REQUIRED}}": user_prompt,
            "{{TO_BE_OPTIONAL}}": "- Keep existing logging and error string style ([error] ...).",
            "{{WHY}}": user_prompt,
        }

        rendered = template_text
        for key, value in context.items():
            rendered = rendered.replace(key, value)

        unresolved = [token for token in CODEX_TEMPLATE_PLACEHOLDERS if token in rendered]
        if unresolved:
            logger.error("Unresolved template placeholders: %s", unresolved)
            raise RuntimeError(f"unresolved placeholders: {', '.join(unresolved)}")

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
    target_file_path: Path,
    target_source_code: str,
    optional_contexts: list[str] | None = None,
) -> str:
    try:
        rel_path = target_file_path.relative_to(settings.BASE_DIR)
    except ValueError:
        rel_path = target_file_path
    sections = [
        "You are a senior Python engineer.",
        "Follow CODEX_RULES strictly.",
        "Return ONLY a valid unified diff patch.",
        "Do not modify unrelated code.",
        "Do not add explanations.",
        "Ensure the patch can be applied by git apply.",
        "Do not change file structure.",
        "Do not rename functions unless required.",
        "Preserve existing behavior unless explicitly instructed.",
        "",
        "## CODEX_RULES",
        codex_rules_text,
        "",
        "## CODEX_TASK",
        codex_task_prompt,
        "",
        "## TARGET_FILE_PATH",
        str(rel_path),
        "",
        "## TARGET_SOURCE_CODE",
        target_source_code,
    ]

    contexts = [c for c in (optional_contexts or []) if c and c.strip()]
    if contexts:
        sections.extend(["", "## OPTIONAL_CONTEXTS", "\n\n".join(contexts)])

    return "\n".join(sections)


def run_ai_openai(ai_input: str, target_file_path: Path) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    if not api_key:
        logger.error("OPENAI_API_KEY is not set")
        return "[error] OPENAI_API_KEY is not set"

    logger.info("OpenAI diff generation start: file=%s model=%s", target_file_path, model)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model,
            input=ai_input,
        )

        output_text = getattr(response, "output_text", "") or ""
        diff_text = extract_unified_diff(output_text)
        if not diff_text.strip():
            logger.error("OpenAI response diff was empty for file=%s", target_file_path)
            return "[error] OpenAI response diff was empty"

        check_result = subprocess.run(
            ["git", "-C", str(settings.BASE_DIR), "apply", "--check", "-"],
            input=diff_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if check_result.returncode != 0:
            message = check_result.stderr.strip() or check_result.stdout.strip() or "git apply --check failed"
            logger.error("git apply --check failed: %s", message)
            return f"[error] git apply check failed: {message}"

        apply_result = subprocess.run(
            ["git", "-C", str(settings.BASE_DIR), "apply", "-"],
            input=diff_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if apply_result.returncode != 0:
            message = apply_result.stderr.strip() or apply_result.stdout.strip() or "git apply failed"
            logger.error("git apply failed: %s", message)
            return f"[error] git apply failed: {message}"

        logger.info("OpenAI diff applied successfully: file=%s", target_file_path)
        return diff_text
    except Exception as exc:
        logger.exception("OpenAI diff apply flow failed for file=%s", target_file_path)
        return f"[error] OpenAI API call failed: {exc}"


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


def run_git_diff(raw_directory: str) -> tuple[dict | None, str | None]:
    target_dir = resolve_target_directory(raw_directory)
    if not target_dir.exists() or not target_dir.is_dir():
        return None, f"directory not found: {target_dir}"

    completed = subprocess.run(
        ["git", "-C", str(target_dir), "diff", "--no-color"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "git diff failed"
        return None, message

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
            cwd=str(settings.BASE_DIR),
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
            ["git", "-C", str(settings.BASE_DIR), "diff", "--stat"],
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
            ["git", "-C", str(settings.BASE_DIR), "add", "-A"],
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
            ["git", "-C", str(settings.BASE_DIR), "commit", "-m", message],
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
            ["git", "-C", str(settings.BASE_DIR), "rev-parse", "--abbrev-ref", "HEAD"],
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
            ["git", "-C", str(settings.BASE_DIR), "push"],
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
        codex_task_prompt = build_codex_prompt(prompt)
        target_file_path = Path(TARGET_PYTHON_FILE)
        target_source_code = read_target_source_code(target_file_path)
        ai_input = build_ai_input(
            codex_rules_text=codex_rules_text,
            codex_task_prompt=codex_task_prompt,
            target_file_path=target_file_path,
            target_source_code=target_source_code,
            optional_contexts=[],
        )
        result = run_ai_openai(ai_input, target_file_path)
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
    directory = read_directory(request)
    if not directory:
        if is_json_request(request):
            return JsonResponse({"error": "directory is required"}, status=400)
        return HttpResponseBadRequest("directory is required")

    result, error = run_git_diff(directory)

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
                "git_directory": directory,
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

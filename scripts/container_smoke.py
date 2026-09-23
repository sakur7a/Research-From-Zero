"""Build-result acceptance for the hosted container: HTTP task, export and ephemeral cold start.

Run after `docker build --tag re0:ci .`. It starts the unmodified production entrypoint once, then a
CI-only app launcher to inject the fake model while keeping the same FastAPI/SQLite/auth/task code.
Nothing here contacts a model provider or publishes a service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HOST = "re0-ci.onrender.com"
PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
SERVER_HARNESS = Path(__file__).with_name("container_smoke_server.py").resolve()


def docker(args, *, check=True, quiet=False):
    result = subprocess.run(["docker", *args], check=False, text=True,
                            stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
                            stderr=subprocess.DEVNULL if quiet else subprocess.PIPE)
    if check and result.returncode:
        # Do not echo environment arguments: this command line can contain the generated session secret.
        raise RuntimeError(f"docker {args[0]} failed with exit code {result.returncode}")
    return result


def request(path, *, method="GET", data=None, cookie="", host=HOST, timeout=5):
    headers = {"Host": host, "X-Re0-Client": "web"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    if cookie:
        # TestClient follows browser cookie semantics. This smoke sends the Secure cookie explicitly
        # over a loopback-only test port; the deployed Render endpoint remains HTTPS.
        headers["Cookie"] = f"re0_session={cookie}"
    req = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as response:
        return response.code, response.headers, response.read()


def json_request(path, *, method="GET", data=None, cookie="", expected=200):
    status, headers, body = request(path, method=method, data=data, cookie=cookie)
    decoded = json.loads(body) if body else None
    if status != expected:
        raise AssertionError(f"{method} {path}: expected {expected}, received {status}: {decoded}")
    return decoded, headers


def start(name, image, secret, *, harness=False):
    docker(["rm", "--force", name], check=False, quiet=True)
    args = ["run", "--detach", "--name", name,
            "--health-interval", "2s", "--health-start-period", "1s", "--health-retries", "5",
            "--publish", f"127.0.0.1:{PORT}:10000",
            "--env", "PORT=10000", "--env", "RE0_HOST=0.0.0.0",
            "--env", "RE0_MODE=hosted", "--env", "RE0_STORAGE_MODE=ephemeral-demo",
            "--env", "RE0_GUEST_ACCESS=true",
            "--env", f"RE0_SESSION_SECRET={secret}", "--env", "RENDER=true",
            "--env", f"RENDER_EXTERNAL_URL=https://{HOST}",
            "--env", f"RENDER_EXTERNAL_HOSTNAME={HOST}"]
    if harness:
        args.extend(["--mount", f"type=bind,source={SERVER_HARNESS},target=/tmp/re0-container-smoke-server.py,readonly"])
    args.append(image)
    if harness:
        args.append("python")
        args.append("/tmp/re0-container-smoke-server.py")
    docker(args)


def wait_for_health(timeout=60):
    end = time.monotonic() + timeout
    latest = "not started"
    while time.monotonic() < end:
        try:
            status, _, body = request("/api/health", timeout=2)
            latest = f"HTTP {status}: {body[:300].decode('utf-8', errors='replace')}"
            if status == 200:
                data = json.loads(body)
                if data.get("auth_required") is True and data.get("storage_mode") == "ephemeral-demo":
                    return data
        except (OSError, URLError, TimeoutError) as exc:
            latest = str(exc)
        time.sleep(0.5)
    raise AssertionError(f"container health did not become ready: {latest}")


def wait_for_container_health(name, timeout=30):
    end = time.monotonic() + timeout
    latest = "starting"
    while time.monotonic() < end:
        result = docker(["inspect", "--format", "{{.State.Health.Status}}", name])
        latest = result.stdout.strip()
        if latest == "healthy":
            return latest
        if latest == "unhealthy":
            raise AssertionError("Dockerfile healthcheck failed on the injected PORT")
        time.sleep(0.5)
    raise AssertionError(f"Dockerfile healthcheck did not pass: {latest}")


def start_guest():
    payload, headers = json_request("/api/auth/guest", method="POST", data={})
    if payload.get("identity", {}).get("kind") != "guest":
        raise AssertionError(f"guest entry did not create an isolated identity: {payload}")
    cookie = re.search(r"(?:^|;\s*)re0_session=([^;]+)", headers.get("Set-Cookie", ""))
    if not cookie:
        raise AssertionError("guest entry did not issue a session cookie")
    return cookie.group(1)


def run(image="re0:ci"):
    name = "re0-hosted-smoke"
    secret = secrets.token_urlsafe(48)
    checks = []
    try:
        # Exercise the image's production CMD and Docker health route, including platform PORT.
        start(name, image, secret)
        health = wait_for_health()
        image_health = wait_for_container_health(name)
        shell_status, _, shell_body = request("/login")
        if shell_status != 200:
            raise AssertionError(f"login shell returned HTTP {shell_status}")
        login_shell = shell_body.decode("utf-8", errors="replace")
        if 'id="storage-warning"' not in login_shell or "工作区文件" not in login_shell:
            raise AssertionError("the public login shell omitted the ephemeral data warning")
        for path in ("/static/login.js", "/static/login-core.js", "/static/login.css",
                     "/static/agent.js", "/static/skill.html"):
            status, _, asset = request(path)
            if status != 200 or not asset:
                raise AssertionError(f"container static resource failed: {path} returned HTTP {status}")
        public_session, _ = json_request("/api/auth/session")
        if public_session.get("deployment", {}).get("storage_mode") != "ephemeral-demo":
            raise AssertionError("the public session endpoint did not state its storage mode")
        status, _, _ = request("/api/health", host="attacker.invalid")
        if status != 400:
            raise AssertionError(f"untrusted Host returned {status}, expected 400")
        checks.append("production_entrypoint_platform_port_health_and_host_allowlist")
        docker(["rm", "--force", name], quiet=True)

        # Run the actual task loop in a clean container. Only the model object is a fixture.
        start(name, image, secret, harness=True)
        wait_for_health()
        cookie = start_guest()
        paper, _ = json_request("/api/papers", method="POST", cookie=cookie,
                                data={"title": "Container smoke fixture paper",
                                      "abstract": "Seeded CI metadata for the hosted container smoke."},
                                expected=201)
        config, _ = json_request("/api/agent/config", method="PUT", cookie=cookie,
                                 data={"base_url": "https://api.openai.com/v1", "model": "container-smoke",
                                       "api_key": "ci-fake-key-never-sent-to-a-provider",
                                       "trust_endpoint": True, "max_output_tokens": 512})
        if not config.get("configured") or "api_key" in config:
            raise AssertionError("model configuration response exposed or failed to keep the fixture credential")
        task, _ = json_request("/api/agent/runs", method="POST", cookie=cookie,
                               data={"goal": "Verify the seeded container smoke fixture", "use_library": True,
                                     "consent_to_send": True, "max_model_calls": 6, "max_tool_calls": 6,
                                     "attempt_seconds": 90}, expected=202)
        run_id = task["id"]
        statuses = [task.get("status", "")]
        end = time.monotonic() + 20
        while time.monotonic() < end:
            observed, _ = json_request(f"/api/agent/runs/{run_id}", cookie=cookie)
            statuses.append(observed["status"])
            if observed["status"] in {"completed", "failed", "cancelled", "budget_exhausted"}:
                task = observed
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"task did not stop before the smoke deadline: {statuses}")
        if task.get("status") != "completed":
            raise AssertionError(f"task did not complete: {task}")
        if not task.get("report", {}).get("findings") or not task.get("evidence"):
            raise AssertionError("the completed task did not retain its report and source evidence")
        if not {"queued", "running"}.intersection(statuses):
            raise AssertionError(f"the 202 task was never observed while active: {statuses}")
        exported_bytes = request(f"/api/agent/runs/{run_id}/export", cookie=cookie)[2]
        exported = json.loads(exported_bytes)
        if exported.get("status") != "completed" or not exported.get("report") or not exported.get("evidence"):
            raise AssertionError("the task export is missing the finished report or evidence")
        if "ci-fake-key-never-sent-to-a-provider" in exported_bytes.decode("utf-8"):
            raise AssertionError("the task export contains the configured API key")
        checks.append("202_poll_tool_loop_report_evidence_and_full_export")

        # Remove/recreate the container to model an ephemeral cold start. The old guest token must
        # not authenticate, and a fresh guest sees no old task; the export remains with the user.
        docker(["rm", "--force", name], quiet=True)
        start(name, image, secret, harness=True)
        wait_for_health()
        session, _ = json_request("/api/auth/session")
        if session.get("identity", {}).get("authenticated") or session.get("accounts_provisioned") != 0:
            raise AssertionError("ephemeral cold start unexpectedly retained account state")
        json_request("/api/agent/runs", cookie=cookie, expected=401)
        fresh_cookie = start_guest()
        runs, _ = json_request("/api/agent/runs", cookie=fresh_cookie)
        if runs:
            raise AssertionError("the fresh ephemeral instance exposed a task from the previous instance")
        checks.append("ephemeral_cold_start_invalidates_old_guest_and_task_state")

        result = {"commit": os.getenv("GITHUB_SHA", "local"),
                  "checks": checks, "task_statuses": statuses,
                  "task_model_calls": task.get("model_calls"), "task_tool_calls": task.get("tool_calls"),
                  "export_bytes": len(exported_bytes),
                  "export_retained_after_restart_on_client_side": bool(exported.get("report")),
                  "external_model_or_provider_calls": 0,
                  "storage_mode": health.get("storage_mode"), "docker_healthcheck": image_health}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception:
        logs = docker(["logs", name], check=False)
        if logs.stdout:
            print("--- container stdout ---\n" + logs.stdout)
        if logs.stderr:
            print("--- container stderr ---\n" + logs.stderr)
        raise
    finally:
        docker(["rm", "--force", name], check=False, quiet=True)


if __name__ == "__main__":
    run(os.getenv("RE0_SMOKE_IMAGE", "re0:ci"))

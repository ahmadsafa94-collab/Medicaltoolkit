"""
Minimal VMOS Cloud OpenAPI client: run one shell command on the cloud phone
and get its output back.

Auth is VMOS's "V2 signing" (Getting Started page of their OpenAPI docs):
three headers on every request --
  X-Access-Key  the AccessKeyID from the console (Developer -> API)
  X-Timestamp   unix seconds, 10 digits, within +-5 minutes of their clock
  X-Sign        lowercase hex SHA-256 of  SK + timestamp + path + body
where body is the exact JSON bytes sent (so it is serialized once, signed,
and sent as-is). No HMAC, no Authorization header.

Shell commands: /syncCmd returns the output in the same call and is tried
first. Their public docs only fully document /asyncCmd (output fetched by
polling /padTaskDetail), so if /syncCmd is refused the client switches to
that for the rest of the process. VMOS_CMD_MODE=sync|async pins one.
"""

import asyncio
import hashlib
import json
import logging
import time

import httpx

from config import VMOS_ACCESS_KEY, VMOS_API_BASE, VMOS_CMD_MODE, VMOS_PAD_CODE, VMOS_SECRET_KEY

logger = logging.getLogger(__name__)

_PREFIX = "/vcpcloud/api/padApi"
_HTTP_TIMEOUT = 45.0
_ASYNC_POLL_SECONDS = 0.8
_ASYNC_MAX_WAIT_SECONDS = 40.0

# taskStatus values from the padTaskDetail docs
_DONE = {3}
_FAILED = {-1, -2, -3, -4}

_mode = VMOS_CMD_MODE if VMOS_CMD_MODE in ("sync", "async") else "auto"


class VmosError(Exception):
    pass


def is_configured() -> bool:
    return bool(VMOS_ACCESS_KEY and VMOS_SECRET_KEY and VMOS_PAD_CODE)


def _sign(timestamp: str, path: str, body: str) -> str:
    return hashlib.sha256((VMOS_SECRET_KEY + timestamp + path + body).encode("utf-8")).hexdigest()


async def call(endpoint: str, payload: dict) -> dict:
    """POST one signed request; returns the parsed JSON's `data`, or raises VmosError."""
    path = f"{_PREFIX}/{endpoint}"
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Access-Key": VMOS_ACCESS_KEY,
        "X-Timestamp": ts,
        "X-Sign": _sign(ts, path, body),
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            resp = await http.post(VMOS_API_BASE.rstrip("/") + path, content=body.encode("utf-8"), headers=headers)
    except httpx.HTTPError as e:
        raise VmosError(f"network error: {type(e).__name__}")
    try:
        data = resp.json()
    except ValueError:
        raise VmosError(f"HTTP {resp.status_code}, non-JSON reply")
    code = data.get("code")
    if resp.status_code != 200 or code not in (200, 0, "200", "0"):
        raise VmosError(f"{endpoint}: code {code} {data.get('msg') or ''}".strip())
    return data.get("data")


def _first(data) -> dict:
    if isinstance(data, list):
        return data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _output_of(item: dict) -> str:
    # taskResult is where the output lands; errorMsg carries the message
    # when the command itself failed.
    out = item.get("taskResult")
    if out is None:
        out = item.get("taskContent")
    return out if isinstance(out, str) else ("" if out is None else str(out))


async def _shell_sync(command: str) -> str:
    item = _first(await call("syncCmd", {"padCode": VMOS_PAD_CODE, "scriptContent": command}))
    status = item.get("taskStatus")
    if status is not None and int(status) in _FAILED:
        raise VmosError(item.get("errorMsg") or f"command failed (status {status})")
    return _output_of(item)


async def _shell_async(command: str) -> str:
    item = _first(await call("asyncCmd", {"padCodes": [VMOS_PAD_CODE], "scriptContent": command}))
    if item.get("vmStatus") == 0:
        raise VmosError("the cloud phone is offline")
    task_id = item.get("taskId")
    if task_id is None:
        raise VmosError("no taskId returned for the command")
    deadline = time.monotonic() + _ASYNC_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(_ASYNC_POLL_SECONDS)
        detail = _first(await call("padTaskDetail", {"taskIds": [task_id]}))
        status = detail.get("taskStatus")
        status = int(status) if status is not None else 1
        if status in _DONE:
            return _output_of(detail)
        if status in _FAILED:
            raise VmosError(detail.get("errorMsg") or f"command failed (status {status})")
    raise VmosError("command timed out on the cloud phone")


async def shell(command: str) -> str:
    """Run a shell command on the configured pad and return its output."""
    global _mode
    if not is_configured():
        raise VmosError("VMOS Cloud is not configured")
    if _mode == "async":
        return await _shell_async(command)
    try:
        return await _shell_sync(command)
    except VmosError as e:
        if _mode == "sync":
            raise
        logger.warning("VMOS syncCmd unavailable (%s); switching to asyncCmd", e)
        _mode = "async"
        return await _shell_async(command)


async def pad_status() -> dict:
    """The pad's padDetail row (padStatus 10 = running), for the admin check."""
    data = await call("padDetail", {"padCodes": [VMOS_PAD_CODE], "rows": 10})
    rows = (data or {}).get("pageData") if isinstance(data, dict) else data
    return _first(rows or [])

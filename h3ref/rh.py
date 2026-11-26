"""RunningHub: MiniMax H3 through the accelerated reference-to-video workflow (workflows/h3-ref2va-turbo.json).

The whole graph is sent with each task (asking for this workflow by id alone returns 810), with the prompt, reference
images, length, size, steps and seed filled in. H3 is billed by machine time, not by output seconds: a member key pays
RH coins (0.2 a second on the standard machine), an enterprise key pays its wallet. Measured: a 10–15 s clip runs
3½–5½ minutes, about 4 coins (≈ ¥0.009) per output second. Callers record the task id before polling, so a restart
resumes the task instead of paying twice.
"""
import json
import time
from pathlib import Path

from . import config, http
from .i18n import message

# nodes of the workflow we fill in, and their types (checked when the graph is loaded)
PROMPT, LENGTH, SIZE, STEPS, SEED = "250", "205", "230", "124", "129"
IMAGES = ["231", "232", "233", "234", "235", "236", "237", "238", "239"]   # ref_image_0..8, in prompt order (<Picture N>)
AUDIO = ["247", "248", "249"]
NODE_TYPES = {PROMPT: "CR Prompt Text", LENGTH: "ImpactSwitch", SIZE: "MiniMaxH3ReferenceToVideo", STEPS: "BasicScheduler",
              SEED: "RandomNoise", **{n: "LoadImage" for n in IMAGES}, **{n: "LoadAudio" for n in AUDIO}}
RUN_SECONDS_PER_OUTPUT_SECOND = 22     # measured 198–330 s for 10–15 s clips; the estimate rounds up
COINS_PER_RUN_SECOND = 0.2             # member key, standard machine


class SubmissionRejected(RuntimeError):
    """The server explicitly confirmed it did not accept a task."""


class QueueFull(SubmissionRejected):
    """Submission was rejected because queue (421) or machine (415) capacity is full; safe to retry."""

    def __init__(self, reason: str, *, code: int = 421):
        super().__init__(reason)
        self.code = code


NON_RETRYABLE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}
TERMINAL_POLL_CODES = {401, 403, 404, 807}


def _post(path: str, body: dict, timeout: float = 60) -> dict:
    key = config.rh_key()
    out = http.request(config.RH_BASE + path, key, {"apiKey": key, **body}, timeout=timeout, tries=1 if path.endswith("/create") else 3)
    if not isinstance(out, dict):
        raise RuntimeError("RunningHub returned non-object response: " + json.dumps(out, ensure_ascii=False).replace(key, "[key]")[:200])
    if isinstance(out.get("data"), dict):        # connection urls carry temporary credentials
        out["data"].pop("netWssUrl", None)
        out["data"].pop("clientId", None)
    return json.loads(json.dumps(out, ensure_ascii=False).replace(key, "[key]"))


def account() -> dict:
    return _post("/uc/openapi/accountStatus", {"apikey": config.rh_key()}).get("data") or {}


def base_graph() -> dict:
    g = json.loads(config.RH_GRAPH.read_text(encoding="utf-8"))
    bad = [n for n, t in NODE_TYPES.items() if g.get(n, {}).get("class_type") != t]
    if bad:
        raise SystemExit(f"{config.RH_GRAPH.name} 的节点对不上（{', '.join(bad)}），换过工作流就要改 rh.py 里的节点号")
    return g


def upload(path: Path) -> str:
    """The file name RunningHub gives an uploaded input; it goes into a LoadImage node as it is."""
    key = config.rh_key()
    out = http.upload(config.RH_BASE + "/task/openapi/upload", key, path, {"apiKey": key, "fileType": "input"})
    if out.get("code") != 0 or not (out.get("data") or {}).get("fileName"):
        raise RuntimeError("upload to RunningHub failed: " + json.dumps(out, ensure_ascii=False).replace(key, "[key]")[:200])
    return out["data"]["fileName"]


def graph(prompt: str, image_names: list[str], seconds: float, aspect: str, seed: int, steps: int | None = None) -> dict:
    """The workflow with this task's inputs. Length is chosen by second (5–15); the workflow snaps frames to 17k+5."""
    if len(image_names) > len(IMAGES):
        raise ValueError(f"H3 最多 {len(IMAGES)} 张参考图，这一段有 {len(image_names)} 张")
    if aspect not in config.SIZES:
        raise ValueError(f"画幅只支持 {', '.join(config.SIZES)}")
    g = base_graph()
    g[PROMPT]["inputs"]["prompt"] = prompt
    g[LENGTH]["inputs"]["select"] = max(1, min(11, round(float(seconds)) - 4))   # input1..11 = 5..15 s
    g[SIZE]["inputs"]["width"], g[SIZE]["inputs"]["height"] = config.SIZES[aspect]
    g[STEPS]["inputs"]["steps"] = int(steps or config.H3_STEPS)
    g[SEED]["inputs"]["noise_seed"] = int(seed)
    for node, name in zip(IMAGES, image_names + ["None"] * len(IMAGES)):
        g[node]["inputs"]["image"] = name
    for node in AUDIO:
        g[node]["inputs"]["audio"] = "None"
    return g


def estimate_cny(seconds: float) -> float:
    return round(max(5.0, float(seconds)) * RUN_SECONDS_PER_OUTPUT_SECOND * COINS_PER_RUN_SECOND * config.RH_COIN_CNY, 3)


def submit(g: dict) -> str:
    try:
        out = _post("/task/openapi/create", {"workflowId": config.RH_WORKFLOW_ID, "workflow": json.dumps(g, ensure_ascii=False), "addMetadata": False})
    except http.HTTPResponseError as exc:
        if exc.status_code in NON_RETRYABLE_STATUSES:
            raise SubmissionRejected(message("submission_rejected", reason=str(exc))) from exc
        raise
    if out.get("code") in (421, 415) or "QUEUE_MAXED" in str(out.get("msg") or ""):
        code = 421 if out.get("code") == 421 or "QUEUE_MAXED" in str(out.get("msg") or "") else 415
        raise QueueFull(json.dumps(out, ensure_ascii=False)[:200], code=code)
    data = out.get("data")
    task = data.get("taskId") if isinstance(data, dict) else None
    if out.get("code") != 0 or not task:
        rejected = bool((isinstance(out.get("code"), int) and out["code"] != 0)
                        or out.get("_http_status") in NON_RETRYABLE_STATUSES or out.get("error"))
        error = SubmissionRejected if rejected else RuntimeError
        raise error(message("submission_rejected", reason=json.dumps(out, ensure_ascii=False)[:300]))
    return str(task)


def submit_with_retry(g: dict, log, *, record_submission=None) -> str:
    """Retry only explicit capacity rejections; never retry an ambiguous network failure."""
    rejections = {421: 0, 415: 0}
    while True:
        if record_submission:
            record_submission(True)
        try:
            return submit(g)
        except QueueFull as exc:
            if record_submission:
                record_submission(False)
            rejections[exc.code] += 1
            attempt = rejections[exc.code]
            limit = 31 if exc.code == 421 else 5
            if attempt == limit:
                detail = message("submission_exhausted", attempt=attempt, limit=limit, reason=exc)
                log(detail)
                raise SubmissionRejected(detail) from exc
            log(message("submission_retry", attempt=attempt, limit=limit, seconds=60, reason=exc))
            time.sleep(60)
        except RuntimeError as exc:
            if isinstance(exc, SubmissionRejected) and record_submission:
                record_submission(False)
            log(message("submission_failed", reason=exc))
            raise


def poll(task: str, *, report_billing: bool = False) -> dict:
    """{state: running|done|failed, url, cny, coins, run_seconds, error}"""
    out = _post("/task/openapi/outputs", {"taskId": task})
    http_status = out.get("_http_status")
    if isinstance(http_status, int) and http_status >= 400:
        raise RuntimeError(f"RunningHub poll HTTP {http_status}: {json.dumps(out, ensure_ascii=False)[:260]}")
    code = out.get("code")
    if code in (804, 813):          # running / queued
        return {"state": "running", "queued": code == 813}
    msg_lower = str(out.get("msg") or "").lower()
    if code in (401, 403) or any(tok in msg_lower for tok in ("apikey", "api_key", "unauthorized", "forbidden")):
        raise RuntimeError(f"RunningHub poll auth error ({code}): {out.get('msg', '')}"[:300])
    if code == 805 or code in TERMINAL_POLL_CODES:
        reason = (out.get("data") or {}).get("failedReason") if isinstance(out.get("data"), dict) else None
        reason = reason or out.get("msg") or f"code {code}"
        if isinstance(reason, dict):
            reason = f"{reason.get('node_name', '')} {reason.get('exception_type', '')}: {str(reason.get('exception_message', ''))[:200]}"
        return {"state": "failed", "error": str(reason)[:300]}
    if any(tok in msg_lower for tok in ("not exist", "cancelled", "canceled")):
        return {"state": "failed", "error": f"{code} {out.get('msg', '')}"[:300]}
    if code == 0 and not out.get("data"):
        return {"state": "failed", "error": "completed without output media"}
    if code != 0 or not out.get("data"):
        return {"state": "running", "note": f"{code} {out.get('msg', '')}"[:60]}
    items = out["data"]
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return {"state": "failed", "error": "completed with invalid output list"}
    item = next((d for d in items if isinstance(d, dict) and str(d.get("fileType", "")).lower() in ("mp4", "mov", "webm")), items[0])
    if not item.get("fileUrl"):
        return {"state": "failed", "error": "completed without output fileUrl"}
    coins = float(item.get("consumeCoins") or 0)
    money = float(item.get("consumeMoney") or 0)
    result = {"state": "done", "url": item["fileUrl"], "cny": round(money + coins * config.RH_COIN_CNY, 4), "coins": coins,
              "money": money, "run_seconds": float(item.get("taskCostTime") or 0)}
    if report_billing:
        reported = [type(item.get(field)) in (int, float) or
                    isinstance(item.get(field), str) and bool(item[field].strip())
                    for field in ("consumeCoins", "consumeMoney")]
        result["billing_known"] = any(reported)
        result["billing_complete"] = all(reported)
    return result

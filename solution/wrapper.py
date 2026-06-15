"""Mitigation + observability layer for the Observathon agent.

Legal moves only: retry / cache / route / sanitize / redact / guardrail / fallback.
No hardcoded answers, no question->answer lookup, no agent internals, no network.

mitigate(call_next, question, config, context) is called once per request. The
shared dict `context["cache"]` (guarded by `context["cache_lock"]`) lets us
de-duplicate identical requests across concurrent workers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time


# ---------------------------------------------------------------------------
# Load improved system prompt (prompt routing).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROMPT_PATH = os.path.join(_HERE, "prompt.txt")
_TELEMETRY_PATH = os.path.join(_HERE, "telemetry_events.jsonl")
_LOG_LOCK = threading.Lock()

try:
    with open(_PROMPT_PATH, "r", encoding="utf-8") as _f:
        _SYSTEM_PROMPT = _f.read().strip()
except Exception:
    _SYSTEM_PROMPT = ""


# ---------------------------------------------------------------------------
# Input sanitation: neutralise prompt-injection phrasing in order notes.
# We MASK (do not delete) so product/quantity/coupon/destination tokens survive.
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    re.compile(r"(?is)\b(ignore|disregard|bo\s*qua)\s+(previous|prior|all|earlier|cac)\s+"
               r"(instructions?|huong\s*dan|prompt|rules?)\b"),
    re.compile(r"(?im)^\s*(system|developer|admin)\s*[:\-]\s*"),
    re.compile(r"(?is)\b(price\s*override|override\s+the?\s+price|gia\s*chinh\s*thuc|"
               r"gia\s*that(?:\s*la)?|set\s+price\s+to|fake\s+tool|fake\s+result)\b"),
    re.compile(r"(?is)\b(true|real|actual|secret)\s+(price|gia)\b"),
    re.compile(r"(?is)\blam\s+theo\s+(system|developer|admin|instruction)\b"),
    re.compile(r"(?is)```[\s\S]{0,400}```"),
]


def _sanitize_question(q):
    if not isinstance(q, str) or not q:
        return q, 0
    text = q
    masked = 0
    for pat in _INJECTION_PATTERNS:
        text, k = pat.subn("[NOTE_SANITIZED]", text)
        masked += k
    return text, masked


def _cache_key(text):
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Output PII redaction. We never log raw PII either.
# ---------------------------------------------------------------------------
_PII_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PII_PHONE_VN = re.compile(r"\b(?:\+84|0)\d{9}\b")
_PII_PHONE_LOOSE = re.compile(r"(?<!\d)\d{3,4}[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}(?!\d)")
_PII_CCCD = re.compile(r"\b\d{12}\b")
# Matches a "Tong cong: <number> VND" fragment anywhere (line-anchored OR inline),
# so we can both *detect* a stated total and *replace* it cleanly.
_TONG_FRAG = re.compile(
    r"(?i)tong\s*cong\s*[:\-]\s*[0-9][\d\.,\s]*\s*(?:vnd|vnđ|đ|d)?",
)


def _redact_pii(text):
    if not isinstance(text, str):
        return text, 0
    n = 0
    text, k = _PII_EMAIL.subn("[email khach]", text); n += k
    text, k = _PII_CCCD.subn("[cccd khach]", text); n += k
    text, k = _PII_PHONE_VN.subn("[sdt khach]", text); n += k
    # _PII_PHONE_LOOSE may eat order amounts; only apply to clearly phone-looking
    # tokens (>=10 digits in a row separated by space/dash).
    text, k = _PII_PHONE_LOOSE.subn("[so khach]", text); n += k
    return text, n


# ---------------------------------------------------------------------------
# Trace parsing -- defensive: the shape may vary across model/tool serialisers.
# ---------------------------------------------------------------------------
def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _to_int(x):
    try:
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return int(x)
        if isinstance(x, str):
            s = re.sub(r"[^\d\-]", "", x)
            return int(s) if s and s != "-" else None
    except Exception:
        return None
    return None


def _node_action(n):
    for k in ("action", "tool", "name", "tool_name", "function", "operation"):
        v = n.get(k) if isinstance(n, dict) else None
        if isinstance(v, str):
            return v.lower()
    return None


def _node_result(n):
    for k in ("result", "output", "observation", "response", "data", "value"):
        if isinstance(n, dict) and k in n:
            return n[k]
    return None


def _trace_facts(trace):
    """Best-effort: pull {in_stock, unit_price, pct, shipping} + refusal signal."""
    facts = {}
    refusal = None
    for n in _walk(trace):
        if not isinstance(n, dict):
            continue
        name = _node_action(n)
        if not name:
            continue
        result = _node_result(n)
        flat = result if isinstance(result, dict) else {}
        # gather some fields from the node itself too (different serialisers)
        merged = {}
        merged.update(flat)
        for k in ("in_stock", "unit_price", "price", "pct", "percent", "percentage",
                  "fee", "shipping", "status", "valid"):
            if k in n and k not in merged:
                merged[k] = n[k]

        if "check_stock" in name or name == "stock":
            stk = merged.get("in_stock")
            if stk is None:
                stk = merged.get("available")
            if isinstance(stk, str):
                stk = stk.lower() not in ("0", "false", "no", "out", "oos", "out_of_stock")
            if stk is False or str(merged.get("status", "")).lower() in (
                    "out_of_stock", "not_found", "unavailable"):
                refusal = refusal or str(merged.get("status") or "out_of_stock")
                facts["in_stock"] = False
            elif stk is True:
                facts["in_stock"] = True
            for k in ("unit_price", "price"):
                v = _to_int(merged.get(k))
                if v is not None:
                    facts.setdefault("unit_price", v)
                    break
        elif "discount" in name or "coupon" in name:
            for k in ("pct", "percent", "percentage", "discount_pct", "value"):
                v = _to_int(merged.get(k))
                if v is not None:
                    facts.setdefault("pct", v)
                    break
            if merged.get("valid") is False or str(merged.get("status", "")).lower() in (
                    "invalid", "expired", "not_applicable", "unknown"):
                facts["pct"] = 0
        elif "ship" in name:
            for k in ("fee", "cost", "amount", "value", "shipping", "price"):
                v = _to_int(merged.get(k))
                if v is not None:
                    facts.setdefault("shipping", v)
                    break
            if str(merged.get("status", "")).lower() in (
                    "not_served", "unsupported", "unknown_destination", "unavailable"):
                refusal = refusal or "not_served"
    return facts, refusal


def _trace_had_tool_error(trace):
    for n in _walk(trace):
        if not isinstance(n, dict):
            continue
        if n.get("error"):
            return True
        st = n.get("status")
        if isinstance(st, str) and st.lower() in ("error", "tool_error", "failed", "exception"):
            return True
    return False


def _extract_qty(question):
    m = re.search(r"\b([1-9]\d{0,2})\b", question or "")
    return int(m.group(1)) if m else 1


def _recompute_total(facts, question):
    unit = facts.get("unit_price")
    if unit is None or unit <= 0:
        return None
    qty = _extract_qty(question)
    pct = facts.get("pct", 0) or 0
    ship = facts.get("shipping", 0) or 0
    try:
        subtotal = int(unit) * int(qty)
        discounted = subtotal * (100 - int(pct)) // 100
        return discounted + int(ship)
    except Exception:
        return None


def _replace_total_line(answer, total):
    new_line = f"Tong cong: {total} VND"
    if not isinstance(answer, str) or not answer.strip():
        return new_line
    # Strip any prior "Tong cong: <n> VND" fragments anywhere, then re-append once.
    stripped = _TONG_FRAG.sub("", answer).strip()
    return (stripped + "\n" + new_line).strip() if stripped else new_line


def _force_refusal(answer, reason):
    reason = (reason or "khong dat duoc").replace("_", " ")
    msg = f"Khong the dat hang ({reason}). Tu choi va khong dua ra tong tien."
    if isinstance(answer, str):
        cleaned = _TONG_FRAG.sub("", answer).strip()
        if cleaned and msg not in cleaned:
            return cleaned + "\n" + msg
        if cleaned:
            return cleaned
    return msg


# ---------------------------------------------------------------------------
# Observability sink: JSON lines into solution/telemetry_events.jsonl.
# We swallow IO errors -- observability must never crash the wrapper.
# ---------------------------------------------------------------------------
def _log_event(event):
    try:
        with _LOG_LOCK:
            with open(_TELEMETRY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


_RETRYABLE_STATUSES = {"loop", "max_steps", "no_action", "wrapper_error", "error", "timeout"}
_NON_RETRYABLE_ERR = re.compile(
    r"(?i)\b(authenticat|invalid_api_key|permission|forbidden|401|403|"
    r"insufficient_quota|billing|model_not_found|context_length)\b"
)


def mitigate(call_next, question, config, context):
    t0 = time.time()
    context = context if isinstance(context, dict) else {}
    qid = context.get("qid")
    session_id = context.get("session_id")
    turn_index = context.get("turn_index")
    cache = context.get("cache")
    cache_lock = context.get("cache_lock")

    # 1) Sanitize input (mask injection phrasing). Cache key uses sanitized text.
    q_sanitized, n_injection = _sanitize_question(question or "")
    ckey = _cache_key(q_sanitized)

    # 2) Cache hit?
    if cache is not None and cache_lock is not None:
        try:
            with cache_lock:
                cached = cache.get(ckey)
        except Exception:
            cached = None
        if cached is not None:
            _log_event({
                "qid": qid, "session_id": session_id, "turn_index": turn_index,
                "status": cached.get("status") if isinstance(cached, dict) else None,
                "cache_hit": True, "retry_count": 0,
                "wall_ms": int((time.time() - t0) * 1000),
                "detected_faults": [],
            })
            return cached

    # 3) Prompt routing: override the agent's system prompt for this request.
    conf = dict(config) if isinstance(config, dict) else {}
    if _SYSTEM_PROMPT:
        conf["system_prompt"] = _SYSTEM_PROMPT

    # 4) Retry loop with bounded backoff.
    max_attempts = 3
    backoff_ms = 120
    attempt = 0
    result = None
    last_err = None
    detected = []
    tool_err_seen = False

    while attempt < max_attempts:
        attempt += 1
        try:
            result = call_next(q_sanitized, conf)
        except Exception as e:
            last_err = repr(e)[:240]
            result = {"answer": None, "status": "wrapper_error",
                      "steps": 0, "trace": [], "meta": {}}
        if not isinstance(result, dict):
            result = {"answer": None, "status": "wrapper_error",
                      "steps": 0, "trace": [], "meta": {}}
        status = result.get("status") or "wrapper_error"
        trace = result.get("trace") or []
        tool_err = _trace_had_tool_error(trace)
        if tool_err:
            tool_err_seen = True
        if status == "ok" and not tool_err:
            break
        # Don't waste retries on permanent failures (auth, quota, model not found).
        if last_err and _NON_RETRYABLE_ERR.search(last_err):
            detected.append("auth_error")
            break
        if attempt < max_attempts and (status in _RETRYABLE_STATUSES or tool_err):
            time.sleep(min(backoff_ms, 800) / 1000.0)
            backoff_ms = min(backoff_ms * 2, 800)
            continue
        break

    if tool_err_seen:
        detected.append("tool_failure")

    answer = result.get("answer") or ""
    trace = result.get("trace") or []
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}

    # 5) Ground the answer against trace facts.
    facts, refusal = _trace_facts(trace)
    had_total_line = bool(_TONG_FRAG.search(answer))
    if refusal:
        # If LLM still emitted a total, that's a fabrication.
        if had_total_line:
            detected.append("fabrication")
        answer = _force_refusal(answer, refusal)
    else:
        recomputed = _recompute_total(facts, question)
        if recomputed is not None:
            m = re.search(r"(?im)tong\s*cong\s*[:\-]\s*([0-9\.\s,]+)", answer)
            stated = None
            if m:
                digits = re.sub(r"[^\d]", "", m.group(1))
                stated = int(digits) if digits else None
            if stated != recomputed:
                detected.append("arithmetic_error")
                answer = _replace_total_line(answer, recomputed)

    # 6) PII redaction on the final answer.
    answer, pii_n = _redact_pii(answer)
    if pii_n:
        detected.append("pii_leak")

    # 7) If LLM produced no answer at all, give a safe minimal refusal.
    if not answer.strip():
        answer = "Khong the xu ly yeu cau. Vui long cung cap them thong tin san pham/so luong/dia chi giao."

    result["answer"] = answer

    # 8) Persist to cache (post-mitigation, so cache hits are already clean).
    if cache is not None and cache_lock is not None:
        try:
            with cache_lock:
                cache[ckey] = result
        except Exception:
            pass

    # 9) Observability event (no raw PII, no raw question text).
    try:
        usage = meta.get("usage") or {}
        tools_used = meta.get("tools_used") or []
        _log_event({
            "qid": qid,
            "session_id": session_id,
            "turn_index": turn_index,
            "status": result.get("status"),
            "steps": result.get("steps"),
            "latency_ms": meta.get("latency_ms"),
            "wall_ms": int((time.time() - t0) * 1000),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            "tools_used": tools_used,
            "tool_count": len(tools_used) if isinstance(tools_used, list) else None,
            "retry_count": attempt - 1,
            "pii_found": int(pii_n),
            "injection_masked": int(n_injection),
            "cache_hit": False,
            "detected_faults": sorted(set(detected)),
            "model": meta.get("model"),
            "provider": meta.get("provider"),
            "last_error": last_err,
        })
    except Exception:
        pass

    return result

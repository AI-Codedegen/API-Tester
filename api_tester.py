#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_tester.py — Tool tạo test case tự động từ 1 lệnh cURL và chạy kiểm thử API (CLI + mini UI)

BẢN SỬA LỖI (SystemExit: 1 khi chạy UI + sandbox không có socket.SO_REUSEADDR):
- Tiếp tục tránh "SystemExit: 1" khi Flask không bind được cổng: thử nhiều host (0.0.0.0 → 127.0.0.1 → localhost), tăng port +1..+20, cuối cùng dùng port=0 (OS random), bắt `SystemExit`/`OSError` và không làm sập tiến trình.
- **Sửa crash `AttributeError: socket.SO_REUSEADDR`**: BỎ HẲN `setsockopt(...)` trong `_port_free` (một số sandbox không có hằng số này). Chỉ cần `bind()` thử là đủ kiểm tra khả dụng.
- Đã thay toàn bộ in lỗi sang `sys.stderr.write(...)` khi cần, giữ UI `use_reloader=False`.
- KHÔNG thay đổi các test case cũ; vẫn giữ các case đã bổ sung: `empty_body`, `malformed_json`, `wrong_accept_header`.

Cài đặt:
  pip install requests flask

Cách chạy nhanh:
  # 1) Không tham số → tự mở UI (tự chọn host/port khả dụng)
  python3 api_tester.py

  # 2) Chạy UI tường minh
  PORT=8787 python3 api_tester.py --ui --port 8787

  # 3) Chạy CLI
  python3 api_tester.py --curl "<dán lệnh curl ở đây>" --out report --timeout 30

Lưu ý:
- Công cụ dùng heuristic; hãy nêu rõ **kỳ vọng chuẩn** của API để tinh chỉnh rules chấm PASS/FAIL.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import errno
import html
import json
import os
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("Thiếu thư viện 'requests'. Vui lòng chạy: pip install requests\n")
    raise


# --------------------------
# Mô hình dữ liệu
# --------------------------
@dataclass
class ParsedCurl:
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    data: Optional[str] = None
    verify_ssl: bool = True


@dataclass
class TestCase:
    name: str
    description: str
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[str]
    expect: Dict[str, Any]


@dataclass
class TestResult:
    name: str
    description: str
    method: str
    url: str
    request_headers: Dict[str, str]
    request_body: Optional[str]
    status_code: Optional[int]
    elapsed_ms: Optional[int]
    ok: bool
    reason: str
    response_preview: str


DEFAULT_TIMEOUT = int(os.getenv("API_TESTER_TIMEOUT", "30"))
DEFAULT_PORT = int(os.getenv("PORT", "8787"))
DEFAULT_HOST = os.getenv("BIND_HOST", "0.0.0.0")

# --------------------------
# cURL parser đơn giản (dùng shlex)
# --------------------------
_CTYPE_JSON = "application/json"


def parse_curl(curl_cmd: str) -> ParsedCurl:
    """Parse nhanh một lệnh curl.
    Hỗ trợ các option phổ biến: -X/--request, -H/--header, --data/--data-raw/--data-binary/-d,
    --insecure/-k. URL lấy là token cuối cùng không phải option.
    """
    if not curl_cmd or not curl_cmd.strip():
        raise ValueError("Thiếu cURL: vui lòng cung cấp lệnh curl đầy đủ.")

    if not curl_cmd.strip().lower().startswith("curl "):
        # cho phép dán không có từ 'curl' đầu
        curl_cmd = "curl " + curl_cmd.strip()

    tokens = shlex.split(curl_cmd)
    method = None
    headers: Dict[str, str] = {}
    data = None
    verify_ssl = True
    url = None

    i = 1  # skip 'curl'
    while i < len(tokens):
        t = tokens[i]
        if t in ("-X", "--request") and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            i += 2
        elif t in ("-H", "--header") and i + 1 < len(tokens):
            hv = tokens[i + 1]
            # chấp nhận 'Key: value'
            if ":" in hv:
                k, v = hv.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2
        elif t in ("--data", "--data-raw", "--data-binary", "--data-ascii", "-d") and i + 1 < len(tokens):
            data = tokens[i + 1]
            i += 2
        elif t in ("--insecure", "-k"):
            verify_ssl = False
            i += 1
        elif t.startswith("http://") or t.startswith("https://"):
            url = t
            i += 1
        elif t == "--url" and i + 1 < len(tokens):
            url = tokens[i + 1]
            i += 2
        else:
            # có thể là URL ở cuối mà không có tiền tố option
            if i == len(tokens) - 1 and (t.startswith("http://") or t.startswith("https://")):
                url = t
            i += 1

    # Suy đoán method
    if not method:
        method = "POST" if data is not None else "GET"

    if not url:
        raise ValueError("Không tìm thấy URL trong cURL.")

    return ParsedCurl(method=method, url=url, headers=headers, data=data, verify_ssl=verify_ssl)


# --------------------------
# Hỗ trợ phân tích body & sinh biến thể
# --------------------------

def is_json_content(headers: Dict[str, str], body: Optional[str]) -> bool:
    ct = headers.get("Content-Type") or headers.get("content-type")
    if ct and _CTYPE_JSON in ct.lower():
        return True
    if body:
        b = body.strip()
        if (b.startswith("{") and b.endswith("}")) or (b.startswith("[") and b.endswith("]")):
            return True
    return False


def try_parse_json(text: Optional[str]) -> Tuple[bool, Any]:
    if text is None:
        return False, None
    try:
        return True, json.loads(text)
    except Exception:
        return False, None


def detect_path_id(url: str) -> Optional[Tuple[str, str]]:
    """Tìm số trong path để thử thay đổi (id). Trả về (pattern, replacement)."""
    m = re.search(r"/(\d+)(?=/|$)", url)
    if m:
        old = m.group(1)
        new = "9999999" if old in ("0", "1") else "-1"
        return old, new
    return None


# --------------------------
# Sinh test cases từ ParsedCurl (GIỮ NGUYÊN + bổ sung hợp lệ)
# --------------------------

def generate_testcases(pc: ParsedCurl) -> List[TestCase]:
    cases: List[TestCase] = []

    headers = dict(pc.headers)

    # 1) Baseline (giữ nguyên)
    cases.append(TestCase(
        name="baseline",
        description="Gửi đúng theo cURL gốc.",
        method=pc.method,
        url=pc.url,
        headers=headers,
        body=pc.data,
        expect={"accept_any": True},
    ))

    # 2) Thiếu Authorization (giữ nguyên)
    if any(k.lower() == "authorization" for k in headers):
        no_auth_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        cases.append(TestCase(
            name="missing_auth",
            description="Bỏ header Authorization => kỳ vọng 401/403.",
            method=pc.method,
            url=pc.url,
            headers=no_auth_headers,
            body=pc.data,
            expect={"status_in": [401, 403]},
        ))

    # 3) Sai method (giữ nguyên)
    alt_method = "GET" if pc.method != "GET" else "POST"
    cases.append(TestCase(
        name="wrong_method",
        description=f"Dùng method {alt_method} thay cho {pc.method} => kỳ vọng không 2xx.",
        method=alt_method,
        url=pc.url,
        headers=headers,
        body=pc.data if alt_method in ("POST", "PUT", "PATCH") else None,
        expect={"not_2xx": True},
    ))

    # Phát hiện JSON
    json_like = is_json_content(headers, pc.data)
    ok_json, body_json = try_parse_json(pc.data) if json_like else (False, None)

    # 4) Thiếu 1 field JSON (giữ nguyên)
    if ok_json and isinstance(body_json, dict) and body_json:
        key_to_remove = next(iter(body_json.keys()))
        missing_json = copy.deepcopy(body_json)
        missing_json.pop(key_to_remove, None)
        cases.append(TestCase(
            name="missing_field",
            description=f"Bỏ field bất kỳ '{key_to_remove}' trong JSON => kỳ vọng 4xx.",
            method=pc.method,
            url=pc.url,
            headers=headers,
            body=json.dumps(missing_json, ensure_ascii=False),
            expect={"is_4xx": True},
        ))

    # 5) Sai kiểu dữ liệu trường đầu tiên (giữ nguyên)
    if ok_json and isinstance(body_json, dict) and body_json:
        k0 = next(iter(body_json.keys()))
        bad_json = copy.deepcopy(body_json)
        v0 = bad_json[k0]
        bad_json[k0] = ("not-a-number" if isinstance(v0, (int, float)) else 12345)
        cases.append(TestCase(
            name="invalid_type",
            description=f"Đổi kiểu dữ liệu field '{k0}' => kỳ vọng 4xx.",
            method=pc.method,
            url=pc.url,
            headers=headers,
            body=json.dumps(bad_json, ensure_ascii=False),
            expect={"is_4xx": True},
        ))

    # 6) Chuỗi quá dài (giữ nguyên)
    if ok_json and isinstance(body_json, dict):
        str_key = None
        for k, v in body_json.items():
            if isinstance(v, str):
                str_key = k
                break
        if str_key is not None:
            long_json = copy.deepcopy(body_json)
            long_json[str_key] = "A" * 2000
            cases.append(TestCase(
                name="too_long_string",
                description=f"Tăng chiều dài chuỗi field '{str_key}' lên 2000 ký tự => kỳ vọng 4xx.",
                method=pc.method,
                url=pc.url,
                headers=headers,
                body=json.dumps(long_json, ensure_ascii=False),
                expect={"is_4xx": True},
            ))

    # 7) SQLi thử nghiệm (giữ nguyên)
    if ok_json and isinstance(body_json, dict):
        sqli_json = copy.deepcopy(body_json)
        injected = False
        for k, v in sqli_json.items():
            if isinstance(v, str):
                sqli_json[k] = "' OR '1'='1"
                injected = True
                break
        if injected:
            cases.append(TestCase(
                name="sqli_probe",
                description="Thử payload SQLi đơn giản => kỳ vọng KHÔNG 5xx.",
                method=pc.method,
                url=pc.url,
                headers=headers,
                body=json.dumps(sqli_json, ensure_ascii=False),
                expect={"not_5xx": True},
            ))

    # 8) Đổi path id nếu có (giữ nguyên)
    id_pat = detect_path_id(pc.url)
    if id_pat:
        old, new = id_pat
        new_url = pc.url.replace(f"/{old}", f"/{new}")
        cases.append(TestCase(
            name="path_id_variant",
            description=f"Thay id trong path {old} -> {new} => kỳ vọng 4xx hoặc 404.",
            method=pc.method,
            url=new_url,
            headers=headers,
            body=pc.data,
            expect={"status_in": [400, 401, 403, 404]},
        ))

    # 9) Sai Content-Type (giữ nguyên)
    if json_like:
        ct_headers = dict(headers)
        ct_headers["Content-Type"] = "text/plain"
        cases.append(TestCase(
            name="wrong_content_type",
            description="Đổi Content-Type thành text/plain với body JSON => kỳ vọng 4xx/415.",
            method=pc.method,
            url=pc.url,
            headers=ct_headers,
            body=pc.data,
            expect={"is_4xx_or_415": True},
        ))

    # 10) Replay/Idempotency (giữ nguyên)
    cases.append(TestCase(
        name="replay_same_request",
        description="Gửi lại cùng request 2 lần => kỳ vọng status giống nhau (ổn định).",
        method=pc.method,
        url=pc.url,
        headers=headers,
        body=pc.data,
        expect={"stable_status": True},
    ))

    # ----------------------
    # BỔ SUNG TEST CASE (đã thêm trước)
    # ----------------------
    if pc.method in ("POST", "PUT", "PATCH") and pc.data is not None:
        cases.append(TestCase(
            name="empty_body",
            description="Gửi body rỗng cho API vốn có body => kỳ vọng 4xx.",
            method=pc.method,
            url=pc.url,
            headers=headers,
            body="",
            expect={"is_4xx": True},
        ))

    if is_json_content(headers, pc.data) and pc.data:
        cases.append(TestCase(
            name="malformed_json",
            description="Body JSON sai cú pháp (thiếu ngoặc/ngoặc thừa) => kỳ vọng 4xx.",
            method=pc.method,
            url=pc.url,
            headers={**headers, "Content-Type": "application/json"},
            body=(pc.data.rstrip() + "]"),
            expect={"is_4xx": True},
        ))

    wrong_accept_headers = dict(headers)
    wrong_accept_headers["Accept"] = "application/xml"
    cases.append(TestCase(
        name="wrong_accept_header",
        description="Đặt Accept=application/xml cho API thường trả JSON => kỳ vọng KHÔNG 5xx.",
        method=pc.method,
        url=pc.url,
        headers=wrong_accept_headers,
        body=pc.data,
        expect={"not_5xx": True},
    ))

    return cases


# --------------------------
# Thực thi test cases
# --------------------------

def mask_sensitive(h: Dict[str, str]) -> Dict[str, str]:
    masked = {}
    for k, v in h.items():
        if k.lower() in ("authorization", "api-key", "x-api-key", "token", "x-token", "cookie"):
            masked[k] = ("***" if len(v) <= 8 else v[:4] + "***" + v[-4:])
        else:
            masked[k] = v
    return masked


def send_request(method: str, url: str, headers: Dict[str, str], body: Optional[str], verify_ssl: bool, timeout: int) -> Tuple[int, int, str]:
    start = time.time()
    try:
        resp = requests.request(method=method, url=url, headers=headers or None,
                                data=body if body is not None else None,
                                timeout=timeout, verify=verify_ssl)
        elapsed_ms = int((time.time() - start) * 1000)
        preview = resp.text
        if len(preview) > 800:
            preview = preview[:800] + "..."
        return resp.status_code, elapsed_ms, preview
    except requests.RequestException as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return -1, elapsed_ms, f"ERROR: {e}"


def evaluate(expect: Dict[str, Any], status: int) -> Tuple[bool, str]:
    if expect.get("accept_any"):
        return True, "Baseline chấp nhận mọi kết quả"
    if "status_in" in expect:
        allowed = expect["status_in"]
        return (status in allowed), f"status={status}, kỳ vọng trong {allowed}"
    if expect.get("not_2xx"):
        return (not (200 <= status < 300)), f"status={status}, kỳ vọng không phải 2xx"
    if expect.get("is_4xx"):
        return (400 <= status < 500), f"status={status}, kỳ vọng 4xx"
    if expect.get("is_4xx_or_415"):
        return (400 <= status < 500), f"status={status}, kỳ vọng 4xx/415"
    if expect.get("not_5xx"):
        return (not (500 <= status < 600)), f"status={status}, kỳ vọng không 5xx"
    if expect.get("stable_status"):
        return True, "Sẽ so sánh ở bước thực thi"
    return True, "Không có kỳ vọng cụ thể"


def run_cases(curl_cmd: str, out_prefix: Optional[str] = None, serve_mode: bool = False, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    pc = parse_curl(curl_cmd)
    cases = generate_testcases(pc)

    results: List[TestResult] = []

    for tc in cases:
        if tc.expect.get("stable_status"):
            s1, t1, p1 = send_request(tc.method, tc.url, tc.headers, tc.body, pc.verify_ssl, timeout)
            s2, t2, p2 = send_request(tc.method, tc.url, tc.headers, tc.body, pc.verify_ssl, timeout)
            ok = (s1 == s2) and (s1 != -1)
            reason = f"status1={s1}, status2={s2}, kỳ vọng giống nhau"
            results.append(TestResult(
                name=tc.name,
                description=tc.description,
                method=tc.method,
                url=tc.url,
                request_headers=mask_sensitive(tc.headers),
                request_body=tc.body,
                status_code=s2,
                elapsed_ms=t1 + t2,
                ok=ok,
                reason=reason,
                response_preview=(p2 if p2 else p1) or "",
            ))
            continue

        status, elapsed_ms, preview = send_request(tc.method, tc.url, tc.headers, tc.body, pc.verify_ssl, timeout)
        ok, reason = evaluate(tc.expect, status)
        results.append(TestResult(
            name=tc.name,
            description=tc.description,
            method=tc.method,
            url=tc.url,
            request_headers=mask_sensitive(tc.headers),
            request_body=tc.body,
            status_code=status,
            elapsed_ms=elapsed_ms,
            ok=ok,
            reason=reason,
            response_preview=preview,
        ))

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "duration_ms": sum(r.elapsed_ms or 0 for r in results),
    }

    output = {
        "summary": summary,
        "parsed": dataclasses.asdict(pc),
        "results": [dataclasses.asdict(r) for r in results],
    }

    if out_prefix and not serve_mode:
        with open(out_prefix + ".json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        with open(out_prefix + ".md", "w", encoding="utf-8") as f:
            f.write(render_markdown_report(output))
        with open(out_prefix + ".html", "w", encoding="utf-8") as f:
            f.write(render_html_report(output))

    return output


# --------------------------
# Render báo cáo
# --------------------------

def render_markdown_report(data: Dict[str, Any]) -> str:
    s = data["summary"]
    lines = []
    lines.append("# API Test Report")
    lines.append("")
    lines.append(f"Tổng: **{s['total']}** · PASS: **{s['passed']}** · FAIL: **{s['failed']}** · Thời gian: **{s['duration_ms']} ms**")
    lines.append("")
    lines.append("| Case | Trạng thái | Status | Thời gian (ms) | Ghi chú |")
    lines.append("|---|---|---:|---:|---|")
    for r in data["results"]:
        badge = "✅ PASS" if r["ok"] else "❌ FAIL"
        lines.append(f"| `{r['name']}` | {badge} | {r['status_code']} | {r['elapsed_ms']} | {r['reason']} |")
    lines.append("")
    lines.append("## Chi tiết")
    for r in data["results"]:
        lines.append(f"### {r['name']}")
        lines.append(r.get("description", ""))
        lines.append("")
        lines.append("**Request**:")
        lines.append("")
        lines.append(f"- Method: `{r['method']}`")
        lines.append(f"- URL: `{r['url']}`")
        lines.append(f"- Headers: `{json.dumps(r['request_headers'], ensure_ascii=False)}`")
        if r.get("request_body") is not None:
            body_pre = r['request_body']
            if isinstance(body_pre, str) and len(body_pre) > 800:
                body_pre = body_pre[:800] + "..."
            lines.append(f"- Body: `{body_pre}`")
        lines.append("")
        lines.append("**Response**:")
        lines.append("")
        lines.append(f"- Status: `{r['status_code']}`")
        lines.append(f"- Time: `{r['elapsed_ms']} ms`")
        resp_pre = r.get("response_preview") or ""
        resp_pre = resp_pre.replace("`", "\`")
        lines.append("")
        lines.append("```\n" + resp_pre + "\n```")
        lines.append("")
    return "\n".join(lines)


def render_html_report(data: Dict[str, Any]) -> str:
    s = data["summary"]
    rows = []
    for r in data["results"]:
        badge = ("<span style='color:#0a0'>PASS</span>" if r["ok"] else "<span style='color:#c00'>FAIL</span>")
        rows.append(
            f"<tr><td><code>{html.escape(r['name'])}</code></td>"
            f"<td>{badge}</td>"
            f"<td style='text-align:right'>{r['status_code']}</td>"
            f"<td style='text-align:right'>{r['elapsed_ms']}</td>"
            f"<td>{html.escape(r['reason'])}</td></tr>"
        )

    detail_blocks = []
    for r in data["results"]:
        resp_pre = html.escape(r.get("response_preview") or "")
        req_body = r.get("request_body")
        if isinstance(req_body, str) and len(req_body) > 800:
            req_body = req_body[:800] + "..."
        req_body = req_body or ""
        detail_blocks.append(f"""
        <section style='margin:16px 0;padding:12px;border:1px solid #eee;border-radius:10px'>
          <h3 style='margin:0 0 8px 0'>{html.escape(r['name'])}</h3>
          <p style='margin:0 0 6px 0;color:#555'>{html.escape(r.get('description',''))}</p>
          <div><b>Request</b></div>
          <div>Method: <code>{html.escape(r['method'])}</code></div>
          <div>URL: <code>{html.escape(r['url'])}</code></div>
          <div>Headers: <code>{html.escape(json.dumps(r['request_headers'], ensure_ascii=False))}</code></div>
          {f"<div>Body: <code>{html.escape(req_body)}</code></div>" if req_body is not None else ""}
          <div style='height:8px'></div>
          <div><b>Response</b></div>
          <div>Status: <code>{r['status_code']}</code></div>
          <div>Time: <code>{r['elapsed_ms']} ms</code></div>
          <pre style='white-space:pre-wrap;background:#fafafa;border:1px solid #eee;padding:10px;border-radius:8px;max-height:400px;overflow:auto'>{resp_pre}</pre>
        </section>
        """)

    return f"""
<!doctype html>
<html lang=\"vi\"> 
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>API Test Report</title>
  <style>
    body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial,sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; text-align: left; }}
    th {{ background: #fafafa; }}
    code {{ background: #f5f5f7; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>API Test Report</h1>
  <p>Tổng: <b>{s['total']}</b> · PASS: <b style='color:#0a0'>{s['passed']}</b> · FAIL: <b style='color:#c00'>{s['failed']}</b> · Thời gian: <b>{s['duration_ms']} ms</b></p>
  <table>
    <thead>
      <tr><th>Case</th><th>Trạng thái</th><th style='text-align:right'>Status</th><th style='text-align:right'>Thời gian (ms)</th><th>Ghi chú</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>

  <h2>Chi tiết</h2>
  {''.join(detail_blocks)}
</body>
</html>
"""


# --------------------------
# Chọn host/port khả dụng & chạy UI (tránh SystemExit:1)
# --------------------------

def _port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Không dùng SO_REUSEADDR vì một số môi trường không có hằng số này
        s.bind((host, port))
        return True
    except OSError as e:
        # EADDRINUSE (98/48) hoặc EACCES (13)
        if isinstance(e, OSError) and e.errno in (getattr(errno, 'EADDRINUSE', 98), getattr(errno, 'EACCES', 13)):
            return False
        return False
    finally:
        try:
            s.close()
        except Exception:
                pass


def _pick_port(host: str, preferred: int, attempts: int = 20) -> int:
    if preferred and preferred > 0 and _port_free(host, preferred):
        return preferred
    base = preferred if preferred and preferred > 0 else DEFAULT_PORT
    for i in range(1, attempts + 1):
        cand = base + i
        if _port_free(host, cand):
            return cand
    return 0  # 0 = OS chọn ngẫu nhiên


def run_ui(host: str, port: int):
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    INDEX_HTML = """
<!doctype html>
<html lang=\"vi\"> 
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>API Tester</title>
  <style>
    :root {
      --bg: #ffffff;
      --fg: #111111;
      --muted: #6b7280;
      --card: #ffffff;
      --border: #e5e7eb;
      --accent: #2563eb;
      --ok: #16a34a;
      --ng: #dc2626;
      --codebg: #f3f4f6;
      --shadow: 0 2px 10px rgba(0,0,0,.06);
    }
    :root[data-theme="dark"] {
      --bg: #0b0f19;
      --fg: #e5e7eb;
      --muted: #9ca3af;
      --card: #111827;
      --border: #1f2937;
      --accent: #60a5fa;
      --ok: #22c55e;
      --ng: #f87171;
      --codebg: #0f172a;
      --shadow: 0 2px 10px rgba(0,0,0,.3);
    }

    * { box-sizing: border-box }
    body { font-family: system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Helvetica,Arial,sans-serif; margin: 24px; background: var(--bg); color: var(--fg); }
    textarea { width: 100%; min-height: 140px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace; background: var(--card); color: var(--fg); border: 1px solid var(--border); border-radius: 12px; padding: 12px; box-shadow: var(--shadow); }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    .btn { padding: 10px 16px; border-radius: 12px; border: 1px solid var(--border); background: var(--fg); color: var(--bg); cursor: pointer; font-weight:600; box-shadow: var(--shadow); }
    .btn[disabled] { opacity:.6; cursor: not-allowed; }
    .btn-secondary { background: transparent; color: var(--fg); }
    .panel { border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-top: 16px; background: var(--card); box-shadow: var(--shadow); }
    table { border-collapse: collapse; width: 100%; margin-top: 10px; }
    th, td { border-bottom: 1px solid var(--border); padding: 8px 10px; text-align: left; }
    th { background: color-mix(in oklab, var(--card), var(--fg) 5%); }
    code { background: var(--codebg); padding: 2px 6px; border-radius: 6px; }
    .ok { color: var(--ok) }
    .ng { color: var(--ng) }

    /* Loading bar */
    .loading { display:none; margin-top:12px; }
    .progress-wrap { height: 10px; width: 100%; background: var(--border); border-radius: 999px; overflow: hidden; }
    .progress-bar { height: 100%; width: 40%; background: var(--accent); border-radius: 999px; animation: indet 1.2s infinite; }
    @keyframes indet { 0% { transform: translateX(-100%); } 50% { transform: translateX(20%); } 100% { transform: translateX(100%); } }
    .loading-text { color: var(--muted); font-size: 14px; margin-top: 6px; display:flex; align-items:center; gap:8px; }
    .dot { width:6px; height:6px; border-radius:999px; background: var(--accent); animation: blink 1s infinite alternate; }
    @keyframes blink { from { opacity:.3 } to { opacity:1 } }

    .header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom: 8px; }
    .title { display:flex; align-items:center; gap:10px; }
    .title .badge { font-size:12px; padding:2px 8px; background: var(--codebg); border-radius:999px; color: var(--muted); border:1px solid var(--border); }
  </style>
</head>
<body>
  <div class=\"header\">
    <div class=\"title\">
      <h1 style=\"margin:0\">API Tester</h1>
      <span class=\"badge\">cURL → Test cases → Report</span>
    </div>
    <button id=\"themeBtn\" class=\"btn btn-secondary\" title=\"Chuyển giao diện sáng/tối\">🌙 Dark</button>
  </div>

  <p>Dán lệnh <code>curl</code> (đầy đủ method, headers, body, URL) rồi bấm <b>Chạy test</b>.</p>
  <textarea id=\"curl\" placeholder=\"curl -X POST https://api.example.com/v1/things -H 'Authorization: Bearer xxxxx' -H 'Content-Type: application/json' -d '{\\"name\\":\\"abc\\"}'\"></textarea>
  <div class=\"row\" style=\"margin-top:8px\">
    <button id=\"runBtn\" class=\"btn\" onclick=\"run()\">Chạy test</button>
    <small id=\"hint\" style=\"color:var(--muted)\">Mẹo: bạn có thể dán trực tiếp cả lệnh <code>curl</code>.</small>
  </div>

  <div id=\"loading\" class=\"loading\">
    <div class=\"progress-wrap\"><div class=\"progress-bar\"></div></div>
    <div class=\"loading-text\"><span class=\"dot\"></span> Đang chạy test… vui lòng đợi.</div>
  </div>

  <div id=\"result\" class=\"panel\" style=\"display:none\"></div>

<script>
(function initTheme(){
  const root = document.documentElement;
  const saved = localStorage.getItem('api_tester_theme');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = saved || (prefersDark ? 'dark' : 'light');
  root.setAttribute('data-theme', theme);
  document.getElementById('themeBtn').textContent = theme==='dark' ? '☀️ Light' : '🌙 Dark';
})();

document.getElementById('themeBtn').addEventListener('click', ()=>{
  const root = document.documentElement;
  const cur = root.getAttribute('data-theme') || 'light';
  const next = cur === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('api_tester_theme', next);
  document.getElementById('themeBtn').textContent = next==='dark' ? '☀️ Light' : '🌙 Dark';
});

async function run(){
  const runBtn = document.getElementById('runBtn');
  const loader = document.getElementById('loading');
  const el = document.getElementById('result');
  const curl = document.getElementById('curl').value;

  // UI state
  runBtn.disabled = true;
  loader.style.display = 'block';
  el.style.display = 'none';
  el.innerHTML = '';

  try{
    const res = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({curl, timeout: 30})});
    const data = await res.json();

    if (data.error){
      el.style.display = 'block';
      el.innerHTML = `<div style='color:var(--ng)'><b>Lỗi:</b> ${escapeHtml(data.error)}</div>`;
      return;
    }

    const s = data.summary;
    let html = `<h2 style='margin-top:0'>Kết quả</h2>`+
      `<p>Tổng: <b>${s.total}</b> · PASS: <b class='ok'>${s.passed}</b> · FAIL: <b class='ng'>${s.failed}</b> · Thời gian: <b>${s.duration_ms} ms</b></p>`;
    html += `<table><thead><tr><th>Case</th><th>Trạng thái</th><th style='text-align:right'>Status</th><th style='text-align:right'>Time (ms)</th><th>Ghi chú</th></tr></thead><tbody>`;
    for (const r of data.results){
      html += `<tr><td><code>${r.name}</code></td><td>${r.ok?'<span class=ok>PASS</span>':'<span class=ng>FAIL</span>'}</td><td style='text-align:right'>${r.status_code}</td><td style='text-align:right'>${r.elapsed_ms}</td><td>${r.reason}</td></tr>`;
    }
    html += `</tbody></table>`;
    html += `<details style='margin-top:12px'><summary>Chi tiết</summary>`;
    for (const r of data.results){
      html += `<section style='margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--card)'>`
              + `<h3 style='margin:0 0 8px 0'>${r.name}</h3>`
              + `<div><b>Request</b></div>`
              + `<div>Method: <code>${r.method}</code></div>`
              + `<div>URL: <code>${r.url}</code></div>`
              + `<div>Headers: <code>${escapeHtml(JSON.stringify(r.request_headers))}</code></div>`
              + (r.request_body!==null && r.request_body!==undefined ? `<div>Body: <code>${escapeHtml(r.request_body)}</code></div>`:'' )
              + `<div style='height:8px'></div>`
              + `<div><b>Response</b></div>`
              + `<div>Status: <code>${r.status_code}</code></div>`
              + `<div>Time: <code>${r.elapsed_ms} ms</code></div>`
              + `<pre style='white-space:pre-wrap;background:var(--codebg);border:1px solid var(--border);padding:10px;border-radius:8px;max-height:400px;overflow:auto'>${escapeHtml(r.response_preview||'')}</pre>`
              + `</section>`;
    }
    html += `</details>`;

    el.innerHTML = html;
    el.style.display = 'block';
  } catch(err){
    el.style.display = 'block';
    el.innerHTML = `<div style='color:var(--ng)'><b>Lỗi:</b> ${escapeHtml(String(err))}</div>`;
  } finally {
    // Restore UI state
    loader.style.display = 'none';
    runBtn.disabled = false;
  }
}

function escapeHtml(unsafe){
  return String(unsafe)
    .replaceAll('&','&amp;')
    .replaceAll('<','&lt;')
    .replaceAll('>','&gt;')
    .replaceAll('"','&quot;')
    .replaceAll("'",'&#039;');
}
</script>
</body>
</html>
"""

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/run")
    def run():
        from flask import request, jsonify
        payload = request.get_json(silent=True) or {}
        curl = payload.get("curl")
        timeout = int(payload.get("timeout") or DEFAULT_TIMEOUT)
        if not curl:
            return jsonify({"error": "Thiếu trường 'curl' trong payload."}), 400
        try:
            result = run_cases(curl, out_prefix=None, serve_mode=True, timeout=timeout)
            return jsonify(result)
        except Exception as e:  # pragma: no cover
            return jsonify({"error": str(e)}), 400

    # Thử nhiều host/port để tránh SystemExit:1 do bind thất bại
    host_candidates = [host or DEFAULT_HOST, DEFAULT_HOST, "127.0.0.1", "localhost"]
    tried_any = False
    for bind_host in host_candidates:
        chosen = _pick_port(bind_host, int(port) if port else DEFAULT_PORT)
        attempts = 0
        max_attempts = 20 if chosen != 0 else 1
        while True:
            tried_any = True
            try:
                print(f"[api_tester] UI chạy tại http://{bind_host}:{chosen if chosen!=0 else '<random>'}")
                # use_reloader=False để tránh reloader làm exit(1) ở vài môi trường
                app.run(host=bind_host, port=chosen, debug=False, use_reloader=False)
                return  # server blocking; khi dừng mới thoát
            except SystemExit as e:
                code = getattr(e, 'code', 0)
                if code == 0:
                    return
                attempts += 1
                if attempts > max_attempts:
                    sys.stderr.write(f"[api_tester] Không bind được {bind_host}:{chosen}.\n")
                    break  # thử host tiếp theo
                if chosen != 0:
                    chosen += 1
                    sys.stderr.write(f"[api_tester] Port bận, thử lại với {bind_host}:{chosen}...\n")
                else:
                    sys.stderr.write("[api_tester] Dùng port ngẫu nhiên vẫn lỗi — thử host khác.\n")
                    break
            except OSError as e:
                sys.stderr.write(f"[api_tester] OSError khi chạy UI: {e}\n")
                break

    if not tried_any:
        sys.stderr.write("[api_tester] Không thử được host/port nào.\n")
    sys.stderr.write("[api_tester] Không thể khởi động UI sau nhiều lần thử. Hãy dùng CLI: \n"
                     "  python3 api_tester.py --curl \"curl -X GET https://httpbin.org/get\"\n")
    return  # thoát hài hòa, không raise


# --------------------------
# CLI
# --------------------------

def main():
    p = argparse.ArgumentParser(description="Tạo test case từ cURL và chạy tự động (CLI + UI)")
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--curl", type=str, help="Lệnh cURL đầy đủ (có thể dùng @file để đọc từ file)")
    g.add_argument("--curl-file", type=str, help="Đường dẫn file chứa cURL")

    p.add_argument("--out", type=str, default="report", help="Prefix file báo cáo (mặc định: report)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout mỗi request (giây)")
    p.add_argument("--ui", action="store_true", help="Chạy UI (Flask)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port UI (mặc định 8787; 0 = random)")

    # Không tham số → mở UI (tự chọn host/port)
    if len(sys.argv) == 1:
        print("[api_tester] Không có tham số, mở UI (auto pick host/port) — dùng --help để xem trợ giúp")
        run_ui(host=DEFAULT_HOST, port=DEFAULT_PORT)
        return

    args = p.parse_args()

    if args.ui:
        run_ui(host=DEFAULT_HOST, port=args.port)
        return

    # Hỗ trợ --curl dạng @file
    curl_cmd = args.curl
    if curl_cmd and curl_cmd.startswith("@"):
        file_path = curl_cmd[1:]
        with open(file_path, "r", encoding="utf-8") as f:
            curl_cmd = f.read()

    if not curl_cmd and args.curl_file:
        with open(args.curl_file, "r", encoding="utf-8") as f:
            curl_cmd = f.read()

    if not curl_cmd:
        sys.stderr.write(
            "[api_tester] Cần --curl hoặc --curl-file, hoặc dùng --ui để chạy giao diện. Ví dụ:\n"
            "  python3 api_tester.py --ui\n"
            "  python3 api_tester.py --curl \"curl -X GET https://httpbin.org/get\"\n"
        )
        return

    out = run_cases(curl_cmd, out_prefix=args.out, timeout=args.timeout)
    s = out["summary"]
    print(f"Tổng: {s['total']} · PASS: {s['passed']} · FAIL: {s['failed']} · Thời gian: {s['duration_ms']} ms")
    print(f"Đã ghi báo cáo: {args.out}.json, {args.out}.md, {args.out}.html")


if __name__ == "__main__":
    main()

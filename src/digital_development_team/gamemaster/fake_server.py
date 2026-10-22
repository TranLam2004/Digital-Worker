"""HTTP bọc FakeGameMaster theo đường dẫn mặc định của GameMasterCfg — luyện tập trên máy đội và test HttpGameMaster.

`ddt match fake-server --scenario testdata/matches/practice.json --team-repo <repo>` · Admin: POST /admin/start ·
/admin/pause · /admin/resume (chỉ có ở bản giả lập).
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from digital_development_team.gamemaster.fake import FakeGameMaster
from digital_development_team.gamemaster.http_client import GameMasterError

_ITEM = r"/matches/(?P<m>[^/]+)/backlog/(?P<i>[^/]+)"


def _snapshot_json(fake: FakeGameMaster) -> dict[str, Any]:
    s = fake.snapshot()
    return {
        "id": s.match_id,
        "status": s.status.value,
        "serverTime": s.server_time.isoformat(),
        "endsAt": s.ends_at.isoformat() if s.ends_at else None,
        "applicationBrief": s.brief,
        "repository": s.repository,
        "examInfo": s.exam_info,
        "backlog": [
            {
                "id": i.id,
                "type": i.type.value,
                "title": i.title,
                "description": i.description,
                "status": i.status.value,
                "points": i.points,
                "revealedAt": i.revealed_at.isoformat() if i.revealed_at else None,
                "deadlineAt": i.deadline_at.isoformat() if i.deadline_at else None,
            }
            for i in s.items
        ],
    }


def make_server(fake: FakeGameMaster, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # im lặng
            return

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return data if isinstance(data, dict) else {}

        def _guard(self, fn: Any) -> None:
            try:
                fn()
            except GameMasterError as e:
                self._send(e.status or 400, {"error": e.message})

        def do_GET(self) -> None:
            def go() -> None:
                if m := re.fullmatch(rf"{_ITEM}/submissions/(?P<s>[^/]+)", self.path):
                    g = fake.submission(m["i"], m["s"])
                    self._send(200, {"gradeStatus": g.status.value, "score": g.score})
                elif re.fullmatch(r"/matches/[^/]+", self.path):
                    self._send(200, _snapshot_json(fake))
                else:
                    self._send(404, {"error": "not found"})

            self._guard(go)

        def do_POST(self) -> None:
            def go() -> None:
                admin = {
                    "/admin/start": fake.start_match,
                    "/admin/pause": fake.pause,
                    "/admin/resume": fake.resume,
                }
                if self.path in admin:
                    admin[self.path]()
                    self._send(200, {"ok": True})
                elif m := re.fullmatch(rf"{_ITEM}/start-working", self.path):
                    st = fake.start_working(m["i"])
                    self._send(200, {"submitTemplate": st.submit_template, "mvs": list(st.mvs)})
                elif m := re.fullmatch(rf"{_ITEM}/submissions", self.path):
                    body = self._body()
                    sr = fake.submit(
                        m["i"],
                        git_revision=str(body.get("gitRevision", "")),
                        answer=dict(body.get("answer") or {}),
                        idempotency_key=str(self.headers.get("Idempotency-Key", "")),
                    )
                    self._send(200, {"submissionId": sr.submission_id, "status": sr.status})
                else:
                    self._send(404, {"error": "not found"})

            self._guard(go)

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(fake: FakeGameMaster, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = make_server(fake, port=port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, p = srv.server_address[:2]
    return srv, f"http://{host!s}:{p}"

"""An in-process MCP server that stands in for the AgentCore Gateway in tests.

Runs a real FastMCP streamable-HTTP server on a background thread and exposes the same tool
names the Gateway would (``germany___search_debates`` / ``germany___get_debate_text``), so the
tests drive the REAL Strands MCPClient + agent event loop over real sockets — only the LLM and
the upstream parliament API are faked. This is the multi-government analogue of the old
"real DIP tool over a real socket" e2e approach.

The tools return canned envelopes that match the shared SpeechResult contract, so the whole
chain (MCP transport -> tool result -> _extract_sources -> citations) is exercised end to end.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable, Optional

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from strands.tools.mcp import MCPClient

# A canned Germany search result matching the normalized contract.
GERMANY_RESULT = {
    "jurisdiction": "de",
    "jurisdiction_label": "German Bundestag",
    "doc_id": "aktivitaet:1784775@protokoll:5798",
    "source_url": "https://dserver.bundestag.de/btp/21/21083.pdf#P.10089",
    "title": "Befragung der Bundesregierung",
    "date": "2026-06-11",
    "snippet": None,
    "speaker": "Hubertus Heil",
    "group": "SPD",
    "party": None,
    "role": "Bundesminister",
    "chamber": "Bundestag",
    "term": "21",
    "session_ref": "21/83, p. 10089D",
    "language_original": "de",
    "language_text": "de",
    "is_translation": False,
    "text_status": "final",
    "extras": {"aktivitaetsart": "Rede"},
}


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class InProcessGateway:
    """A FastMCP server exposing Germany tools, plus a Strands MCPClient wired to it.

    ``calls`` records every tool invocation so tests can assert what the agent actually called.
    """

    def __init__(self) -> None:
        self.port = _free_port()
        self.calls: list[dict[str, Any]] = []
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self.mcp = FastMCP("gateway-under-test", host="127.0.0.1", port=self.port)
        self._register_tools()

    def _register_tools(self) -> None:
        gw = self

        @self.mcp.tool(name="germany___search_debates")
        def germany_search_debates(
            query: str | None = None,
            speaker: str | None = None,
            date_start: str | None = None,
            date_end: str | None = None,
            term: str | None = None,
            chamber: str | None = None,
            cursor: str | None = None,
            max_results: int = 5,
        ) -> dict:
            """Search German Bundestag floor debates and speeches (DIP)."""
            gw.calls.append({
                "tool": "germany___search_debates",
                "args": {"query": query, "speaker": speaker, "date_start": date_start,
                         "date_end": date_end, "term": term, "max_results": max_results},
            })
            return {"results": [GERMANY_RESULT], "total": 332, "jurisdiction": "de",
                    "truncated": True, "cursor": "NEXT"}

        @self.mcp.tool(name="germany___get_debate_text")
        def germany_get_debate_text(doc_id: str, query: str | None = None,
                                    max_chars: int = 6000) -> dict:
            """Fetch the verbatim text of a German Bundestag protocol by doc_id."""
            gw.calls.append({"tool": "germany___get_debate_text", "args": {"doc_id": doc_id}})
            return {"results": [], "total": 0, "jurisdiction": "de", "doc_id": doc_id,
                    "title": "Protokoll 21/83", "date": "2026-06-11",
                    "text": "Klimaschutz ist eine zentrale Aufgabe dieser Legislaturperiode.",
                    "language_original": "de", "language_text": "de"}

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        app = self.mcp.streamable_http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # Wait for the port to accept connections.
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/mcp", timeout=0.2)
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("in-process MCP gateway did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def make_client(self, transport_factory: Callable[[], Any]) -> MCPClient:
        return MCPClient(transport_factory, startup_timeout=30)

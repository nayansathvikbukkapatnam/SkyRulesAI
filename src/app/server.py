"""SkyRules AI web server (CLAUDE.md section 5, UI_SPEC section 5).

    make app   ->   http://localhost:8000

GET  /            the static page (src/app/static/index.html)
GET  /api/health  what is loaded: configs, LLM provider and model, local index files
POST /api/ask     {question, history, pilot_type, compare} -> server-sent events:
                    event: stage   {name, status, ms, config}   one per stage as it happens
                    event: answer  the Answer from answer(), plus "config"
                    event: error   {message}                    if answer() raised
                    event: done    {}                           end of the stream

The main answer uses config b3. With compare, a second answer from b2 follows,
with "b3_check_flags": the B3 checks run over the B2 sentences (UI_SPEC 4.7).

Configs, the retriever, the reranker and the LLM client are created once at
startup. answer() runs in a worker thread and its on_stage callback feeds the
event stream, so the page sees each stage live. Nothing here logs or returns a key.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.common import paths
from src.common.config import load_config
from src.generate import answer as answer_mod

log = logging.getLogger(__name__)

STATIC_DIR = paths.ROOT / "src" / "app" / "static"
MAIN_CONFIG = "b3"
COMPARE_CONFIG = "b2"
# The page keeps the last 6 turns (UI_SPEC 5); a turn is a question and its answer.
MAX_HISTORY_MESSAGES = 12

# Flag text under a B2 sentence for each B3 check it would fail (UI_SPEC 4.7).
FLAG_TEXT = {
    "citation_validity": "No citation",
    "claim_support": "Claim not supported by cited passage",
    "number_date": "Number not found in source",
    "quote_limit": "Quote over 25 words",
}

AnswerFn = Callable[..., dict]


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list)
    pilot_type: Literal["recreational", "part107", "not_sure"] = "not_sure"
    compare: bool = False


@dataclass
class State:
    """Everything the server loads once at startup."""

    configs: dict[str, dict] = field(default_factory=dict)
    answer_fn: AnswerFn | None = None
    client: Any = None
    retrieve_fn: Callable | None = None
    rerank_fn: Callable | None = None
    llm_error: str | None = None


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def b3_check_flags(b2: dict, b3_config: dict, client: Any) -> list[list[str]]:
    """For each B2 sentence, the B3 checks it would fail, as UI flag text."""
    if b2.get("status") != "answered":
        return []
    basic, claims = answer_mod._validate(b3_config)
    checked = answer_mod._run_checks(
        b2["sentences"], b2["trace"]["reranked"], basic, claims, answer_mod._MeteredClient(client)
    )
    flags = []
    for v in checked["sentences"]:
        failed = []
        if "error" in v:  # claim_check: no citation, or a chunk that was not retrieved
            failed.append("citation_validity")
        for name in checked["requested"]:
            verdict = v.get(name)
            key = "supported" if name == "claim_support" else "ok"
            if verdict is not None and not verdict[key]:
                failed.append(name)
        flags.append(list(dict.fromkeys(FLAG_TEXT[n] for n in failed)))
    return flags


def _load(state: State) -> None:
    """Load configs, the LLM client and retrieval once.

    A missing key or index is reported by /api/health, not fatal, so the page still loads.
    """
    for name in (MAIN_CONFIG, COMPARE_CONFIG):
        state.configs.setdefault(name, load_config(name))
    if state.answer_fn is None:
        state.answer_fn = answer_mod.answer
    if state.client is None:
        try:
            from src.generate.llm import LLMClient

            state.client = LLMClient(state.configs[MAIN_CONFIG])
        except Exception as e:  # e.g. LLM_PROVIDER not set; the message never holds a key
            state.llm_error = f"{type(e).__name__}: {e}"
            log.warning("LLM client not available: %s", state.llm_error)
    if state.retrieve_fn is None or state.rerank_fn is None:
        from src.retrieve.rerank import Reranker
        from src.retrieve.retrieve import Retriever

        default = load_config("default")
        retriever, reranker = Retriever(default), Reranker.from_config(default)
        state.retrieve_fn = state.retrieve_fn or retriever.retrieve
        state.rerank_fn = state.rerank_fn or reranker.rerank
        if paths.CHROMA.exists():
            try:  # warm up so the first question does not pay for loading models
                _ = retriever.store
                _ = reranker.model
            except Exception as e:
                log.warning("could not warm up retrieval: %s: %s", type(e).__name__, e)


def _index_status(configs: dict[str, dict]) -> dict[str, Any]:
    strategies = sorted({c["chunk_strategy"] for c in configs.values() if c.get("chunk_strategy")})
    return {
        "chroma": paths.CHROMA.exists(),
        "bm25": {s: paths.bm25_path(s).exists() for s in strategies},
    }


def create_app(
    answer_fn: AnswerFn | None = None,
    configs: dict[str, dict] | None = None,
    client: Any = None,
    retrieve_fn: Callable | None = None,
    rerank_fn: Callable | None = None,
) -> FastAPI:
    """The FastAPI app. Tests pass stand-ins; make app uses the real pipeline."""
    state = State(
        configs=dict(configs or {}),
        answer_fn=answer_fn,
        client=client,
        retrieve_fn=retrieve_fn,
        rerank_fn=rerank_fn,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _load(state)
        yield

    app = FastAPI(title="SkyRules AI", lifespan=lifespan)
    app.state.skyrules = state
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_model=None)
    def index() -> FileResponse | HTMLResponse:
        page = STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(page)
        return HTMLResponse(
            "<!doctype html><title>SkyRules AI</title><p>Frontend not built yet.</p>"
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        main = state.configs.get(MAIN_CONFIG, {})
        return {
            "status": "ok" if state.llm_error is None else "degraded",
            "configs": {"main": MAIN_CONFIG, "compare": COMPARE_CONFIG},
            "llm": {
                "provider": main.get("llm_provider"),
                "model": main.get("generator_model"),
                "ready": state.client is not None,
                "error": state.llm_error,
            },
            "index": _index_status(state.configs),
        }

    @app.post("/api/ask")
    def ask(req: AskRequest) -> StreamingResponse:
        history = [t.model_dump() for t in req.history][-MAX_HISTORY_MESSAGES:]
        return StreamingResponse(
            _stream(state, req.question, history, req.pilot_type, req.compare),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _stream(
    state: State, question: str, history: list[dict], pilot_type: str, compare: bool
) -> Iterator[str]:
    """Run answer() in a thread and yield its stage events live, then the answer(s)."""
    events: queue.Queue = queue.Queue()
    runs = [MAIN_CONFIG] + ([COMPARE_CONFIG] if compare else [])

    def worker() -> None:
        try:
            if state.client is None:
                raise RuntimeError(f"the LLM client is not available ({state.llm_error})")
            for name in runs:
                cfg = state.configs[name]

                def on_stage(stage: str, status: str, ms: float, _name: str = name) -> None:
                    events.put(
                        ("stage", {"name": stage, "status": status, "ms": ms, "config": _name})
                    )

                result = state.answer_fn(
                    question,
                    history,
                    cfg,
                    pilot_type=pilot_type,
                    on_stage=on_stage,
                    client=state.client,
                    retrieve_fn=state.retrieve_fn,
                    rerank_fn=state.rerank_fn,
                )
                result = {**result, "config": name}
                if name == COMPARE_CONFIG and compare:
                    try:
                        result["b3_check_flags"] = b3_check_flags(
                            result, state.configs[MAIN_CONFIG], state.client
                        )
                    except Exception as e:  # flags are extra; the B2 answer still goes out
                        result["b3_check_flags"] = None
                        result["b3_check_error"] = f"{type(e).__name__}: {e}"
                events.put(("answer", result))
        except Exception as e:  # the page shows a refusal card; never a partial answer
            log.exception("answer() failed")
            events.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            events.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        event, data = events.get()
        yield _sse(event, data)
        if event == "done":
            return


app = create_app()

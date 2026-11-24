"""Serve the sample daily digest workflow over HTTP with fake integrations."""
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from daily_sender_digest import STALE, TOKEN, Store
from demo_daily_sender_digest import (
    INSTRUCTION, MESSAGE_ID, OPERATOR, RECEIVED, SCOPE, SampleInbox, fake_model,
)


class Demo:
    def __init__(self, directory):
        self.store = Store(Path(directory).resolve(), SCOPE, now=lambda: RECEIVED.timestamp() + 60)
        self.inbox = SampleInbox()
        self.outbox = []

    def send(self, draft, account):
        self.outbox.append({**draft, "account": account})
        return "sent"

    def click(self, token):
        return self.store.callback({**OPERATOR, "data": token}, self.inbox, send=self.send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with TemporaryDirectory(prefix="email-agent-api-", dir="/tmp") as directory:
        app.state.demo = Demo(directory)
        yield


app = FastAPI(title="Email Agent Draft Demo", lifespan=lifespan, docs_url=None, redoc_url=None)


class EditRequest(BaseModel):
    edit_token: str = Field(pattern=f"^{TOKEN.pattern}$")
    instruction: str = Field(examples=[INSTRUCTION])


class ApprovalRequest(BaseModel):
    approval_token: str = Field(pattern=f"^{TOKEN.pattern}$")


def draft_response(record):
    reviewing = record["status"] == "awaiting_review"
    return {
        "status": record["status"],
        "version": record["version"],
        "draft": record["draft"] if reviewing else None,
        "edit_token": record["actions"]["edit"] if reviewing and record["version"] == 1 else None,
        "approval_token": record["actions"]["send"] if reviewing else None,
    }


@app.post("/draft")
def create_draft(request: Request):
    demo = request.app.state.demo
    store = demo.store
    with store.lock(timeout=5):
        report = store.report(demo.inbox, notify=lambda view: None, generate=fake_model)
        record = store.load()["records"].get(MESSAGE_ID)
        if report["failed"] or report["attention"] or record is None:
            raise HTTPException(500, "Could not report the sample draft")
        return draft_response(record)


@app.post("/draft/edit")
def edit_draft(payload: EditRequest, request: Request):
    if payload.instruction != INSTRUCTION:
        raise HTTPException(422, f"The sample edit instruction is {INSTRUCTION!r}")

    demo = request.app.state.demo
    store = demo.store
    with store.lock(timeout=5):
        state = store.load()
        record = state["records"].get(MESSAGE_ID)
        if (record is None or record["status"] != "awaiting_review" or record["version"] != 1
                or record["actions"]["edit"] != payload.edit_token):
            raise HTTPException(409, "The edit token is stale or the sample draft is unavailable for editing")

        demo.click(payload.edit_token)
        session = store.edit_session(OPERATOR)
        message_id = str(len(state["text_events"]) + 1)
        store.revise({**OPERATOR, "message_id": message_id, "text": payload.instruction,
                      "edit_token": session["edit_token"]}, demo.inbox, generate=fake_model)
        updated = store.load()["records"][MESSAGE_ID]
        if updated["version"] == record["version"]:
            raise HTTPException(500, "Could not revise the sample draft")
        return draft_response(updated)


@app.post("/draft/approve")
def approve_draft(payload: ApprovalRequest, request: Request):
    demo = request.app.state.demo
    store = demo.store
    with store.lock(timeout=5):
        record = store.load()["records"].get(MESSAGE_ID)
        token = payload.approval_token
        if record and token in (record["actions"]["edit"], record["actions"]["skip"]):
            raise HTTPException(422, "An approval token is required")

        result = demo.click(token)
        status = "stale" if result["text"] == STALE else store.load()["records"][MESSAGE_ID]["status"]
        return {"status": status, "simulated_send_count": len(demo.outbox)}


@app.get("/outbox")
def read_outbox(request: Request):
    demo = request.app.state.demo
    with demo.store.lock(timeout=5):
        return {"simulated_send_count": len(demo.outbox), "messages": list(demo.outbox)}

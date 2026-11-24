"""Bounded revision diagnostics; never derive display text from exceptions."""
from contextlib import contextmanager
from enum import Enum

from read_latest_email_from_sender import PipelineError


class RevisionCode(str, Enum):
    SOURCE = "R_SOURCE"
    MODEL_PROCESS = "R_MODEL_PROCESS"
    OUTER_ENVELOPE = "R_OUTER_ENVELOPE"
    PAYLOAD = "R_PAYLOAD"
    TEXT_CONTRACT = "R_TEXT_CONTRACT"
    STATE = "R_STATE"


class RevisionFailure(PipelineError):
    def __init__(self, code):
        self.code = code if isinstance(code, RevisionCode) else RevisionCode.STATE
        super().__init__(self.code.value, 8)


@contextmanager
def revision_stage(code):
    try:
        yield
    except RevisionFailure:
        raise
    except Exception:
        raise RevisionFailure(code) from None


def safe_revision_code(error):
    if isinstance(error, RevisionFailure) and isinstance(error.code, RevisionCode):
        return error.code.value
    return RevisionCode.STATE.value


def revision_failure_text(error):
    return "Chưa chỉnh được; bản hiện tại được giữ lại. Mã: " + safe_revision_code(error)

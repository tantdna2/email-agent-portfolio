"""Separate, narrowly scoped Gmail label writer. Construct ONLY after confirmation."""
import os
from pathlib import Path
import re
import stat

from new_email_pipeline import require, valid_id
from run_email_agent import outside_repo, parse_json

MODIFY = "https://www.googleapis.com/auth/gmail.modify"
BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"


def load_label_authorized_user_file():
    name = os.environ.get("GMAIL_LABEL_AUTHORIZED_USER_FILE", "")
    require(bool(name), "label_credentials_required")
    original = Path(name).expanduser()
    require(original.is_absolute() and not any(p.is_symlink() for p in (original, *original.parents)),
            "label_credential_symlink")
    path = outside_repo(original)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        mode = os.fstat(stream.fileno())
        require(stat.S_ISREG(mode.st_mode) and stat.S_IMODE(mode.st_mode) == 0o600
                and mode.st_uid == os.getuid(), "label_credential_permissions")
        info = parse_json(stream.read())
    require(isinstance(info, dict) and info.get("type") == "authorized_user", "label_credentials_invalid")
    scopes = info.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    allowed = {MODIFY, "openid", "email", "profile",
               "https://www.googleapis.com/auth/userinfo.email",
               "https://www.googleapis.com/auth/userinfo.profile"}
    require(isinstance(scopes, list) and all(isinstance(x, str) for x in scopes)
            and MODIFY in scopes and set(scopes) <= allowed, "label_scope_invalid")
    require(all(isinstance(info.get(k), str) and info[k]
                for k in ("client_id", "client_secret", "refresh_token")), "label_credentials_invalid")
    return info


def source_labels(raw, message_id, thread_id):
    require(isinstance(raw, dict) and raw.get("id") == message_id
            and raw.get("threadId") == thread_id and valid_id(message_id) and valid_id(thread_id),
            "label_source_mismatch")
    labels = raw.get("labelIds")
    require(isinstance(labels, list) and all(isinstance(x, str) for x in labels)
            and len(labels) == len(set(labels)) and "DRAFT" not in labels, "label_source_invalid")
    return labels


def resolve_user_label(value, name):
    require(isinstance(value, dict) and isinstance(value.get("labels"), list), "label_list_invalid")
    labels = value["labels"]
    require(all(isinstance(x, dict) and isinstance(x.get("id"), str)
                and isinstance(x.get("name"), str) and x.get("type") in {"user", "system"}
                for x in labels), "label_list_invalid")
    matches = [x for x in labels if x["name"] == name]
    require(len(matches) == 1 and matches[0]["type"] == "user", "label_resolution_failed")
    label_id = matches[0]["id"]
    require(bool(re.fullmatch(r"Label_[A-Za-z0-9_-]+", label_id))
            and sum(x["id"] == label_id for x in labels) == 1, "label_resolution_failed")
    return label_id


class LabelWriter:
    """No general request method, sending, threads, drafts or label administration."""
    def __init__(self, session):
        self._session = session

    def prepare(self, message_id, thread_id, name, account):
        require(valid_id(message_id) and valid_id(thread_id), "label_source_invalid")
        def read(path, **kwargs):
            response = self._session.get(BASE + path, timeout=60, allow_redirects=False, **kwargs)
            require(response.status_code == 200, "label_read_failed")
            return parse_json(response.text)
        profile = read("profile")
        require(isinstance(profile, dict) and isinstance(profile.get("emailAddress"), str)
                and profile["emailAddress"].casefold() == account.casefold(), "label_account_mismatch")
        label_id = resolve_user_label(read("labels"), name)
        labels = source_labels(read("messages/" + message_id, params={"format": "minimal"}),
                               message_id, thread_id)
        return label_id, label_id in labels

    def add(self, message_id, thread_id, label_id):
        require(valid_id(message_id) and valid_id(thread_id)
                and bool(re.fullmatch(r"Label_[A-Za-z0-9_-]+", label_id)), "label_write_invalid")
        # No redirects or HTTP/auth retries: a dispatched POST is never replayed.
        response = self._session.post(BASE + "messages/" + message_id + "/modify",
                                      json={"addLabelIds": [label_id], "removeLabelIds": []},
                                      timeout=60, allow_redirects=False)
        require(response.status_code == 200, "label_write_uncertain")
        labels = source_labels(parse_json(response.text), message_id, thread_id)
        require(label_id in labels, "label_write_uncertain")


def writer_from_environment():
    info = load_label_authorized_user_file()
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials
    credentials = Credentials.from_authorized_user_info(info, scopes=[MODIFY])
    return LabelWriter(AuthorizedSession(credentials, max_refresh_attempts=0))

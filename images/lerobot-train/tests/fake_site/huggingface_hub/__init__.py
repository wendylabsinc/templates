"""A fake of the parts of huggingface_hub 1.33 that entrypoint.py calls.

The tests put this directory first on PYTHONPATH. The Hub's state is a JSON
file named by FAKE_HF_STATE:

    {"token": "<the valid token>",
     "writable_namespaces": ["team"],
     "datasets": {"team/ds": {"private": false, "gated": false, "server_error": false,
                              "refs": {"main": "<40-hex>"}, "commits": ["<40-hex>", ...],
                              "info": {...meta/info.json...}}},
     "commit_failures": 0, "commit_failure_status": 503,
     "leak_token_in_errors": false}

Every call is appended to FAKE_HF_CALLS as a JSON line. Tokens are never
logged; `token_ok` records whether a valid one was used.

test_entrypoint.HubSignatureTest checks that the real huggingface_hub
accepts every keyword argument the entrypoint passes here.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import errors

__version__ = "0.0.0+fake"


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def _error(cls, status, message):
    return cls(message, response=_Response(status))


def _load():
    return json.loads(Path(os.environ["FAKE_HF_STATE"]).read_text())


def _store(state):
    Path(os.environ["FAKE_HF_STATE"]).write_text(json.dumps(state))


def _log(**entry):
    with open(os.environ["FAKE_HF_CALLS"], "a") as f:
        f.write(json.dumps(entry) + "\n")


def _authenticate(state, token):
    """The valid token, None for anonymous; a wrong token is a 401, as on the Hub."""
    if token is None:
        token = os.environ.get("HF_TOKEN") or None
    if token is None:
        return None
    if token != state["token"]:
        message = "401 Client Error: Unauthorized. Invalid credentials in Authorization header"
        if state.get("leak_token_in_errors"):
            message += f" (Bearer {token})"
        raise _error(errors.HfHubHTTPError, 401, message)
    return token


def _dataset(state, repo_id, token):
    dataset = state["datasets"].get(repo_id)
    if dataset is None or (dataset.get("private") and token is None):
        message = f"404 Client Error. Repository Not Found for url: https://huggingface.co/api/datasets/{repo_id}."
        if state.get("leak_token_in_errors") and os.environ.get("HF_TOKEN"):
            message += f" Authorization: Bearer {os.environ['HF_TOKEN']}"
        raise _error(errors.RepositoryNotFoundError, 404, message)
    if dataset.get("gated"):
        raise _error(errors.GatedRepoError, 403, f"Access to dataset {repo_id} is restricted.")
    if dataset.get("server_error"):
        message = f"500 Server Error: Internal Server Error for url: https://huggingface.co/api/datasets/{repo_id}"
        if state.get("leak_token_in_errors") and token:
            message += f" (Authorization: Bearer {token})"
        raise _error(errors.HfHubHTTPError, 500, message)
    return dataset


def _commit_of(dataset, repo_id, revision):
    revision = revision or "main"
    refs = dataset["refs"]
    if revision in refs:
        return refs[revision]
    if revision in refs.values() or revision in dataset.get("commits", []):
        return revision  # any commit stays readable by its id, as on the Hub
    raise _error(errors.RevisionNotFoundError, 404, f"Invalid rev id: {revision} for {repo_id}")


class CommitOperationAdd:
    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo = path_in_repo
        self.path_or_fileobj = path_or_fileobj

    def read(self):
        if isinstance(self.path_or_fileobj, bytes):
            return self.path_or_fileobj
        return Path(self.path_or_fileobj).read_bytes()


class HfApi:
    def __init__(self, endpoint=None, token=None, **kwargs):
        self.token = token

    def _token(self, token):
        return token if token is not None else self.token

    def dataset_info(self, repo_id, *, revision=None, timeout=None, files_metadata=False, expand=None, token=None):
        state = _load()
        valid = _authenticate(state, self._token(token))
        _log(call="dataset_info", repo_id=repo_id, revision=revision, token_ok=valid is not None)
        dataset = _dataset(state, repo_id, valid)
        return SimpleNamespace(id=repo_id, sha=_commit_of(dataset, repo_id, revision))

    def create_repo(self, repo_id, *, token=None, private=None, repo_type=None, exist_ok=False, **kwargs):
        state = _load()
        valid = _authenticate(state, self._token(token))
        _log(call="create_repo", repo_id=repo_id, repo_type=repo_type, exist_ok=exist_ok, token_ok=valid is not None)
        if valid is None:
            raise _error(errors.HfHubHTTPError, 401, "401 Client Error: Unauthorized. You must be authenticated.")
        if repo_id.split("/")[0] not in state.get("writable_namespaces", []):
            raise _error(
                errors.HfHubHTTPError,
                403,
                f"403 Forbidden: You don't have the rights to create a model under {repo_id.split('/')[0]}",
            )
        return f"https://huggingface.co/{repo_id}"

    def create_commit(
        self,
        repo_id,
        operations,
        *,
        commit_message,
        commit_description=None,
        token=None,
        repo_type=None,
        revision=None,
        create_pr=None,
        num_threads=5,
        parent_commit=None,
        run_as_future=False,
    ):
        state = _load()
        valid = _authenticate(state, self._token(token))
        if valid is None:
            raise _error(errors.HfHubHTTPError, 401, "401 Client Error: Unauthorized.")
        if state.get("commit_failures", 0) > 0:
            state["commit_failures"] -= 1
            _store(state)
            _log(call="create_commit", repo_id=repo_id, failed=True)
            status = state.get("commit_failure_status", 503)
            raise _error(
                errors.HfHubHTTPError,
                status,
                f"{status} Server Error for url: https://huggingface.co/api/models/{repo_id}/commit/main",
            )
        files = {}
        readme = None
        for operation in operations:
            data = operation.read()
            files[operation.path_in_repo] = hashlib.sha256(data).hexdigest()
            if operation.path_in_repo == "README.md":
                readme = data.decode("utf-8")
        oid = hashlib.sha1(json.dumps(files, sort_keys=True).encode() + commit_message.encode()).hexdigest()
        _log(
            call="create_commit",
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message=commit_message,
            files=sorted(files),
            readme=readme,
            oid=oid,
            token_ok=True,
        )
        return SimpleNamespace(oid=oid, commit_url=f"https://huggingface.co/{repo_id}/commit/{oid}")


def hf_hub_download(repo_id, filename, *, repo_type=None, revision=None, token=None, **kwargs):
    state = _load()
    valid = _authenticate(state, token)
    _log(call="hf_hub_download", repo_id=repo_id, filename=filename, repo_type=repo_type, revision=revision)
    dataset = _dataset(state, repo_id, valid)
    _commit_of(dataset, repo_id, revision)
    info = dataset.get("info")
    if filename != "meta/info.json" or info is None:
        raise _error(
            errors.RemoteEntryNotFoundError, 404, f"404 Client Error. Entry Not Found for url: {repo_id}/{filename}"
        )
    fd, path = tempfile.mkstemp(suffix=".json", dir=os.environ.get("FAKE_HF_CACHE"))
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    return path

"""The exception classes of huggingface_hub.errors (1.33) the entrypoint handles, with the same hierarchy."""


class HfHubHTTPError(Exception):
    def __init__(self, message, *, response=None, server_message=None):
        super().__init__(message)
        self.response = response
        self.server_message = server_message


class RepositoryNotFoundError(HfHubHTTPError):
    pass


class GatedRepoError(RepositoryNotFoundError):
    pass


class RevisionNotFoundError(HfHubHTTPError):
    pass


class EntryNotFoundError(Exception):
    pass


class RemoteEntryNotFoundError(HfHubHTTPError, EntryNotFoundError):
    pass

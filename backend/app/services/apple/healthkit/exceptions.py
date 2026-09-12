class TransientImportError(Exception):
    """An SDK import step failed for a reason likely to succeed on retry.

    ``import_service.import_data_from_request`` re-raises these instead of
    converting them into a 400 response, so ``process_sdk_upload``'s
    ``autoretry_for`` policy actually gets a chance to retry the batch. Use
    this (not a bare ``Exception``) for lock contention, transient DB writes,
    and similar infra hiccups — never for a malformed payload, which should
    stay a permanent 400.
    """


class SleepLockTimeoutError(TransientImportError):
    """Could not acquire the per-user Redis sleep-processing lock in time.

    Previously this path logged a warning and silently dropped the whole
    sleep batch with no retry (see fix OW-02 in the 2026-09-12 pipeline audit).
    """


class SleepPersistenceError(TransientImportError):
    """Failed to write a finalized sleep session (or its merge) to the database.

    Previously this path logged the exception and returned as if nothing had
    happened, leaving Redis state intact for the next periodic sweep but
    giving the caller (and any inline finalize) no signal that anything went
    wrong (see fix OW-03).
    """

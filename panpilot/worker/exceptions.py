"""Shared exceptions for the worker package."""


class TicketBusy(Exception):
    """
    Raised when a ticket is already in PENDING_EVALUATION.

    Not a processing failure — the DLQ catches this and skips without
    incrementing the backoff attempt counter.  The main worker loop
    catches it and leaves the event unprocessed to retry next poll.
    """

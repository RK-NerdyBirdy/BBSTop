"""Notification hook for project-module events.

Every state change calls `notify(...)`. For now it only logs; email delivery (outbox +
background sender) will be implemented here later without touching the callers.
"""

import logging

logger = logging.getLogger("bbstop.notify")


def notify(event: str, recipients: list[str], context: dict | None = None) -> None:
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    logger.info("notify event=%s to=%s context=%s", event, ",".join(recipients), context or {})

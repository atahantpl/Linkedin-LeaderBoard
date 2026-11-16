"""
Post LinkedIn Games leaderboard summaries to Slack via Incoming Webhook.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class SlackNotifyError(RuntimeError):
    pass


def slack_webhook_url() -> str | None:
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    return url or None


def post_message(text: str, *, webhook_url: str | None = None) -> None:
    """
    Send a plain-text/mrkdwn message to a Slack channel via Incoming Webhook.

    Create a webhook: Slack app → Incoming Webhooks → Add to your channel/group.
    Set SLACK_WEBHOOK_URL in scripts/.env (never commit the URL).
    """
    url = (webhook_url or slack_webhook_url() or "").strip()
    if not url:
        raise SlackNotifyError(
            "SLACK_WEBHOOK_URL is not set. Add it to scripts/.env:\n"
            "  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/..."
        )

    payload = json.dumps({"text": text, "mrkdwn": True}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            if body and body != "ok":
                logger.debug("Slack response: %s", body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SlackNotifyError(f"Slack webhook failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SlackNotifyError(f"Could not reach Slack webhook: {exc}") from exc

    logger.info("Leaderboard posted to Slack.")

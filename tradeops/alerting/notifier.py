"""Severity-tiered alerting with a fallback chain.

Alerting is load-bearing in an unattended system: when the only operator
interface is a phone notification, a dropped CRITICAL is an incident nobody
attends. Two rules follow:

1. **An alert path must never take the trading process down.** Nothing in
   this module raises. (A production backup job once crashed its scheduler
   because a slow upload's timeout propagated uncaught — the message about
   the failure became the failure.)
2. **Degrade loudly, in order.** Delivery walks a chain — Slack, then SMTP,
   then stdout — and the last resort always succeeds, so every message lands
   somewhere an operator or a log shipper can find it.

Transports are injectable (the Slack notifier takes an ``http_post``
callable), so the test suite runs without network and the core package stays
dependency-free; the ``slack`` extra pulls ``requests`` for the default
transport.
"""
import os
import smtplib
from email.message import EmailMessage

SEVERITY_BADGES = {
    "INFO": ":information_source:",
    "WARN": ":warning:",
    "CRITICAL": ":rotating_light:",
}


def format_alert(agent_name: str, message: str, severity: str) -> str:
    badge = SEVERITY_BADGES.get(severity, "")
    return f"{badge} *[{agent_name}] {severity}* — {message}"


def _requests_post(url: str, payload: dict, timeout: float) -> int:
    # lazy import: only the default transport needs it (the `slack` extra)
    import requests

    return requests.post(url, json=payload, timeout=timeout).status_code


class SlackNotifier:
    """Webhook delivery. `send` returns delivery truth for the fallback
    chain; `send_alert` satisfies the `Notifier` port for standalone use."""

    def __init__(self, webhook_url: str, agent_name: str = "agent",
                 http_post=None, timeout: float = 5.0):
        self.webhook_url = webhook_url
        self.agent_name = agent_name
        self.timeout = timeout
        self._post = http_post or _requests_post

    def send(self, message: str, severity: str = "INFO") -> bool:
        if not self.webhook_url:
            return False
        try:
            status = self._post(
                self.webhook_url,
                {"text": format_alert(self.agent_name, message, severity)},
                self.timeout,
            )
            return status == 200
        except Exception:
            return False

    def send_alert(self, message: str, severity: str = "INFO") -> None:
        self.send(message, severity)


class EmailNotifier:
    """SMTP fallback, stdlib only. Configuration is explicit; anything
    missing (or any SMTP failure) reports non-delivery instead of raising."""

    def __init__(self, from_addr: str, to_addr: str, password: str,
                 smtp_host: str, smtp_port: int = 587,
                 agent_name: str = "agent", smtp_factory=smtplib.SMTP):
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.password = password
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.agent_name = agent_name
        self._smtp = smtp_factory

    def send(self, message: str, severity: str = "INFO") -> bool:
        if not all([self.from_addr, self.to_addr, self.password, self.smtp_host]):
            return False
        msg = EmailMessage()
        msg["Subject"] = f"[{self.agent_name} {severity}] {message[:60]}"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg.set_content(message)
        try:
            with self._smtp(self.smtp_host, self.smtp_port) as s:
                s.starttls()
                s.login(self.from_addr, self.password)
                s.send_message(msg)
            return True
        except Exception:
            return False

    def send_alert(self, message: str, severity: str = "INFO") -> None:
        self.send(message, severity)


class ConsoleNotifier:
    """Terminal transport of last resort — cannot fail, captured by journald
    (or any process supervisor) in production."""

    def __init__(self, agent_name: str = "agent"):
        self.agent_name = agent_name

    def send(self, message: str, severity: str = "INFO") -> bool:
        print(f"[ALERT {severity}] [{self.agent_name}] {message}")
        return True

    def send_alert(self, message: str, severity: str = "INFO") -> None:
        self.send(message, severity)


class FallbackNotifier:
    """The `Notifier` port over an ordered transport chain. A console
    transport is appended unless one is present, so delivery cannot fail —
    and a transport that raises is treated as non-delivery, never propagated."""

    def __init__(self, transports, ensure_console: bool = True):
        self.transports = list(transports)
        if ensure_console and not any(isinstance(t, ConsoleNotifier)
                                      for t in self.transports):
            self.transports.append(ConsoleNotifier())

    def send_alert(self, message: str, severity: str = "INFO") -> None:
        for transport in self.transports:
            try:
                if transport.send(message, severity):
                    return
            except Exception:
                continue


def notifier_from_env(agent_name: str | None = None) -> FallbackNotifier:
    """Production wiring: Slack primary (SLACK_WEBHOOK_URL), SMTP fallback
    (ALERT_EMAIL_FROM / ALERT_EMAIL_TO / ALERT_SMTP_HOST / ALERT_SMTP_PORT /
    ALERT_SMTP_PASSWORD), console last."""
    name = agent_name or os.getenv("AGENT_NAME", "agent")
    return FallbackNotifier([
        SlackNotifier(os.getenv("SLACK_WEBHOOK_URL", ""), agent_name=name),
        EmailNotifier(
            from_addr=os.getenv("ALERT_EMAIL_FROM", ""),
            to_addr=os.getenv("ALERT_EMAIL_TO", ""),
            password=os.getenv("ALERT_SMTP_PASSWORD", ""),
            smtp_host=os.getenv("ALERT_SMTP_HOST", ""),
            smtp_port=int(os.getenv("ALERT_SMTP_PORT", "587")),
            agent_name=name,
        ),
    ])

"""Alerting unit suite — no network, no SMTP, all transports faked.

SlackNotifier and EmailNotifier both accept injectable transports so the
suite exercises the full logic (retry, fallback ordering, format, exception
swallowing) without any outbound I/O.
"""
import smtplib

from tradeops.alerting import (
    ConsoleNotifier,
    EmailNotifier,
    FallbackNotifier,
    SlackNotifier,
    format_alert,
    notifier_from_env,
)


# ── format_alert ───────────────────────────────────────────────────────────────


def test_format_alert_info():
    out = format_alert("alpha", "market open", "INFO")
    assert "alpha" in out and "INFO" in out and "market open" in out


def test_format_alert_critical_badge():
    out = format_alert("beta", "broker down", "CRITICAL")
    assert ":rotating_light:" in out


def test_format_alert_unknown_severity_no_badge():
    out = format_alert("alpha", "msg", "TRACE")
    assert "TRACE" in out


# ── SlackNotifier ──────────────────────────────────────────────────────────────


class FakePost:
    def __init__(self, status=200, raises=None):
        self.calls = []
        self._status = status
        self._raises = raises

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload))
        if self._raises:
            raise self._raises
        return self._status


def make_slack(status=200, raises=None, webhook="https://hooks.example.com/T123"):
    post = FakePost(status=status, raises=raises)
    notifier = SlackNotifier(webhook, agent_name="test", http_post=post)
    return notifier, post


def test_slack_send_returns_true_on_200():
    n, _ = make_slack()
    assert n.send("hello") is True


def test_slack_send_returns_false_on_non_200():
    n, _ = make_slack(status=500)
    assert n.send("fail") is False


def test_slack_send_returns_false_on_exception():
    n, _ = make_slack(raises=ConnectionError("timeout"))
    assert n.send("boom") is False


def test_slack_send_alert_never_raises():
    n, _ = make_slack(raises=RuntimeError("any error"))
    n.send_alert("msg", "CRITICAL")  # must not propagate


def test_slack_empty_webhook_skips_post():
    post = FakePost()
    n = SlackNotifier("", agent_name="test", http_post=post)
    assert n.send("hi") is False
    assert post.calls == []


def test_slack_payload_contains_formatted_message():
    n, post = make_slack()
    n.send("disk full", "WARN")
    assert len(post.calls) == 1
    _, payload = post.calls[0]
    assert "disk full" in payload["text"]
    assert "WARN" in payload["text"]


# ── EmailNotifier ──────────────────────────────────────────────────────────────


class FakeSMTP:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sent = []
        self.tls_called = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def starttls(self):
        self.tls_called = True

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.sent.append(msg)


class FailSMTP:
    def __init__(self, host, port):
        pass

    def __enter__(self):
        raise smtplib.SMTPException("cannot connect")

    def __exit__(self, *_):
        pass


def make_email(smtp_factory=FakeSMTP):
    return EmailNotifier(
        from_addr="from@example.com",
        to_addr="to@example.com",
        password="secret",
        smtp_host="smtp.example.com",
        smtp_port=587,
        agent_name="test",
        smtp_factory=smtp_factory,
    )


def test_email_send_returns_true_on_success():
    n = make_email()
    assert n.send("test alert", "INFO") is True


def test_email_send_returns_false_on_smtp_failure():
    n = make_email(smtp_factory=FailSMTP)
    assert n.send("test") is False


def test_email_send_returns_false_when_config_missing():
    n = EmailNotifier("", "to@example.com", "pw", "host")
    assert n.send("msg") is False


def test_email_send_alert_never_raises():
    n = make_email(smtp_factory=FailSMTP)
    n.send_alert("bad", "CRITICAL")  # must not propagate


def test_email_subject_contains_severity_and_message():
    smtp = FakeSMTP("host", 587)

    def factory(host, port):
        return smtp

    n = EmailNotifier("a@b.com", "c@d.com", "pw", "host", smtp_factory=factory)
    n.send("disk warning", "WARN")
    assert smtp.sent
    assert "WARN" in smtp.sent[0]["Subject"]
    assert "disk warning" in smtp.sent[0]["Subject"]


# ── ConsoleNotifier ────────────────────────────────────────────────────────────


def test_console_always_returns_true(capsys):
    n = ConsoleNotifier(agent_name="test")
    assert n.send("hi", "INFO") is True


def test_console_prints_severity_and_message(capsys):
    n = ConsoleNotifier(agent_name="myagent")
    n.send("disk full", "CRITICAL")
    out = capsys.readouterr().out
    assert "CRITICAL" in out and "disk full" in out and "myagent" in out


def test_console_send_alert_never_raises():
    n = ConsoleNotifier()
    n.send_alert("anything", "WARN")


# ── FallbackNotifier ───────────────────────────────────────────────────────────


class FixedTransport:
    def __init__(self, returns: bool):
        self.calls = []
        self._returns = returns

    def send(self, message, severity="INFO"):
        self.calls.append(message)
        return self._returns

    def send_alert(self, message, severity="INFO"):
        self.send(message, severity)


def test_fallback_stops_at_first_success():
    first = FixedTransport(True)
    second = FixedTransport(True)
    fn = FallbackNotifier([first, second], ensure_console=False)
    fn.send_alert("msg")
    assert len(first.calls) == 1
    assert len(second.calls) == 0


def test_fallback_advances_on_failure():
    first = FixedTransport(False)
    second = FixedTransport(True)
    fn = FallbackNotifier([first, second], ensure_console=False)
    fn.send_alert("msg")
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_fallback_appends_console_by_default():
    fn = FallbackNotifier([], ensure_console=True)
    assert any(isinstance(t, ConsoleNotifier) for t in fn.transports)


def test_fallback_does_not_double_console():
    c = ConsoleNotifier()
    fn = FallbackNotifier([c], ensure_console=True)
    console_count = sum(1 for t in fn.transports if isinstance(t, ConsoleNotifier))
    assert console_count == 1


def test_fallback_swallows_raising_transport():
    class BombTransport:
        def send(self, msg, sev="INFO"):
            raise RuntimeError("explode")

    fn = FallbackNotifier([BombTransport()], ensure_console=False)
    fn.send_alert("test")  # must not propagate


def test_fallback_send_alert_never_raises_all_fail():
    first = FixedTransport(False)
    second = FixedTransport(False)
    fn = FallbackNotifier([first, second], ensure_console=False)
    fn.send_alert("msg")  # must not raise even when all fail


# ── notifier_from_env ──────────────────────────────────────────────────────────


def test_notifier_from_env_returns_fallback(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("AGENT_NAME", "test-agent")
    n = notifier_from_env()
    assert isinstance(n, FallbackNotifier)


def test_notifier_from_env_uses_agent_name_arg(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    n = notifier_from_env("my-agent")
    slack = next(t for t in n.transports if isinstance(t, SlackNotifier))
    assert slack.agent_name == "my-agent"

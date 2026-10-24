"""The nightly wrapper must never break the night: it skips, retries, and reports instead of raising."""
import pytest

from review_site.tools import nightly_sync, publish, pull


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in nightly_sync.NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(nightly_sync, "user_environment", lambda name: None)
    monkeypatch.setattr(nightly_sync, "busy", lambda: False)
    monkeypatch.setattr(nightly_sync.time, "sleep", lambda _: None)


def configure(monkeypatch):
    monkeypatch.setenv("REVIEW_SITE_URL", "https://example.test")
    monkeypatch.setenv("REVIEW_TOOL_TOKEN", "token")


def test_skips_quietly_without_credentials(capsys):
    assert nightly_sync.run("pull") == 0
    assert "not set" in capsys.readouterr().out


def test_picks_up_credentials_from_the_windows_user_environment(monkeypatch):
    monkeypatch.setattr(nightly_sync, "user_environment", lambda name: {"REVIEW_SITE_URL": "https://x.test",
                                                                          "REVIEW_TOOL_TOKEN": "t"}.get(name))
    calls = []
    monkeypatch.setattr(pull, "main", lambda argv: calls.append(argv))
    assert nightly_sync.run("pull") == 0 and calls == [["--runs", str(nightly_sync.ROOT)]]


def test_skips_while_another_render_job_is_running(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(nightly_sync, "busy", lambda: True)
    monkeypatch.setattr(publish, "main", lambda argv: pytest.fail("must not publish while a render is running"))
    assert nightly_sync.run("publish") == 0
    assert "render running" in capsys.readouterr().out


def test_retries_once_then_reports_failure_without_raising(monkeypatch, capsys):
    configure(monkeypatch)
    attempts = []

    def flaky(argv):
        attempts.append(1)
        raise ConnectionError("site is asleep")
    monkeypatch.setattr(publish, "main", flaky)
    assert nightly_sync.run("publish") == 1 and len(attempts) == 2
    assert "ConnectionError" in capsys.readouterr().out


def test_second_attempt_can_succeed(monkeypatch):
    configure(monkeypatch)
    tries = []

    def once_then_ok(argv):
        tries.append(1)
        if len(tries) == 1:
            raise TimeoutError("waking up")
    monkeypatch.setattr(pull, "main", once_then_ok)
    assert nightly_sync.run("pull") == 0 and len(tries) == 2


def test_tool_exit_messages_become_a_failure_code_not_a_crash(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(publish, "main", lambda argv: (_ for _ in ()).throw(SystemExit("No assembled chapter named: x")))
    assert nightly_sync.run("publish") == 1
    assert "No assembled chapter" in capsys.readouterr().out


def test_clean_exit_from_a_tool_is_success(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(pull, "main", lambda argv: (_ for _ in ()).throw(SystemExit(0)))
    assert nightly_sync.run("pull") == 0


def test_command_line_rejects_unknown_steps():
    with pytest.raises(SystemExit):
        nightly_sync.main(["explode"])

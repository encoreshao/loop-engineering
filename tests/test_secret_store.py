import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import mail_auth
import secret_store


class FakeSecurity:
    def __init__(self):
        self.calls = []
    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        class R: returncode = 0; stdout = "s3cret\n"; stderr = ""
        return R()


def test_sandboxed_service_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))
    assert mail_auth.sandboxed_service("x.y").startswith("x.y.sandbox-")


def test_sandboxed_service_plain_without_home(monkeypatch):
    monkeypatch.delenv("LOOP_ENGINEERING_HOME", raising=False)
    assert mail_auth.sandboxed_service("x.y") == "x.y"


def test_mail_keychain_service_unchanged(monkeypatch):
    monkeypatch.delenv("LOOP_ENGINEERING_HOME", raising=False)
    assert mail_auth.keychain_service() == "loop-engineering.mail"


def test_secret_store_uses_connector_service(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))
    fake = FakeSecurity()
    monkeypatch.setattr(mail_auth, "_security", fake)
    assert secret_store.get("github-personal") == "s3cret"
    args = fake.calls[0][0]
    service = args[args.index("-s") + 1]
    assert service.startswith("loop-engineering.connectors.sandbox-")
    assert args[args.index("-a") + 1] == "github-personal"


def test_secret_store_put_uses_stdin_not_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOP_ENGINEERING_HOME", str(tmp_path))
    fake = FakeSecurity()
    monkeypatch.setattr(mail_auth, "_security", fake)
    secret_store.put("gh", "tok-123")
    args, stdin = fake.calls[0]
    assert "tok-123" not in " ".join(args)
    assert "tok-123" in stdin

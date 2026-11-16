"""Testes unitários do provedor 'claude_cli' (Claude Code CLI em modo headless)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from stays_automation.cli import build_arg_parser
from stays_automation.llm_client import ClaudeCliClient, SYSTEM_PROMPT, create_llm_client
from stays_automation.models import CustomFieldsCatalog, FieldStatus, SourceExtractionArtifact
from stays_automation.settings import AppSettings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FIELDS_PATH = PROJECT_ROOT / "config" / "custom_fields.yaml"
TARGET_URL = "https://ssmt.stays.com.br/i/apartment/AB25I/setup"


@pytest.fixture
def catalog() -> CustomFieldsCatalog:
    return CustomFieldsCatalog.from_yaml(CONFIG_FIELDS_PATH)


@pytest.fixture
def sample_artifact() -> SourceExtractionArtifact:
    text = "Endereço: Rua das Flores, 100, Urubici - SC\nCheck-out: até 11h00.\n"
    return SourceExtractionArtifact(
        source_type="file",
        source_reference="exemplo.txt",
        raw_input_text=text,
        extracted_text=text,
    )


def _model_answer() -> str:
    return json.dumps(
        {
            "property": {
                "target_url": TARGET_URL,
                "property_id": "AB25I",
                "source_type": "file",
                "source_reference": "exemplo.txt",
            },
            "fields": [
                {
                    "field_key": "property_address",
                    "stays_label": "Endereço da Propriedade",
                    "value": "Rua das Flores, 100, Urubici - SC",
                    "status": "found",
                    "confidence": 0.98,
                    "sensitive": False,
                    "evidence": [{"source": "source_file", "excerpt": "Rua das Flores, 100"}],
                    "warnings": [],
                }
            ],
        },
        ensure_ascii=False,
    )


class FakeRunner:
    """Simula subprocess.run registrando as chamadas."""

    def __init__(self, outcomes: List[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"cmd": cmd, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _completed(envelope: Dict[str, Any] | None, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    stdout = json.dumps(envelope) if envelope is not None else ""
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _success_envelope(result: str) -> Dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": result}


def _classify(client: ClaudeCliClient, catalog: CustomFieldsCatalog, artifact: SourceExtractionArtifact):
    return client.classify_and_structure(
        target_url=TARGET_URL,
        property_id="AB25I",
        source_artifact=artifact,
        catalog=catalog,
    )


def test_claude_cli_success_parses_result_and_builds_safe_command(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    runner = FakeRunner([_completed(_success_envelope(_model_answer()))])
    client = ClaudeCliClient(model="sonnet", runner=runner)

    sanitized_req, raw_resp, validated = _classify(client, catalog, sample_artifact)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    cmd = call["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--system-prompt") + 1] == SYSTEM_PROMPT
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "Rua das Flores" in call["input"]
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["timeout"] == client.timeout_seconds

    assert sanitized_req["provider"] == "claude_cli"
    assert raw_resp["fields"][0]["field_key"] == "property_address"
    assert len(validated.fields) == 37
    addr = next(f for f in validated.fields if f.field_key == "property_address")
    assert addr.status == FieldStatus.FOUND
    assert addr.value == "Rua das Flores, 100, Urubici - SC"


def test_claude_cli_accepts_json_wrapped_in_markdown(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    fenced = f"```json\n{_model_answer()}\n```"
    runner = FakeRunner([_completed(_success_envelope(fenced))])
    _, _, validated = _classify(ClaudeCliClient(runner=runner), catalog, sample_artifact)
    assert validated.summary.found == 1


def test_claude_cli_retries_on_error_envelope_then_succeeds(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stays_automation.llm_client.time.sleep", lambda _s: None)
    error_envelope = {"type": "result", "subtype": "error", "is_error": True, "result": "API Error: overloaded"}
    runner = FakeRunner(
        [_completed(error_envelope, returncode=1), _completed(_success_envelope(_model_answer()))]
    )
    _, _, validated = _classify(ClaudeCliClient(runner=runner, max_retries=1), catalog, sample_artifact)
    assert len(runner.calls) == 2
    assert validated.summary.found == 1


def test_claude_cli_gives_up_after_retries_with_clear_message(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stays_automation.llm_client.time.sleep", lambda _s: None)
    runner = FakeRunner(
        [_completed(None, returncode=1, stderr="Invalid API key · Please run /login")] * 2
    )
    with pytest.raises(RuntimeError, match="Please run /login"):
        _classify(ClaudeCliClient(runner=runner, max_retries=1), catalog, sample_artifact)
    assert len(runner.calls) == 2


def test_claude_cli_missing_executable_is_not_retried(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    runner = FakeRunner([FileNotFoundError("claude")])
    with pytest.raises(RuntimeError, match="não encontrado"):
        _classify(ClaudeCliClient(runner=runner, max_retries=2), catalog, sample_artifact)
    assert len(runner.calls) == 1


def test_claude_cli_timeout_is_not_retried(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    runner = FakeRunner([subprocess.TimeoutExpired(cmd="claude", timeout=5)])
    with pytest.raises(RuntimeError, match="Tempo limite"):
        _classify(ClaudeCliClient(runner=runner, timeout_seconds=5, max_retries=2), catalog, sample_artifact)
    assert len(runner.calls) == 1


def test_claude_cli_invalid_model_json_raises_value_error(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    runner = FakeRunner([_completed(_success_envelope("não é JSON"))])
    with pytest.raises(ValueError, match="Resposta inválida do Claude \\(CLI\\)"):
        _classify(ClaudeCliClient(runner=runner), catalog, sample_artifact)
    assert len(runner.calls) == 1


def test_factory_and_settings_for_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stays_automation.settings.shutil.which", lambda _p: "/usr/bin/claude")
    settings = AppSettings(
        llm_provider="claude_cli",
        claude_cli_model="opus",
        claude_cli_timeout_seconds=120,
        anthropic_api_key="",
    )
    client = create_llm_client(settings)
    assert isinstance(client, ClaudeCliClient)
    assert client.model == "opus"
    assert client.timeout_seconds == 120

    monkeypatch.setattr("stays_automation.settings.shutil.which", lambda _p: None)
    with pytest.raises(ValueError, match="Claude Code não encontrado"):
        settings.validate_for_llm()


def test_cli_parser_and_env_for_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "haiku")
    monkeypatch.setenv("CLAUDE_CLI_TIMEOUT_SECONDS", "90")
    args = build_arg_parser().parse_args(["--llm-provider", "claude_cli"])
    assert args.llm_provider == "claude_cli"

    settings = load_settings(llm_provider_override=args.llm_provider)
    assert settings.llm_provider == "claude_cli"
    assert settings.claude_cli_model == "haiku"
    assert settings.claude_cli_timeout_seconds == 90.0

    overridden = load_settings(llm_provider_override="claude_cli", model_override="opus")
    assert overridden.claude_cli_model == "opus"

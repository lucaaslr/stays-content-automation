"""Testes unitários para integração com LLM: Ollama (local), Claude, normalização e factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

from stays_automation.artifacts import ArtifactManager
from stays_automation.cli import build_arg_parser, execute_automation_flow
from stays_automation.llm_client import (
    BaseLLMClient,
    ClaudeClient,
    OllamaClient,
    create_llm_client,
    extract_json_object_from_text,
    normalize_extraction_response,
)
from stays_automation.models import (
    CustomFieldsCatalog,
    FieldStatus,
    SourceExtractionArtifact,
)
from stays_automation.security import SecretScrubber
from stays_automation.settings import AppSettings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FIELDS_PATH = PROJECT_ROOT / "config" / "custom_fields.yaml"


@pytest.fixture
def catalog() -> CustomFieldsCatalog:
    return CustomFieldsCatalog.from_yaml(CONFIG_FIELDS_PATH)


@pytest.fixture
def sample_artifact() -> SourceExtractionArtifact:
    text = (
        "Endereço: Rua das Flores, 100, Urubici - SC\n"
        "Check-in: das 15h00 às 20h00. Check-out: até 11h00.\n"
        "Wi-Fi: Cabana_5G / Senha: segredo_wifi_123\n"
        "Código do cofre: 1234\n"
    )
    return SourceExtractionArtifact(
        source_type="file",
        source_reference="exemplo.txt",
        raw_input_text=text,
        extracted_text=text,
    )


def test_ollama_settings_validation_without_anthropic_api_key() -> None:
    """Garante que com provedor Ollama nenhuma chave da Anthropic é exigida."""
    settings = AppSettings(
        llm_provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_model="llama3:latest",
        anthropic_api_key="",  # Sem chave de API!
    )
    # Não deve levantar erro:
    settings.validate_for_llm()

    # Mas se o provedor for anthropic sem chave, deve falhar:
    settings.llm_provider = "anthropic"
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        settings.validate_for_llm()


def test_create_llm_client_factory() -> None:
    """Verifica a criação do cliente correto baseado no provedor configurado."""
    settings_ollama = AppSettings(
        llm_provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_model="llama3:latest",
    )
    client = create_llm_client(settings_ollama)
    assert isinstance(client, OllamaClient)
    assert client.model == "llama3:latest"
    assert client.base_url == "http://localhost:11434"

    settings_claude = AppSettings(
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test-key-123456",
        claude_model="claude-3-5-haiku-latest",
    )
    claude_client = create_llm_client(settings_claude)
    assert isinstance(claude_client, ClaudeClient)
    assert claude_client.model == "claude-3-5-haiku-latest"

    # Provedor inválido
    with pytest.raises(ValueError, match="Provedor LLM inválido"):
        AppSettings(llm_provider="desconhecido")


def test_cli_parser_llm_options() -> None:
    """Valida as flags de linha de comando para configuração do Ollama e LLM."""
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--llm-provider",
            "ollama",
            "--ollama-url",
            "http://localhost:11434",
            "--ollama-model",
            "llama3.2:3b",
            "--ollama-timeout",
            "120",
        ]
    )
    assert args.llm_provider == "ollama"
    assert args.ollama_url == "http://localhost:11434"
    assert args.ollama_model == "llama3.2:3b"
    assert args.ollama_timeout == 120.0

    settings = load_settings(
        llm_provider_override=args.llm_provider,
        ollama_url_override=args.ollama_url,
        ollama_model_override=args.ollama_model,
        ollama_timeout_override=args.ollama_timeout,
    )
    assert settings.llm_provider == "ollama"
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "llama3.2:3b"
    assert settings.ollama_timeout_seconds == 120.0


def test_normalize_extraction_response_auto_fills_omitted_fields(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    """
    Testa normalização resiliente: modelos locais que omitem campos ausentes ou o sumário
    são complementados para garantir exatamente 37 campos válidos e sumário correto.
    """
    partial_ollama_json: Dict[str, Any] = {
        "property": {
            "target_url": "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
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
            },
            {
                "field_key": "wifi_and_electronics",
                "stays_label": "Wi-fi e dispositivos eletrônicos",
                "value": "Cabana_5G",
                "status": "found",
                "confidence": 0.95,
            },
            # Tentativa indevida de preencher o campo '.'
            {
                "field_key": "dot_placeholder",
                "stays_label": ".",
                "value": "Dado indevido",
                "status": "found",
                "confidence": 0.99,
            },
        ],
        # 'summary' propositalmente ausente para testar auto-cálculo
    }

    normalized = normalize_extraction_response(
        partial_ollama_json,
        catalog=catalog,
        target_url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
        property_id="AB25I",
        source_artifact=sample_artifact,
    )

    # 1. Total de campos deve ser exatamente 37 (catálogo completo)
    assert len(normalized["fields"]) == 37
    assert normalized["summary"]["total_fields"] == 37
    assert normalized["summary"]["found"] == 2
    assert normalized["summary"]["missing"] == 35

    by_key = {f["field_key"]: f for f in normalized["fields"]}

    # Campo encontrado
    assert by_key["property_address"]["value"] == "Rua das Flores, 100, Urubici - SC"
    assert by_key["property_address"]["status"] == "found"

    # Campo ausente foi preenchido com status missing
    assert by_key["pet_policies"]["value"] is None
    assert by_key["pet_policies"]["status"] == "missing"
    assert by_key["pet_policies"]["confidence"] == 0.0

    # Campo '.' foi rigorosamente bloqueado
    assert by_key["dot_placeholder"]["value"] is None
    assert by_key["dot_placeholder"]["status"] == "missing"
    assert by_key["dot_placeholder"]["confidence"] == 0.0


def test_ollama_client_classify_and_structure_with_mock_http(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    """Testa execução de ponta a ponta do OllamaClient com mock da API HTTP local."""

    mock_ollama_response = {
        "model": "llama3:latest",
        "message": {
            "role": "assistant",
            "content": json.dumps(
                {
                    "property": {
                        "target_url": "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
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
                }
            ),
        },
        "done": True,
    }

    def custom_transport(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/api/chat"
        req_data = json.loads(request.content.decode("utf-8"))
        assert req_data["model"] == "llama3:latest"
        assert req_data["format"] == "json"
        return httpx.Response(200, json=mock_ollama_response, request=request)

    mock_http_client = httpx.Client(transport=httpx.MockTransport(custom_transport))

    ollama_client = OllamaClient(
        base_url="http://localhost:11434",
        model="llama3:latest",
        http_client=mock_http_client,
    )

    sanitized_req, raw_resp, validated = ollama_client.classify_and_structure(
        target_url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
        property_id="AB25I",
        source_artifact=sample_artifact,
        catalog=catalog,
    )

    assert sanitized_req["model"] == "llama3:latest"
    assert len(validated.fields) == 37
    assert validated.summary.found == 1
    assert validated.summary.missing == 36
    addr_field = next(f for f in validated.fields if f.field_key == "property_address")
    assert addr_field.value == "Rua das Flores, 100, Urubici - SC"
    assert addr_field.status == FieldStatus.FOUND


def test_ollama_client_connection_error_raises_clear_message(
    catalog: CustomFieldsCatalog,
    sample_artifact: SourceExtractionArtifact,
) -> None:
    """Garante mensagem clara quando o serviço Ollama não está ativo."""
    # Apontando para uma porta inexistente
    client = OllamaClient(
        base_url="http://localhost:59999",
        model="llama3:latest",
        max_retries=0,
        timeout_seconds=2.0,
    )
    with pytest.raises(RuntimeError, match="Não foi possível conectar ao servidor Ollama"):
        client.classify_and_structure(
            target_url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            property_id="AB25I",
            source_artifact=sample_artifact,
            catalog=catalog,
        )


def test_end_to_end_dry_run_with_mocked_ollama_and_stays_never_saves(tmp_path: Path) -> None:
    """
    Executa o fluxo completo da CLI com Ollama mockado:
    - valida que não exige ANTHROPIC_API_KEY;
    - verifica que dry-run preenche os campos aprovados mas NUNCA salva no Stays;
    - verifica geração correta de artefatos.
    """
    from rich.console import Console
    from test_content_mapper import EXAMPLE_INPUT_PATH, FIXTURE_HTML_PATH, FakeStaysPage

    mock_ollama_response = {
        "model": "llama3:latest",
        "message": {
            "role": "assistant",
            "content": json.dumps(
                {
                    "property": {
                        "target_url": "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
                        "property_id": "AB25I",
                        "source_type": "file",
                        "source_reference": str(EXAMPLE_INPUT_PATH),
                    },
                    "fields": [
                        {
                            "field_key": "property_address",
                            "stays_label": "Endereço da Propriedade",
                            "value": "Estrada Geral do Rio Bonito, nº 1450, Bairro Rio Bonito, Urubici - SC",
                            "status": "found",
                            "confidence": 0.98,
                            "sensitive": False,
                            "evidence": [{"source": "source_file", "excerpt": "Estrada Geral do Rio Bonito, nº 1450"}],
                            "warnings": [],
                        }
                    ],
                }
            ),
        },
        "done": True,
    }

    def custom_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_ollama_response, request=request)

    mock_http = httpx.Client(transport=httpx.MockTransport(custom_transport))
    ollama_client = OllamaClient(
        base_url="http://localhost:11434",
        model="llama3:latest",
        http_client=mock_http,
    )

    raw_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    fake_page = FakeStaysPage(
        html_content=raw_html,
        url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
    )

    answers = iter(["s", "s"])
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--target-url",
            "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            "--source-file",
            str(EXAMPLE_INPUT_PATH),
            "--output-dir",
            str(tmp_path),
            "--llm-provider",
            "ollama",
            "--dry-run",
        ]
    )

    exit_code = execute_automation_flow(
        args,
        console=Console(record=True),
        input_fn=lambda prompt: next(answers, "s"),
        llm_client_override=ollama_client,
        page_override=fake_page,
    )

    assert exit_code == 0
    # Garante que NUNCA clicou em Salvar em modo dry-run
    assert fake_page.save_clicked is False


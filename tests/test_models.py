"""Testes de modelos Pydantic, divergência de 36 vs 37 campos e configurações."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from stays_automation.claude_client import extract_json_object_from_text
from stays_automation.models import (
    ClaudeExtractionResponse,
    CustomFieldsCatalog,
    FieldStatus,
    validate_http_url,
)
from stays_automation.settings import extract_property_id_from_url, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "custom_fields.yaml"
FIXTURE_JSON_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_claude_response.json"


def test_detect_divergence_between_36_and_37_fields() -> None:
    """Garante que o catálogo carrega todas as 37 entradas e documenta a divergência com 36."""
    catalog = CustomFieldsCatalog.from_yaml(CONFIG_PATH)
    assert catalog.expected_count == 36
    assert catalog.actual_count == 37
    assert catalog.has_count_divergence is True
    assert catalog.divergence_message is not None
    assert "36" in catalog.divergence_message
    assert "37" in catalog.divergence_message

    dot_field = catalog.get_by_key("dot_placeholder")
    assert dot_field is not None
    assert dot_field.stays_label == "."
    assert dot_field.allow_auto_fill is False


def test_validate_valid_claude_json_response() -> None:
    """Valida a estrutura JSON completa retornada pelo Claude usando Pydantic."""
    raw_data = json.loads(FIXTURE_JSON_PATH.read_text(encoding="utf-8"))
    response = ClaudeExtractionResponse.model_validate(raw_data)

    assert response.property.property_id == "AB25I"
    assert response.summary.total_fields == 37
    assert len(response.fields) == 5
    assert response.fields[0].status == FieldStatus.FOUND
    assert response.fields[0].confidence == 0.99


def test_invalid_claude_response_raises_validation_error() -> None:
    """Garante tratamento rigoroso para JSON malformado, status inválido ou confiança fora de [0, 1]."""
    with pytest.raises(ValueError, match="não é um JSON válido"):
        extract_json_object_from_text("Texto sem JSON válido {incompleto")

    invalid_payload = {
        "property": {
            "target_url": "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            "property_id": "AB25I",
            "source_type": "url",
            "source_reference": "https://www.multitemporada.com/pt/apartment/SR08H",
        },
        "fields": [
            {
                "field_key": "property_address",
                "stays_label": "Endereço da Propriedade",
                "value": "Rua X",
                "status": "status_inexistente",
                "confidence": 1.5,
                "sensitive": False,
                "evidence": [],
                "warnings": [],
            }
        ],
        "summary": {
            "total_fields": 37,
            "found": 1,
            "missing": 36,
            "conflicting": 0,
            "requires_review": 0,
        },
    }
    with pytest.raises(ValidationError):
        ClaudeExtractionResponse.model_validate(invalid_payload)


def test_reject_save_and_dry_run_simultaneously() -> None:
    """Rejeita a execução caso --save e --dry-run sejam informados simultaneamente."""
    with pytest.raises(ValueError, match="mutuamente exclusivas"):
        load_settings(dry_run_flag=True, save_flag=True)


def test_default_is_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garante que por padrão DRY_RUN é True e save_changes é False."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    settings = load_settings()
    assert settings.dry_run is True
    assert settings.save_changes is False


def test_invalid_urls_are_rejected() -> None:
    """Testa rejeição de URLs malformadas ou com esquemas inseguros."""
    for bad_url in ["", "ftp://example.com", "http://sem-dominio", "https://url com espaco.com", "apenas_texto"]:
        with pytest.raises(ValueError):
            validate_http_url(bad_url)

    assert (
        validate_http_url("https://ssmt.stays.com.br/i/apartment/AB25I/setup")
        == "https://ssmt.stays.com.br/i/apartment/AB25I/setup"
    )
    assert extract_property_id_from_url("https://ssmt.stays.com.br/i/apartment/GJ12G/setup") == "GJ12G"

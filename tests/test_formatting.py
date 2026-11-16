"""Testes do guia de estilo (SR08H) e da conversão para o HTML do Summernote."""

from __future__ import annotations

from pathlib import Path

from stays_automation.formatting import load_style_guide, to_plain_text, to_stays_html
from stays_automation.llm_client import ClaudeCliClient, OllamaClient
from stays_automation.models import CustomFieldsCatalog, SourceExtractionArtifact

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_to_stays_html_matches_reference_structure() -> None:
    text = (
        "**Disponibilizamos aos hóspedes:**\n"
        "Roupa de cama;\n"
        "Toalhas de banho.\n"
        "\n"
        "**Condições de troca:**\n"
        "-Troca a cada 2 dias.\n"
    )
    assert to_stays_html(text) == (
        "<p><b>Disponibilizamos aos hóspedes:</b></p>"
        "<p>Roupa de cama;</p>"
        "<p>Toalhas de banho.</p>"
        "<p><br></p>"
        "<p><b>Condições de troca:</b></p>"
        "<p>- Troca a cada 2 dias.</p>"
    )


def test_to_stays_html_inline_bold_escapes_html_and_collapses_blank_lines() -> None:
    text = "**⚠️ Atenção**: acesso por escadas <sem elevador> & rampa.\n\n\n\nFim.\n\n"
    assert to_stays_html(text) == (
        "<p><b>⚠️ Atenção</b>: acesso por escadas &lt;sem elevador&gt; &amp; rampa.</p>"
        "<p><br></p>"
        "<p>Fim.</p>"
    )


def test_single_line_value_stays_single_paragraph() -> None:
    assert to_stays_html("https://maps.app.goo.gl/abc") == "<p>https://maps.app.goo.gl/abc</p>"


def test_to_plain_text_ignores_markup_and_blank_lines() -> None:
    assert to_plain_text("**Título:**\n\n- Item.") == "Título:\n- Item."
    # Texto lido do editor (innerText) tem linhas vazias extras entre parágrafos
    assert to_plain_text("Título:\n\n\n- Item.") == to_plain_text("**Título:**\n- Item.")


def test_style_guide_covers_every_catalog_field() -> None:
    guide = load_style_guide()
    catalog = CustomFieldsCatalog.from_yaml(PROJECT_ROOT / "config" / "custom_fields.yaml")
    catalog_keys = {f.field_key for f in catalog.fields}
    assert set(guide.field_formats) == catalog_keys
    assert guide.reference_property == "SR08H"


def test_prompts_include_style_rules_and_per_field_style() -> None:
    catalog = CustomFieldsCatalog.from_yaml(PROJECT_ROOT / "config" / "custom_fields.yaml")
    artifact = SourceExtractionArtifact(
        source_type="file",
        source_reference="exemplo.txt",
        raw_input_text="Check-in às 15h.",
        extracted_text="Check-in às 15h.",
    )
    kwargs = dict(
        target_url="https://ssmt.stays.com.br/i/apartment/MN01I/setup",
        property_id="MN01I",
        source_artifact=artifact,
        catalog=catalog,
    )
    for client in (ClaudeCliClient(runner=lambda *a, **k: None), OllamaClient()):
        prompt = client.build_user_prompt(**kwargs)
        assert "PADRÃO DE ESTILO OBRIGATÓRIO" in prompt
        assert "nunca dados do imóvel de referência" in prompt
        assert "Seções **Check-in** e **Check-out**" in prompt

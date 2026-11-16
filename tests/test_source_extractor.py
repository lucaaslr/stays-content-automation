"""Testes de extração de fontes (URL pública, fallback Playwright e texto bruto)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stays_automation.source_extractor import (
    SourceExtractor,
    extract_clean_text_from_html,
    needs_javascript_rendering,
)


def test_extract_clean_text_ignores_nav_footer_and_preserves_content() -> None:
    """Garante que menus, rodapés, scripts e banners são ignorados e listas/títulos/links preservados."""
    sample_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Cabana SR08H - Multitemporada</title>
        <script>console.log("tracking banner");</script>
        <style>.hidden { display: none; }</style>
      </head>
      <body>
        <nav class="main-menu">Menu Principal | Home | Contato</nav>
        <div id="cookie-banner">Aceite todos os cookies</div>
        <main>
          <h1>Cabana Vista da Serra</h1>
          <p>Endereço: Estrada Geral do Rio Bonito, 1450, Urubici - SC.</p>
          <h2>Comodidades</h2>
          <ul>
            <li>Jacuzzi aquecida</li>
            <li>Wi-Fi 300 Mbps</li>
          </ul>
          <p>Localização: <a href="https://maps.google.com/?q=-28.01,-49.59">Ver no Mapa</a></p>
        </main>
        <footer>Rodapé institucional Copyright 2026</footer>
      </body>
    </html>
    """
    extracted_text, title = extract_clean_text_from_html(
        sample_html,
        source_url="https://www.multitemporada.com/pt/apartment/SR08H",
    )

    assert title == "Cabana SR08H - Multitemporada"
    assert "Menu Principal" not in extracted_text
    assert "Aceite todos os cookies" not in extracted_text
    assert "Rodapé institucional" not in extracted_text
    assert "tracking banner" not in extracted_text
    assert "# Cabana Vista da Serra" in extracted_text
    assert "- Jacuzzi aquecida" in extracted_text
    assert "https://maps.google.com/?q=-28.01,-49.59" in extracted_text


def test_source_extractor_falls_back_to_playwright_when_spa_shell() -> None:
    """Verifica que páginas que dependem de JavaScript acionam o fetcher do Playwright."""
    spa_shell = "<html><body><div id='root'></div><noscript>You need to enable JavaScript</noscript></body></html>"
    extracted_empty, _ = extract_clean_text_from_html(spa_shell)
    assert needs_javascript_rendering(spa_shell, extracted_empty) is True

    rendered_html = """
    <html>
      <head><title>Imóvel Renderizado</title></head>
      <body>
        <main>
          <h1>Imóvel SR08H</h1>
          <p>Check-in a partir das 15h00 e check-out até as 11h00. Proibido fumar na unidade.</p>
          <p>Estacionamento privativo para 2 carros em frente ao chalé.</p>
        </main>
      </body>
    </html>
    """
    extractor = SourceExtractor(
        playwright_fetcher=lambda url, headless: rendered_html,
    )
    artifact = extractor.extract_from_url(
        "https://www.multitemporada.com/pt/apartment/SR08H",
        force_playwright=True,
    )

    assert artifact.used_playwright is True
    assert artifact.source_type == "url"
    assert "https://www.multitemporada.com/pt/apartment/SR08H" in artifact.extracted_text
    assert "Check-in a partir das 15h00" in artifact.extracted_text


def test_source_extractor_preserves_raw_file_text_untouched(tmp_path: Path) -> None:
    """Garante que o texto bruto do arquivo é preservado sem modificações em raw_input_text."""
    raw_content = "  Linha 1 com espaços no início\n\nLinha 2: Check-in 15h  \n"
    input_file = tmp_path / "entrada.txt"
    input_file.write_text(raw_content, encoding="utf-8")

    extractor = SourceExtractor()
    artifact = extractor.extract(source_file=input_file)

    assert artifact.source_type == "file"
    assert artifact.raw_input_text == raw_content


def test_validate_single_source_rejects_multiple_or_empty_sources(tmp_path: Path) -> None:
    """Garante que a ferramenta aceita exatamente uma fonte de entrada por execução."""
    dummy_file = tmp_path / "dummy.txt"
    dummy_file.write_text("conteudo", encoding="utf-8")

    with pytest.raises(ValueError, match="Nenhuma fonte"):
        SourceExtractor.validate_single_source()

    with pytest.raises(ValueError, match="Apenas uma fonte"):
        SourceExtractor.validate_single_source(
            source_url="https://www.multitemporada.com/pt/apartment/SR08H",
            source_file=dummy_file,
        )

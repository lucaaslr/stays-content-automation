"""Extração de conteúdo a partir de URLs públicas (HTTP/Playwright) ou texto bruto."""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin

import httpx

from stays_automation.models import SourceExtractionArtifact, validate_http_url

logger = logging.getLogger("stays_automation")

IGNORED_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "nav",
    "footer",
    "aside",
}

NOISE_CLASS_OR_ID_TOKENS = (
    "cookie",
    "gdpr",
    "lgpd",
    "banner-promo",
    "newsletter",
    "navbar",
    "main-menu",
    "site-footer",
    "site-header",
    "breadcrumb",
    "social-share",
    "popup",
)

IMPORTANT_LINK_DOMAINS_OR_TOKENS = (
    "maps.google",
    "goo.gl/maps",
    "maps.app.goo.gl",
    "wa.me/",
    "whatsapp.com",
    "forms.",
    "ficha",
    "cadastro",
    "g.page/",
    "review",
)


class _StructuredHTMLTextParser(HTMLParser):
    """Parser HTML estruturado que ignora menus/rodapés e preserva títulos, listas, links e regras."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title: Optional[str] = None
        self._in_title = False
        self._ignored_depth = 0
        self._ignore_stack: List[bool] = []
        self._current_heading_level: Optional[int] = None
        self._current_link_href: Optional[str] = None
        self._lines: List[str] = []
        self._buffer: List[str] = []

    def _is_noisy_element(self, tag: str, attrs: dict) -> bool:
        if tag in IGNORED_TAGS:
            return True
        role = (attrs.get("role") or "").lower()
        if role in ("navigation", "contentinfo", "complementary"):
            return True
        combined_ident = f"{attrs.get('id', '')} {attrs.get('class', '')}".lower()
        for token in NOISE_CLASS_OR_ID_TOKENS:
            if token in combined_ident:
                return True
        return False

    def _flush_buffer(self, prefix: str = "", suffix: str = "") -> None:
        text = " ".join("".join(self._buffer).split()).strip()
        self._buffer.clear()
        if text:
            self._lines.append(f"{prefix}{text}{suffix}")

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs_list}

        is_ignored = self._ignored_depth > 0 or self._is_noisy_element(tag_lower, attrs)
        self._ignore_stack.append(is_ignored)
        if is_ignored:
            self._ignored_depth += 1
            return

        if tag_lower == "title":
            self._in_title = True
            self._buffer.clear()
        elif tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_buffer()
            self._current_heading_level = int(tag_lower[1])
        elif tag_lower in ("p", "div", "section", "article", "address", "tr", "dt", "dd"):
            self._flush_buffer()
        elif tag_lower == "li":
            self._flush_buffer()
            self._buffer.append("- ")
        elif tag_lower == "br":
            self._flush_buffer()
        elif tag_lower == "a":
            href = attrs.get("href", "").strip()
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                if self.base_url:
                    href = urljoin(self.base_url, href)
                self._current_link_href = href

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        was_ignored = self._ignore_stack.pop() if self._ignore_stack else False
        if was_ignored:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return

        if tag_lower == "title":
            self._in_title = False
            title_text = " ".join("".join(self._buffer).split()).strip()
            self._buffer.clear()
            if title_text and not self.title:
                self.title = title_text
        elif tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = self._current_heading_level or 2
            hashes = "#" * level
            self._flush_buffer(prefix=f"\n{hashes} ")
            self._current_heading_level = None
        elif tag_lower == "a":
            if self._current_link_href:
                href_lower = self._current_link_href.lower()
                if any(token in href_lower for token in IMPORTANT_LINK_DOMAINS_OR_TOKENS):
                    current_buf = "".join(self._buffer)
                    if self._current_link_href not in current_buf:
                        self._buffer.append(f" ({self._current_link_href}) ")
                self._current_link_href = None
        elif tag_lower in ("p", "div", "section", "article", "address", "li", "tr", "dd"):
            self._flush_buffer()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        cleaned = html.unescape(data)
        if cleaned.strip():
            self._buffer.append(cleaned + " ")

    def get_extracted_text(self) -> str:
        self._flush_buffer()
        deduped_lines: List[str] = []
        seen_recent: List[str] = []
        for raw_line in self._lines:
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            # Evita linhas idênticas consecutivas ou repetitivas
            if deduped_lines and deduped_lines[-1] == line:
                continue
            if len(line) > 30 and line in seen_recent[-8:]:
                continue
            deduped_lines.append(line)
            seen_recent.append(line)
        return "\n".join(deduped_lines).strip()


def extract_clean_text_from_html(
    raw_html: str,
    source_url: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    Extrai o texto estruturado de um documento HTML ignorando menus, rodapés,
    banners e scripts, e preservando títulos, listas, regras, endereço e links úteis.
    Retorna (texto_extraido, titulo_da_pagina).
    """
    if not raw_html or not raw_html.strip():
        return "", None

    parser = _StructuredHTMLTextParser(base_url=source_url)
    parser.feed(raw_html)
    parser.close()
    extracted = parser.get_extracted_text()
    title = parser.title

    if title and not extracted.startswith(f"# {title}"):
        extracted = f"# {title}\n\n{extracted}".strip()

    return extracted, title


def needs_javascript_rendering(raw_html: str, extracted_text: str) -> bool:
    """Detecta se a página retornou apenas um shell SPA vazio que depende de JavaScript."""
    if len(extracted_text.strip()) < 100:
        return True
    lowered_html = raw_html.lower()
    spa_markers = (
        "you need to enable javascript",
        "enable javascript to run this app",
        '<div id="root"></div>',
        '<div id="app"></div>',
    )
    if any(marker in lowered_html for marker in spa_markers) and len(extracted_text.strip()) < 250:
        return True
    return False


def fetch_html_with_playwright(
    url: str,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> str:
    """Renderiza uma página pública com Playwright para obter o DOM após execução de JavaScript."""
    from playwright.sync_api import sync_playwright

    logger.info("Renderizando URL pública com Playwright: %s", url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                locale="pt-BR",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                logger.debug("Timeout aguardando networkidle em %s; prosseguindo com DOM carregado.", url)
            return page.content()
        finally:
            browser.close()


class SourceNotFoundError(RuntimeError):
    """A URL de origem respondeu 404/410: a página do imóvel não existe."""


class SourceExtractor:
    """Responsável por validar e extrair conteúdo de URL pública, arquivo ou entrada interativa."""

    def __init__(
        self,
        *,
        http_timeout: float = 20.0,
        max_retries: int = 2,
        headless: bool = True,
        playwright_fetcher: Optional[Callable[[str, bool], str]] = None,
    ) -> None:
        self.http_timeout = http_timeout
        self.max_retries = max_retries
        self.headless = headless
        self._playwright_fetcher = playwright_fetcher or (
            lambda url, is_headless: fetch_html_with_playwright(url, headless=is_headless)
        )

    @staticmethod
    def validate_single_source(
        source_url: Optional[str] = None,
        source_file: Optional[Path] = None,
        raw_text: Optional[str] = None,
    ) -> str:
        """
        Garante que exatamente uma fonte de entrada foi fornecida na execução.
        Retorna 'url', 'file' ou 'interactive'.
        """
        provided = []
        if source_url and source_url.strip():
            provided.append("url")
        if source_file is not None:
            provided.append("file")
        if raw_text is not None and raw_text.strip():
            provided.append("interactive")

        if len(provided) == 0:
            raise ValueError(
                "Nenhuma fonte de entrada fornecida. Informe --source-url, --source-file ou texto interativo."
            )
        if len(provided) > 1:
            raise ValueError(
                f"Apenas uma fonte de entrada é permitida por execução, mas foram informadas: {', '.join(provided)}."
            )
        return provided[0]

    def extract(
        self,
        *,
        source_url: Optional[str] = None,
        source_file: Optional[Path] = None,
        raw_text: Optional[str] = None,
        force_playwright: bool = False,
    ) -> SourceExtractionArtifact:
        """Executa a extração da fonte selecionada e retorna o artefato estruturado."""
        source_kind = self.validate_single_source(
            source_url=source_url,
            source_file=source_file,
            raw_text=raw_text,
        )

        if source_kind == "url":
            assert source_url is not None
            return self.extract_from_url(source_url, force_playwright=force_playwright)
        if source_kind == "file":
            assert source_file is not None
            return self.extract_from_file(source_file)
        assert raw_text is not None
        return self.extract_from_raw_text(raw_text)

    def extract_from_file(self, file_path: Path) -> SourceExtractionArtifact:
        """Lê o texto bruto de um arquivo sem modificá-lo no artefato original."""
        resolved = Path(file_path).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"Arquivo de entrada não encontrado: {resolved}")

        raw_content = resolved.read_text(encoding="utf-8")
        if not raw_content.strip():
            raise ValueError(f"O arquivo de entrada está vazio: {resolved}")

        extracted_at = datetime.now(timezone.utc).isoformat()
        logger.info("Texto bruto carregado do arquivo: %s (%d caracteres)", resolved, len(raw_content))
        return SourceExtractionArtifact(
            source_type="file",
            source_reference=str(resolved),
            raw_input_text=raw_content,
            extracted_text=raw_content.strip(),
            extracted_at=extracted_at,
            used_playwright=False,
            metadata={"file_path": str(resolved), "char_count": len(raw_content)},
        )

    def extract_from_raw_text(self, raw_text: str) -> SourceExtractionArtifact:
        """Processa texto colado interativamente preservando o original intacto."""
        if not raw_text or not raw_text.strip():
            raise ValueError("O texto bruto fornecido interativamente está vazio.")

        extracted_at = datetime.now(timezone.utc).isoformat()
        return SourceExtractionArtifact(
            source_type="interactive",
            source_reference="terminal_stdin",
            raw_input_text=raw_text,
            extracted_text=raw_text.strip(),
            extracted_at=extracted_at,
            used_playwright=False,
            metadata={"char_count": len(raw_text)},
        )

    def _fetch_static_html(self, url: str) -> str:
        """Busca o HTML via HTTP com retry limitado."""
        last_error: Optional[Exception] = None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        }
        for attempt in range(1, self.max_retries + 2):
            try:
                with httpx.Client(timeout=self.http_timeout, follow_redirects=True, headers=headers) as client:
                    response = client.get(url)
                    if response.status_code in (404, 410):
                        raise SourceNotFoundError(
                            f"Página de origem não encontrada (HTTP {response.status_code}): {url}"
                        )
                    response.raise_for_status()
                    return response.text
            except SourceNotFoundError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Tentativa %d/%d falhou ao buscar %s via HTTP: %s",
                    attempt,
                    self.max_retries + 1,
                    url,
                    exc,
                )
                if attempt <= self.max_retries:
                    time.sleep(0.5 * attempt)
        raise RuntimeError(f"Falha ao obter conteúdo da URL {url}: {last_error}") from last_error

    def extract_from_url(
        self,
        source_url: str,
        *,
        force_playwright: bool = False,
    ) -> SourceExtractionArtifact:
        """Extrai e organiza o conteúdo textual relevante de uma URL pública."""
        validated_url = validate_http_url(source_url)
        extracted_at = datetime.now(timezone.utc).isoformat()
        used_playwright = False
        raw_html = ""

        if not force_playwright:
            try:
                raw_html = self._fetch_static_html(validated_url)
            except SourceNotFoundError:
                raise
            except Exception as http_err:
                logger.info(
                    "Busca HTTP inicial falhou (%s); tentando renderização com Playwright.",
                    http_err,
                )
                force_playwright = True

        extracted_text, title = extract_clean_text_from_html(raw_html, source_url=validated_url)

        if force_playwright or needs_javascript_rendering(raw_html, extracted_text):
            raw_html = self._playwright_fetcher(validated_url, self.headless)
            used_playwright = True
            extracted_text, title = extract_clean_text_from_html(raw_html, source_url=validated_url)

        if not extracted_text.strip():
            raise ValueError(f"Não foi possível extrair conteúdo textual útil da URL: {validated_url}")

        header_banner = (
            f"FONTE: {validated_url}\n"
            f"DATA_EXTRACAO_UTC: {extracted_at}\n"
            f"RENDERIZADO_VIA_PLAYWRIGHT: {'sim' if used_playwright else 'nao'}\n"
            f"{'-' * 60}\n"
        )
        full_extracted_text = f"{header_banner}{extracted_text}"

        return SourceExtractionArtifact(
            source_type="url",
            source_reference=validated_url,
            raw_input_text=raw_html,
            extracted_text=full_extracted_text,
            extracted_at=extracted_at,
            used_playwright=used_playwright,
            title=title,
            metadata={
                "source_url": validated_url,
                "extracted_at": extracted_at,
                "used_playwright": used_playwright,
                "title": title,
            },
        )


def combine_source_artifacts(artifacts: List[SourceExtractionArtifact]) -> SourceExtractionArtifact:
    """Une várias fontes em um único artefato, separando cada uma com um cabeçalho identificado."""
    if not artifacts:
        raise ValueError("Nenhuma fonte de entrada para combinar.")
    if len(artifacts) == 1:
        return artifacts[0]

    total = len(artifacts)
    extracted_parts = []
    raw_parts = []
    for idx, art in enumerate(artifacts, start=1):
        banner = f"=== FONTE {idx}/{total} ({art.source_type}): {art.source_reference} ==="
        extracted_parts.append(f"{banner}\n{art.extracted_text.strip()}")
        raw_parts.append(f"{banner}\n{art.raw_input_text}")

    return SourceExtractionArtifact(
        source_type="multi",
        source_reference="; ".join(a.source_reference for a in artifacts),
        raw_input_text="\n\n".join(raw_parts),
        extracted_text="\n\n".join(extracted_parts),
        used_playwright=any(a.used_playwright for a in artifacts),
        title=next((a.title for a in artifacts if a.title), None),
        metadata={
            "sources": [
                {"source_type": a.source_type, "source_reference": a.source_reference}
                for a in artifacts
            ]
        },
    )

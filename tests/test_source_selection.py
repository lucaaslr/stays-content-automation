"""Testes da seleção de fontes: --id, várias --source-url e busca padrão na Multitemporada."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import httpx
import pytest
from rich.console import Console

from stays_automation.cli import _normalize_source_urls, build_arg_parser, execute_automation_flow
from stays_automation.llm_client import ClaudeClient
from stays_automation.models import SourceExtractionArtifact
from stays_automation.settings import build_url_from_template
from stays_automation.source_extractor import (
    SourceExtractor,
    SourceNotFoundError,
    combine_source_artifacts,
)
from test_content_mapper import FIXTURE_HTML_PATH, FIXTURE_JSON_PATH, FakeAnthropicSDK, FakeStaysPage


def _artifact(ref: str, text: str, source_type: str = "url") -> SourceExtractionArtifact:
    return SourceExtractionArtifact(
        source_type=source_type,
        source_reference=ref,
        raw_input_text=f"<html>{text}</html>",
        extracted_text=text,
    )


def test_parser_accepts_id_alias_and_repeated_source_urls() -> None:
    args = build_arg_parser().parse_args(
        ["--id", "ab02i", "--source-url", "https://a.example/x", "--source-url", "https://b.example/y"]
    )
    assert args.property_id == "ab02i"
    assert args.source_url == ["https://a.example/x", "https://b.example/y"]
    assert build_arg_parser().parse_args(["--property-id", "AB02I"]).property_id == "AB02I"


def test_normalize_source_urls_splits_commas_and_dedupes() -> None:
    assert _normalize_source_urls(None) == []
    assert _normalize_source_urls("https://a.example") == ["https://a.example"]
    assert _normalize_source_urls(["https://a.example, https://b.example", "https://a.example"]) == [
        "https://a.example",
        "https://b.example",
    ]


def test_build_url_from_template() -> None:
    assert (
        build_url_from_template("https://www.multitemporada.com/pt/apartment/{property_id}", "ab02i")
        == "https://www.multitemporada.com/pt/apartment/AB02I"
    )
    with pytest.raises(ValueError, match="property_id"):
        build_url_from_template("https://example.com/sem-marcador", "AB02I")


def test_combine_source_artifacts_keeps_single_and_merges_many() -> None:
    single = _artifact("https://a.example", "Texto A")
    assert combine_source_artifacts([single]) is single

    merged = combine_source_artifacts(
        [_artifact("https://a.example", "Texto A"), _artifact("/tmp/x.txt", "Texto B", "file")]
    )
    assert merged.source_type == "multi"
    assert merged.source_reference == "https://a.example; /tmp/x.txt"
    assert "=== FONTE 1/2 (url): https://a.example ===\nTexto A" in merged.extracted_text
    assert "=== FONTE 2/2 (file): /tmp/x.txt ===\nTexto B" in merged.extracted_text
    assert len(merged.metadata["sources"]) == 2


def test_404_source_raises_without_playwright_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client

    def client_with_404(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: httpx.Response(404, text="not found"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr("stays_automation.source_extractor.httpx.Client", client_with_404)
    playwright_calls: List[str] = []
    extractor = SourceExtractor(playwright_fetcher=lambda url, headless: playwright_calls.append(url) or "")

    with pytest.raises(SourceNotFoundError, match="HTTP 404"):
        extractor.extract_from_url("https://www.multitemporada.com/pt/apartment/ZZ99X")
    assert playwright_calls == []


def _run_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_args: List[str]) -> tuple[int, List[str]]:
    fetched: List[str] = []

    def fake_extract_from_url(self, url: str, *, force_playwright: bool = False):
        fetched.append(url)
        return _artifact(url, "Endereço: Rua das Flores, 100, Urubici - SC")

    monkeypatch.setattr(SourceExtractor, "extract_from_url", fake_extract_from_url)
    fake_claude = ClaudeClient(
        api_key="sk-ant-test-dummy-key-for-unit-test",
        sdk_client=FakeAnthropicSDK(FIXTURE_JSON_PATH.read_text(encoding="utf-8")),
    )
    fake_page = FakeStaysPage(
        html_content=FIXTURE_HTML_PATH.read_text(encoding="utf-8"),
        url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
    )
    args = build_arg_parser().parse_args(cli_args + ["--output-dir", str(tmp_path), "--dry-run"])
    exit_code = execute_automation_flow(
        args,
        console=Console(record=True),
        input_fn=lambda prompt: "s",
        claude_client_override=fake_claude,
        page_override=fake_page,
    )
    return exit_code, fetched


def test_only_id_builds_stays_url_and_uses_multitemporada_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, fetched = _run_flow(tmp_path, monkeypatch, ["--id", "ab25i"])

    assert exit_code == 0
    assert fetched == ["https://www.multitemporada.com/pt/apartment/AB25I"]
    run_dir = next(tmp_path.glob("*_AB25I"))
    report = json.loads((run_dir / "execution_report.json").read_text(encoding="utf-8"))
    assert report["target_url"] == "https://ssmt.stays.com.br/i/apartment/AB25I/setup"


def test_multiple_source_urls_are_all_fetched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, fetched = _run_flow(
        tmp_path,
        monkeypatch,
        ["--id", "AB25I", "--source-url", "https://a.example/AB25I", "--source-url", "https://b.example/AB25I"],
    )
    assert exit_code == 0
    assert fetched == ["https://a.example/AB25I", "https://b.example/AB25I"]


def test_id_divergent_from_target_url_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, fetched = _run_flow(
        tmp_path,
        monkeypatch,
        ["--id", "AB02I", "--target-url", "https://ssmt.stays.com.br/i/apartment/AB25I/setup"],
    )
    assert exit_code == 2
    assert fetched == []

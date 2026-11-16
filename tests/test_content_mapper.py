"""Testes de mapeamento de conteúdo, conflitos, HTML local, mocks do Claude/Stays e dry-run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from rich.console import Console

from stays_automation.claude_client import ClaudeClient
from stays_automation.cli import build_arg_parser, execute_automation_flow
from stays_automation.content_mapper import ContentMapper, normalize_label
from stays_automation.field_discovery import FieldDiscoveryService
from stays_automation.models import (
    ClaudeExtractionResponse,
    ConflictDecision,
    CustomFieldsCatalog,
    ExecutionResultStatus,
    FieldAction,
    FieldExtractionResult,
    FieldStatus,
    PropertyMetadata,
    ExtractionSummary,
    SelectorsConfig,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FIELDS_PATH = PROJECT_ROOT / "config" / "custom_fields.yaml"
CONFIG_SELECTORS_PATH = PROJECT_ROOT / "config" / "selectors.yaml"
FIXTURE_HTML_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_stays_setup.html"
FIXTURE_JSON_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_claude_response.json"
EXAMPLE_INPUT_PATH = PROJECT_ROOT / "examples" / "entrada_exemplo.txt"


class FakeLocator:
    """Simula um Locator do Playwright sobre um dicionário de elementos HTML em memória."""

    def __init__(self, page: "FakeStaysPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        if "save-custom-fields" in self._selector or "Salvar" in self._selector:
            return 1
        if "toast-success" in self._selector:
            return 1 if self._page.save_clicked else 0
        if any(tok in self._selector for tok in ("password", "senha", "otp", "mfa", "two_factor", "recaptcha", "hcaptcha", "turnstile")):
            return 0  # Simula sessão já autenticada sem desafio CAPTCHA/MFA
        return 1 if self._page.has_selector(self._selector) else 0

    def is_visible(self, timeout: int = 1000) -> bool:
        return self.count() > 0

    def input_value(self) -> str:
        return self._page.get_value_by_selector(self._selector)

    def fill(self, value: str) -> None:
        self._page.set_value_by_selector(self._selector, value)

    def click(self) -> None:
        if "save-custom-fields" in self._selector or "Salvar" in self._selector:
            self._page.save_clicked = True

    def scroll_into_view_if_needed(self, timeout: int = 1000) -> None:
        return None


class FakeStaysPage:
    """Mock da página do Stays alimentado pelo HTML local (sample_stays_setup.html)."""

    def __init__(self, html_content: str, url: str) -> None:
        self._html = html_content
        self.url = url
        self.save_clicked = False
        self.filled_by_selector: Dict[str, str] = {}
        self.screenshots_taken: List[str] = []

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> None:
        self.url = url

    def wait_for_load_state(self, state: str = "networkidle", timeout: int = 5000) -> None:
        return None

    def content(self) -> str:
        return self._html

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def has_selector(self, selector: str) -> bool:
        # Aceita seletores gerados pela descoberta de campos do HTML local
        return any(
            token in selector
            for token in ("data-testid=", "id=", "name=", "xpath=//label", "div:has(> label")
        )

    def get_value_by_selector(self, selector: str) -> str:
        return self.filled_by_selector.get(selector, "")

    def set_value_by_selector(self, selector: str, value: str) -> None:
        self.filled_by_selector[selector] = value

    def screenshot(self, path: str, full_page: bool = True) -> None:
        self.screenshots_taken.append(path)
        from stays_automation.artifacts import PLACEHOLDER_PNG_BYTES

        Path(path).write_bytes(PLACEHOLDER_PNG_BYTES)


class FakeAnthropicSDK:
    """Mock do SDK oficial da Anthropic que retorna o JSON de fixture sem acessar a rede."""

    def __init__(self, response_json_text: str) -> None:
        self._response_json_text = response_json_text
        self.messages = self
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"content": [{"type": "text", "text": self._response_json_text}]}


def test_label_normalization_and_matching() -> None:
    """Testa normalização de rótulos com acentos, caixa variada e preservação do campo '.'."""
    assert normalize_label("  Esteja Ciente (Pós reserva) ") == "esteja ciente pos reserva"
    assert normalize_label("Wi-fi e dispositivos eletrônicos") == "wi fi e dispositivos eletronicos"
    assert normalize_label(".") == "."

    catalog = CustomFieldsCatalog.from_yaml(CONFIG_FIELDS_PATH)
    mapper = ContentMapper(catalog)

    assert mapper.match_field_config_by_label("Endereço da Propriedade") is not None
    assert mapper.match_field_config_by_label("endereco da propriedade").field_key == "property_address"
    assert mapper.match_field_config_by_label(".").field_key == "dot_placeholder"


def test_local_html_field_discovery_finds_all_37_fields() -> None:
    """Testa a descoberta de campos com HTML local representando os 37 campos do Stays."""
    catalog = CustomFieldsCatalog.from_yaml(CONFIG_FIELDS_PATH)
    selectors_cfg = SelectorsConfig.from_yaml(CONFIG_SELECTORS_PATH)
    service = FieldDiscoveryService(catalog=catalog, selectors_config=selectors_cfg)

    raw_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    discovered, existing_values = service.discover_from_html(raw_html)

    assert len(discovered) == 37
    mapped_keys = {d.field_key for d in discovered if d.field_key}
    assert len(mapped_keys) == 37
    assert "dot_placeholder" in mapped_keys
    assert existing_values.get("property_address") == "Endereço Antigo Diferente, 100"


def test_conflict_detection_char_limit_missing_and_confidence_rules() -> None:
    """
    Verifica:
    - conflito com valor já existente no Stays;
    - limite de caracteres;
    - URL inválida em campo exclusivo de link;
    - bloqueio por baixa confiança;
    - não preenchimento automático do campo '.';
    - campos ausentes não inventados.
    """
    catalog = CustomFieldsCatalog.from_yaml(CONFIG_FIELDS_PATH)
    mapper = ContentMapper(catalog, confidence_threshold=0.80)

    claude_resp = ClaudeExtractionResponse(
        property=PropertyMetadata(
            target_url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            property_id="AB25I",
            source_type="file",
            source_reference="entrada.txt",
        ),
        fields=[
            # 1. Conflito com valor existente no Stays
            FieldExtractionResult(
                field_key="property_address",
                stays_label="Endereço da Propriedade",
                value="Novo Endereço Correto, 1450",
                status=FieldStatus.FOUND,
                confidence=0.98,
            ),
            # 2. Excede limite de caracteres (max_length de property_review_link é 500, ou checkin é 2000)
            FieldExtractionResult(
                field_key="checkin_instructions",
                stays_label="Instruções para check-in",
                value="A" * 2505,
                status=FieldStatus.FOUND,
                confidence=0.95,
            ),
            # 3. URL inválida em campo que exige somente URL
            FieldExtractionResult(
                field_key="google_maps_url",
                stays_label="Abrir no Google Maps",
                value="Texto sem nenhum link valido",
                status=FieldStatus.FOUND,
                confidence=0.90,
            ),
            # 4. Baixa confiança (< 0.80)
            FieldExtractionResult(
                field_key="parking",
                stays_label="Estacionamento",
                value="Talvez tenha 1 vaga",
                status=FieldStatus.FOUND,
                confidence=0.65,
            ),
            # 5. Tentativa da LLM de preencher o campo '.' (deve ser bloqueada!)
            FieldExtractionResult(
                field_key="dot_placeholder",
                stays_label=".",
                value="Valor indevido",
                status=FieldStatus.FOUND,
                confidence=0.99,
            ),
        ],
        summary=ExtractionSummary(
            total_fields=37,
            found=5,
            missing=32,
            conflicting=0,
            requires_review=0,
        ),
    )

    records = mapper.build_execution_records(
        property_id="AB25I",
        target_url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
        claude_response=claude_resp,
        existing_stays_values={"property_address": "Endereço Antigo no Sistema, 100"},
    )

    assert len(records) == 37
    by_key = {r.field_key: r for r in records}

    # Conflito detectado e não sobrescrito silenciosamente
    addr_rec = by_key["property_address"]
    assert addr_rec.status == FieldStatus.CONFLICTING
    assert addr_rec.action == FieldAction.SKIP_CONFLICT
    assert addr_rec.current_value == "Endereço Antigo no Sistema, 100"
    assert addr_rec.proposed_value == "Novo Endereço Correto, 1450"

    # Decisão explícita do usuário para substituir resolve o conflito
    ContentMapper.apply_conflict_decision(addr_rec, ConflictDecision.REPLACE)
    assert addr_rec.action == FieldAction.OVERWRITE

    # Excesso de caracteres bloqueado
    checkin_rec = by_key["checkin_instructions"]
    assert checkin_rec.status == FieldStatus.INVALID_FORMAT
    assert checkin_rec.action == FieldAction.SKIP_INVALID

    # URL inválida bloqueada
    gmaps_rec = by_key["google_maps_url"]
    assert gmaps_rec.status == FieldStatus.INVALID_FORMAT
    assert gmaps_rec.action == FieldAction.SKIP_INVALID

    # Baixa confiança bloqueada
    parking_rec = by_key["parking"]
    assert parking_rec.action == FieldAction.SKIP_LOW_CONFIDENCE
    assert parking_rec.will_be_filled is False

    # Campo '.' bloqueado
    dot_rec = by_key["dot_placeholder"]
    assert dot_rec.action == FieldAction.BLOCKED_DOT_FIELD
    assert dot_rec.proposed_value is None
    assert dot_rec.will_be_filled is False

    # Campo ausente permanece vazio (value=None)
    pets_rec = by_key["pet_policies"]
    assert pets_rec.status == FieldStatus.MISSING
    assert pets_rec.proposed_value is None
    assert pets_rec.action == FieldAction.SKIP_EMPTY


def test_end_to_end_dry_run_with_mocked_claude_and_stays_never_saves(tmp_path: Path) -> None:
    """
    Executa o fluxo completo com mock da API do Claude e mock da página do Stays:
    - verifica que dry-run preenche os campos aprovados mas NUNCA aciona o botão Salvar;
    - verifica geração de todos os artefatos (JSONs, CSV, log, screenshots) com dados sensíveis mascarados.
    """
    fixture_json_str = FIXTURE_JSON_PATH.read_text(encoding="utf-8")
    fake_sdk = FakeAnthropicSDK(fixture_json_str)
    fake_claude = ClaudeClient(
        api_key="sk-ant-test-dummy-key-for-unit-test",
        sdk_client=fake_sdk,
    )

    raw_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    fake_page = FakeStaysPage(
        html_content=raw_html,
        url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
    )

    # Respostas simuladas no terminal:
    # 1ª pergunta: conflito em Endereço da Propriedade -> 's' (substituir)
    # 2ª pergunta: confirmar preenchimento após prévia -> 's' (sim)
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
            "--dry-run",
        ]
    )

    exit_code = execute_automation_flow(
        args,
        console=Console(record=True),
        input_fn=lambda prompt: next(answers, "s"),
        claude_client_override=fake_claude,
        page_override=fake_page,
    )

    assert exit_code == 0
    assert fake_page.save_clicked is False  # Dry-run NUNCA clica em Salvar!
    assert len(fake_page.filled_by_selector) == 4

    # Verifica artefatos gerados
    run_dirs = list(tmp_path.glob("*_AB25I"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    expected_files = [
        "input_original.txt",
        "source_extracted.txt",
        "claude_request_sanitized.json",
        "claude_response.json",
        "normalized_fields.json",
        "field_mapping.json",
        "preview.csv",
        "execution_report.json",
        "execution.log",
        "before_fill.png",
        "after_fill.png",
    ]
    for fname in expected_files:
        assert (run_dir / fname).exists(), f"Artefato obrigatório ausente: {fname}"

    # Verifica que segredos e senhas de Wi-Fi / cofre foram mascarados nos artefatos
    csv_text = (run_dir / "preview.csv").read_text(encoding="utf-8")
    assert "property_id,target_url,field_key,stays_label,current_value,proposed_value,status,confidence,sensitive,action,result,warning" in csv_text
    assert "Serra@2026!" not in csv_text
    assert "4829" not in csv_text
    assert "91735#" not in csv_text
    assert "********" in csv_text
    assert "****" in csv_text

    req_text = (run_dir / "claude_request_sanitized.json").read_text(encoding="utf-8")
    assert "sk-ant-test-dummy-key-for-unit-test" not in req_text


def test_cli_rejects_save_and_dry_run_together(tmp_path: Path) -> None:
    """Garante que a CLI retorna código de erro diferente de zero se --save e --dry-run forem combinados."""
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--target-url",
            "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            "--source-file",
            str(EXAMPLE_INPUT_PATH),
            "--output-dir",
            str(tmp_path),
            "--save",
            "--dry-run",
        ]
    )
    exit_code = execute_automation_flow(args, console=Console(record=True))
    assert exit_code != 0


def test_inspect_mode_generates_sanitized_html_and_field_mapping(tmp_path: Path) -> None:
    """Verifica que o subcomando inspect salva field_mapping.json, HTML sanitizado e screenshot."""
    raw_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    fake_page = FakeStaysPage(
        html_content=raw_html,
        url="https://ssmt.stays.com.br/i/apartment/AB25I/setup",
    )
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "inspect",
            "--target-url",
            "https://ssmt.stays.com.br/i/apartment/AB25I/setup",
            "--output-dir",
            str(tmp_path),
        ]
    )
    exit_code = execute_automation_flow(
        args,
        console=Console(record=True),
        page_override=fake_page,
    )
    assert exit_code == 0
    run_dirs = list(tmp_path.glob("*_AB25I"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "field_mapping.json").exists()
    assert (run_dir / "sanitized_page.html").exists()
    assert (run_dir / "before_fill.png").exists()

    mapping_data = json.loads((run_dir / "field_mapping.json").read_text(encoding="utf-8"))
    assert mapping_data["configured_catalog_count"] == 37
    assert mapping_data["mapped_to_catalog_count"] == 37


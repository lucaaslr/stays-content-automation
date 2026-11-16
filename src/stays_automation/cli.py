"""Interface de linha de comando (CLI) principal e modo de inspeção de seletores."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from rich.console import Console
from rich.table import Table

from stays_automation.artifacts import ArtifactManager
from stays_automation.llm_client import (
    BaseLLMClient,
    ClaudeClient,
    OllamaClient,
    create_llm_client,
)
from stays_automation.content_mapper import ContentMapper
from stays_automation.models import (
    CustomFieldsCatalog,
    ExecutionReport,
    ExecutionResultStatus,
    FieldAction,
    FieldStatus,
    SelectorsConfig,
    validate_http_url,
)
from stays_automation.reviewer import TerminalReviewer
from stays_automation.security import SecretScrubber
from stays_automation.settings import (
    build_url_from_template,
    extract_property_id_from_url,
    load_settings,
)
from stays_automation.source_extractor import SourceExtractor, combine_source_artifacts
from stays_automation.stays_client import StaysAutomationClient
from stays_automation.logging_config import configure_logging


def build_arg_parser() -> argparse.ArgumentParser:
    """Constrói o parser de argumentos da CLI suportando execução principal e subcomando inspect."""
    parser = argparse.ArgumentParser(
        prog="stays_automation",
        description="Automação segura com Playwright e Claude API para campos personalizados no Stays.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["run", "inspect"],
        default="run",
        help="Modo de operação: 'run' (padrão) para extrair e preencher ou 'inspect' para descobrir seletores.",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=None,
        help=(
            "URL da página de configuração do imóvel no Stays (ex.: https://ssmt.stays.com.br/i/apartment/AB25I/setup). "
            "Se omitida, é montada a partir de --id."
        ),
    )
    parser.add_argument(
        "--source-url",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "URL pública com informações do imóvel. Pode ser repetida para usar vários sites "
            "(ex.: --source-url URL1 --source-url URL2)."
        ),
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        default=None,
        help="Caminho para arquivo de texto bruto com informações do imóvel (pode ser combinado com --source-url).",
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        default=False,
        help="Solicita que o texto da fonte seja colado no terminal.",
    )
    parser.add_argument(
        "--id",
        "--property-id",
        dest="property_id",
        type=str,
        default=None,
        help=(
            "Código do imóvel (ex.: AB02I). Sozinho, monta a URL do Stays e, se nenhuma fonte for "
            "informada, busca as informações em https://www.multitemporada.com/pt/apartment/<ID>."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Diretório base para gravação dos artefatos (padrão: ./artifacts).",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.80,
        help="Limiar mínimo de confiança [0.0 a 1.0] para preenchimento automático (padrão: 0.80).",
    )
    parser.add_argument(
        "--skip-empty",
        dest="skip_empty",
        action="store_true",
        default=True,
        help="Ignora campos sem valor encontrado na fonte (padrão: ativo).",
    )
    parser.add_argument(
        "--no-skip-empty",
        dest="skip_empty",
        action="store_false",
        help="Desativa o comportamento padrão de pular campos vazios.",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="Caminho da pasta de artefatos de uma execução anterior para retomar o progresso.",
    )
    parser.add_argument(
        "--headless",
        dest="headless_override",
        action="store_true",
        default=None,
        help="Executa o navegador Chromium em modo invisível (headless).",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless_override",
        action="store_false",
        help="Executa o navegador Chromium em modo visível (recomendado para login/inspeção).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run_flag",
        action="store_true",
        default=False,
        help="Preenche os campos aprovados, mas NUNCA clica em Salvar (padrão).",
    )
    parser.add_argument(
        "--save",
        dest="save_flag",
        action="store_true",
        default=False,
        help="Permite salvar as alterações no Stays após resumo final e confirmação explícita.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Caminho opcional para arquivo .env customizado.",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["ollama", "anthropic", "claude_cli"],
        default=None,
        help=(
            "Provedor de LLM a utilizar: 'ollama' (padrão local), 'anthropic' (Claude API) "
            "ou 'claude_cli' (Claude Code CLI com login da assinatura, sem chave de API)."
        ),
    )
    parser.add_argument(
        "--ollama-url",
        type=str,
        default=None,
        help="URL base do servidor Ollama (padrão: http://localhost:11434).",
    )
    parser.add_argument(
        "--ollama-model",
        type=str,
        default=None,
        help="Nome do modelo local no Ollama (padrão: llama3:latest).",
    )
    parser.add_argument(
        "--ollama-timeout",
        type=float,
        default=None,
        help="Timeout em segundos para requisições ao Ollama (padrão: 600s).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Nome do modelo (sobrescreve o modelo do provedor ativo).",
    )
    return parser


def _normalize_source_urls(raw: Any) -> List[str]:
    """Aceita None, string única ou lista (inclusive itens separados por vírgula) e remove duplicadas."""
    if not raw:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    urls: List[str] = []
    for item in items:
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
    return urls


def _prompt_interactive_source_text(
    console: Console,
    input_fn: Callable[[str], str] = input,
) -> str:
    """Lê texto bruto colado interativamente no terminal até uma linha contendo apenas FIM ou EOF."""
    console.print(
        "\n[bold cyan]Entrada interativa de texto bruto:[/bold cyan] "
        "Cole as informações do imóvel abaixo. Digite [bold]FIM[/bold] em uma nova linha para encerrar:"
    )
    lines: List[str] = []
    while True:
        try:
            line = input_fn("")
        except EOFError:
            break
        if line.strip().upper() == "FIM":
            break
        lines.append(line)
    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("Nenhum texto foi informado na entrada interativa.")
    return text


def run_inspect_mode(
    *,
    target_url: str,
    property_id: str,
    settings: Any,
    catalog: CustomFieldsCatalog,
    selectors_config: SelectorsConfig,
    artifact_manager: ArtifactManager,
    scrubber: SecretScrubber,
    console: Console,
    page_override: Optional[Any] = None,
) -> int:
    """Executa o subcomando `inspect` para descoberta de rótulos e seletores no Stays."""
    # O modo inspect deve abrir o navegador visível por padrão
    if page_override is None:
        settings.headless = False

    reviewer = TerminalReviewer(
        console=console,
        scrubber=scrubber,
        mask_phones=settings.mask_phone_numbers,
    )
    reviewer.show_divergence_notice(catalog.divergence_message)

    with StaysAutomationClient(
        settings=settings,
        catalog=catalog,
        selectors_config=selectors_config,
        artifact_manager=artifact_manager,
        scrubber=scrubber,
        page_override=page_override,
    ) as stays:
        stays.authenticate_if_needed()
        stays.navigate_to_property_custom_fields(target_url, property_id)
        stays.capture_screenshot(artifact_manager.before_fill_screenshot_path)
        discovered, _, mapping_report = stays.inspect_and_discover_fields(
            target_url=target_url,
            property_id=property_id,
            save_sanitized_html=True,
        )

    table = Table(
        title=f"Inspeção de Seletores no Stays - Imóvel {property_id}",
        header_style="bold magenta",
    )
    table.add_column("#", justify="right", width=3)
    table.add_column("Rótulo Visível", style="bold")
    table.add_column("Field Key (Catálogo)")
    table.add_column("Tag")
    table.add_column("Estratégia")
    table.add_column("Seletor Recomendado")

    for idx, item in enumerate(discovered, start=1):
        table.add_row(
            str(idx),
            item.stays_label,
            item.field_key or "[yellow]não mapeado[/yellow]",
            item.element_tag,
            item.strategy_used,
            item.recommended_selector,
        )

    console.print(table)
    console.print(
        f"\n[bold green]Inspeção concluída![/bold green]\n"
        f"- Campos catalogados reconhecidos: {mapping_report['mapped_to_catalog_count']}/{catalog.actual_count}\n"
        f"- Mapeamento salvo em: {artifact_manager.run_dir / 'field_mapping.json'}\n"
        f"- HTML sanitizado salvo em: {artifact_manager.run_dir / 'sanitized_page.html'}\n"
        f"- Captura de tela salva em: {artifact_manager.before_fill_screenshot_path}"
    )
    return 0


def execute_automation_flow(
    args: argparse.Namespace,
    *,
    console: Optional[Console] = None,
    input_fn: Callable[[str], str] = input,
    claude_client_override: Optional[ClaudeClient] = None,
    llm_client_override: Optional[BaseLLMClient] = None,
    page_override: Optional[Any] = None,
) -> int:
    """Fluxo completo de validação, extração, estruturação via LLM, prévia, preenchimento e artefatos."""
    active_console = console or Console()
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        settings = load_settings(
            env_file=args.env_file,
            headless_override=args.headless_override,
            dry_run_flag=args.dry_run_flag,
            save_flag=args.save_flag,
            output_dir_override=args.output_dir,
            confidence_threshold_override=args.confidence_threshold,
            skip_empty_override=args.skip_empty,
            llm_provider_override=getattr(args, "llm_provider", None),
            ollama_url_override=getattr(args, "ollama_url", None),
            ollama_model_override=getattr(args, "ollama_model", None),
            ollama_timeout_override=getattr(args, "ollama_timeout", None),
            model_override=getattr(args, "model", None),
        )
    except ValueError as cfg_err:
        active_console.print(f"[bold red]Erro de configuração:[/bold red] {cfg_err}")
        return 2

    scrubber = SecretScrubber(
        [settings.stays_login, settings.stays_password, settings.anthropic_api_key]
    )

    explicit_property_id = (
        args.property_id.strip().upper() if args.property_id and args.property_id.strip() else None
    )

    target_url = args.target_url
    if not target_url and explicit_property_id:
        try:
            target_url = build_url_from_template(settings.stays_property_url_template, explicit_property_id)
        except ValueError as tpl_err:
            active_console.print(f"[bold red]STAYS_PROPERTY_URL_TEMPLATE inválido:[/bold red] {tpl_err}")
            return 2
        active_console.print(f"URL do Stays montada a partir do código {explicit_property_id}: {target_url}")
    if not target_url:
        target_url = input_fn("Informe a URL de configuração do imóvel no Stays (--target-url): ").strip()

    try:
        target_url = validate_http_url(target_url)
    except ValueError as url_err:
        active_console.print(f"[bold red]URL de destino inválida:[/bold red] {url_err}")
        return 2

    url_property_id = extract_property_id_from_url(target_url)
    if (
        explicit_property_id
        and url_property_id != "UNKNOWN"
        and url_property_id != explicit_property_id
    ):
        active_console.print(
            f"[bold red]Código divergente:[/bold red] --id {explicit_property_id} não corresponde ao "
            f"imóvel da --target-url ({url_property_id})."
        )
        return 2
    property_id = explicit_property_id or url_property_id

    artifact_manager = ArtifactManager(
        base_output_dir=settings.output_dir,
        property_id=property_id,
        scrubber=scrubber,
        mask_phones=settings.mask_phone_numbers,
        resume_dir=args.resume,
    )
    logger = configure_logging(
        scrubber=scrubber,
        log_file=artifact_manager.log_file_path,
        console=active_console,
    )

    try:
        catalog = CustomFieldsCatalog.from_yaml(settings.custom_fields_config_path)
        selectors_config = SelectorsConfig.from_yaml(settings.selectors_config_path)
    except Exception as yaml_err:
        logger.error("Erro ao carregar arquivos YAML de configuração: %s", yaml_err)
        return 1

    if catalog.has_count_divergence:
        logger.warning(catalog.divergence_message)

    if args.mode == "inspect":
        try:
            return run_inspect_mode(
                target_url=target_url,
                property_id=property_id,
                settings=settings,
                catalog=catalog,
                selectors_config=selectors_config,
                artifact_manager=artifact_manager,
                scrubber=scrubber,
                console=active_console,
                page_override=page_override,
            )
        except Exception as inspect_err:
            logger.error("Falha durante o modo inspect: %s", inspect_err)
            return 1

    # Modo principal ('run'): resolve as fontes de entrada (URLs, arquivo e/ou texto colado)
    source_urls = _normalize_source_urls(getattr(args, "source_url", None))

    interactive_text: Optional[str] = None
    if getattr(args, "paste", False):
        try:
            interactive_text = _prompt_interactive_source_text(active_console, input_fn=input_fn)
        except ValueError as empty_err:
            logger.error(str(empty_err))
            return 2

    if not source_urls and args.source_file is None and interactive_text is None:
        if property_id == "UNKNOWN":
            logger.error(
                "Nenhuma fonte informada e não foi possível identificar o código do imóvel. "
                "Use --id, --source-url, --source-file ou --paste."
            )
            return 2
        try:
            default_url = build_url_from_template(settings.default_source_url_template, property_id)
        except ValueError as tpl_err:
            logger.error("DEFAULT_SOURCE_URL_TEMPLATE inválido: %s", tpl_err)
            return 2
        logger.info("Nenhuma fonte informada; buscando informações em %s", default_url)
        source_urls = [default_url]

    extractor = SourceExtractor(headless=settings.headless)
    try:
        artifacts = [extractor.extract_from_url(url) for url in source_urls]
        if args.source_file is not None:
            artifacts.append(extractor.extract_from_file(args.source_file))
        if interactive_text is not None:
            artifacts.append(extractor.extract_from_raw_text(interactive_text))
        source_artifact = combine_source_artifacts(artifacts)
    except Exception as ext_err:
        logger.error("Falha na extração da fonte de entrada: %s", ext_err)
        return 1
    logger.info(
        "Fonte(s) carregada(s): %s (%d caracteres)",
        source_artifact.source_reference,
        len(source_artifact.extracted_text),
    )

    artifact_manager.write_input_original(source_artifact.raw_input_text, preserve_exact=True)
    artifact_manager.write_source_extracted(source_artifact.extracted_text)

    # Integração com LLM (Ollama ou Claude)
    try:
        if llm_client_override is not None:
            llm_client = llm_client_override
        elif claude_client_override is not None:
            llm_client = claude_client_override
        else:
            settings.validate_for_llm()
            llm_client = create_llm_client(settings, scrubber=scrubber)

        active_console.print(
            f"[bold cyan]Classificando campos com LLM ([bold]{settings.llm_provider}[/bold] | modelo: {llm_client.model})...[/bold cyan]"
        )
        sanitized_req, raw_resp, claude_response = llm_client.classify_and_structure(
            target_url=target_url,
            property_id=property_id,
            source_artifact=source_artifact,
            catalog=catalog,
        )
        artifact_manager.write_claude_request_sanitized(sanitized_req)
        artifact_manager.write_claude_response(claude_response)
    except Exception as llm_err:
        logger.error("Falha no processamento com a LLM (%s): %s", settings.llm_provider, llm_err)
        active_console.print(
            f"[bold red]Erro na integração com a LLM ({settings.llm_provider}):[/bold red] {llm_err}"
        )
        return 1

    reviewer = TerminalReviewer(
        console=active_console,
        scrubber=scrubber,
        mask_phones=settings.mask_phone_numbers,
        input_fn=input_fn,
    )
    reviewer.show_divergence_notice(catalog.divergence_message)

    mapper = ContentMapper(
        catalog=catalog,
        confidence_threshold=settings.confidence_threshold,
        skip_empty=settings.skip_empty,
    )

    completed_resume_keys = (
        artifact_manager.load_completed_fields_for_resume() if args.resume else set()
    )

    save_executed = False
    errors: List[str] = []

    try:
        with StaysAutomationClient(
            settings=settings,
            catalog=catalog,
            selectors_config=selectors_config,
            artifact_manager=artifact_manager,
            scrubber=scrubber,
            page_override=page_override,
            manual_auth_prompt_fn=input_fn,
        ) as stays:
            stays.authenticate_if_needed()
            stays.navigate_to_property_custom_fields(target_url, property_id)
            discovered_fields, existing_stays_values, _ = stays.inspect_and_discover_fields(
                target_url=target_url,
                property_id=property_id,
                save_sanitized_html=False,
            )

            records = mapper.build_execution_records(
                property_id=property_id,
                target_url=target_url,
                claude_response=claude_response,
                existing_stays_values=existing_stays_values,
            )

            # Resolução interativa de conflitos caso existam campos já preenchidos com valor diferente
            records = reviewer.resolve_conflicts_interactively(records)

            # Exibe tabela de prévia e grava estado normalizado inicial
            reviewer.display_preview_table(records)
            artifact_manager.write_normalized_fields(records)
            artifact_manager.write_preview_csv(records)

            # Solicita confirmação antes de preencher os campos na página
            if reviewer.confirm_fill_execution(records):
                records = stays.fill_approved_fields(
                    records,
                    discovered_fields,
                    property_id=property_id,
                    target_url=target_url,
                    already_completed_keys=completed_resume_keys,
                )
            else:
                logger.info("Preenchimento cancelado pelo usuário na etapa de confirmação da prévia.")
                stays.capture_screenshot(artifact_manager.before_fill_screenshot_path)
                stays.capture_screenshot(artifact_manager.after_fill_screenshot_path)
                for rec in records:
                    if rec.result == ExecutionResultStatus.PENDING:
                        rec.result = ExecutionResultStatus.SKIPPED

            report = _build_execution_report(
                property_id=property_id,
                target_url=target_url,
                source_type=source_artifact.source_type,
                source_reference=source_artifact.source_reference,
                started_at=started_at,
                settings=settings,
                catalog=catalog,
                artifact_manager=artifact_manager,
                records=records,
                save_executed=False,
                errors=errors,
            )

            # Mesmo com --save, exige resumo final e confirmação explícita antes de clicar em Salvar
            if settings.save_changes and not settings.dry_run and report.filled_count > 0:
                if reviewer.confirm_final_save(report):
                    save_executed = stays.save_changes_on_page()
                    report.save_executed = save_executed
                else:
                    logger.info("Salvamento final cancelado pelo usuário na confirmação final.")

            report.finished_at = datetime.now(timezone.utc).isoformat()
            artifact_manager.write_normalized_fields(records)
            artifact_manager.write_preview_csv(records)
            artifact_manager.write_execution_report(report)
            reviewer.print_final_summary(report)
            return 0

    except Exception as run_err:
        safe_err = scrubber.redact_secrets(str(run_err))
        logger.error("Erro fatal durante a automação no Stays: %s", safe_err)
        errors.append(safe_err)
        artifact_manager.ensure_screenshot_file(artifact_manager.error_screenshot_path)
        return 1


def _build_execution_report(
    *,
    property_id: str,
    target_url: str,
    source_type: str,
    source_reference: str,
    started_at: str,
    settings: Any,
    catalog: CustomFieldsCatalog,
    artifact_manager: ArtifactManager,
    records: List[ Any],
    save_executed: bool,
    errors: List[str],
) -> ExecutionReport:
    found_count = sum(1 for r in records if r.status == FieldStatus.FOUND)
    missing_count = sum(1 for r in records if r.status == FieldStatus.MISSING)
    conflicting_count = sum(1 for r in records if r.status == FieldStatus.CONFLICTING)
    low_conf_count = sum(1 for r in records if r.action == FieldAction.SKIP_LOW_CONFIDENCE)
    filled_count = sum(
        1
        for r in records
        if r.result in (ExecutionResultStatus.FILLED, ExecutionResultStatus.OVERWRITTEN)
    )
    ignored_count = sum(
        1
        for r in records
        if r.result in (ExecutionResultStatus.SKIPPED, ExecutionResultStatus.KEPT)
        and r.status != FieldStatus.MISSING
    )

    try:
        rel_artifacts = str(artifact_manager.run_dir.relative_to(Path.cwd()))
    except ValueError:
        rel_artifacts = str(artifact_manager.run_dir)

    return ExecutionReport(
        property_id=property_id,
        target_url=target_url,
        source_type=source_type,
        source_reference=source_reference,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        mode="save" if (settings.save_changes and not settings.dry_run) else "dry-run",
        confidence_threshold=settings.confidence_threshold,
        configured_fields_count=catalog.actual_count,
        expected_fields_count=catalog.expected_count,
        count_divergence_warning=catalog.divergence_message,
        found_count=found_count,
        missing_count=missing_count,
        conflicting_count=conflicting_count,
        low_confidence_blocked_count=low_conf_count,
        filled_count=filled_count,
        ignored_count=ignored_count,
        save_executed=save_executed,
        artifacts_dir=rel_artifacts,
        fields=records,
        errors=errors,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Ponto de entrada principal da CLI."""
    parser = build_arg_parser()
    parsed_args = parser.parse_args(argv)
    return execute_automation_flow(parsed_args)


if __name__ == "__main__":
    sys.exit(main())

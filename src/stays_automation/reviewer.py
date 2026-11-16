"""Interface interativa no terminal (Rich) para prévia de dados, conflitos e confirmações."""

from __future__ import annotations

from typing import Callable, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from stays_automation.content_mapper import ContentMapper
from stays_automation.models import (
    ConflictDecision,
    ExecutionReport,
    FieldAction,
    FieldExecutionRecord,
    FieldStatus,
)
from stays_automation.security import SecretScrubber, mask_sensitive_property_content


def summarize_text(text: Optional[str], max_chars: int = 65) -> str:
    """Resume um texto longo em uma única linha para exibição limpa na tabela do terminal."""
    if text is None or not text.strip():
        return "-"
    single_line = " ".join(text.split())
    if len(single_line) <= max_chars:
        return single_line
    return single_line[: max_chars - 3] + "..."


class TerminalReviewer:
    """Exibe tabelas de prévia, resolve conflitos interativamente e solicita confirmações explícitas."""

    def __init__(
        self,
        console: Optional[Console] = None,
        scrubber: Optional[SecretScrubber] = None,
        *,
        mask_phones: bool = False,
        input_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.console = console or Console()
        self.scrubber = scrubber or SecretScrubber()
        self.mask_phones = mask_phones
        self._input_fn = input_fn or input

    def _safe_display(self, text: Optional[str], *, is_sensitive: bool = False) -> str:
        if text is None:
            return "-"
        redacted = self.scrubber.redact_secrets(text)
        masked = mask_sensitive_property_content(
            redacted,
            is_sensitive_field=is_sensitive,
            mask_phones=self.mask_phones,
        )
        return masked or "-"

    def show_divergence_notice(self, message: Optional[str]) -> None:
        """Exibe aviso explícito caso haja divergência entre os 36 campos do texto e 37 reais."""
        if message:
            self.console.print(
                Panel(
                    message,
                    title="Aviso de Auditoria de Configuração (36 vs 37 campos)",
                    border_style="yellow",
                )
            )

    def resolve_conflicts_interactively(
        self,
        records: List[FieldExecutionRecord],
    ) -> List[FieldExecutionRecord]:
        """
        Para cada campo que já possui valor diferente no Stays, exibe o valor atual e o novo
        valor proposto e solicita decisão explícita: manter, substituir ou ignorar.
        """
        conflicting_records = [
            r
            for r in records
            if r.status == FieldStatus.CONFLICTING and r.current_value is not None and r.proposed_value is not None
        ]
        if not conflicting_records:
            return records

        self.console.print(
            f"\n[bold yellow]Foram detectados {len(conflicting_records)} campos com valores já preenchidos e diferentes no Stays.[/bold yellow]"
        )

        for rec in conflicting_records:
            cur_masked = self._safe_display(rec.current_value, is_sensitive=rec.sensitive)
            prop_masked = self._safe_display(rec.proposed_value, is_sensitive=rec.sensitive)

            panel_body = (
                f"[bold]Campo:[/bold] {rec.stays_label} ({rec.field_key})\n"
                f"[bold cyan]Valor atual no Stays:[/bold cyan] {cur_masked}\n"
                f"[bold green]Novo valor proposto:[/bold green] {prop_masked}\n"
                f"[bold]Confiança:[/bold] {rec.confidence:.2f}"
            )
            self.console.print(Panel(panel_body, title="Conflito de Conteúdo Existente", border_style="yellow"))

            while True:
                raw_choice = (
                    self._input_fn(
                        f"Decisão para '{rec.stays_label}' - [m]anter atual / [s]ubstituir / [i]gnorar (padrão=m): "
                    )
                    .strip()
                    .lower()
                )
                if raw_choice in ("", "m", "manter", "keep"):
                    ContentMapper.apply_conflict_decision(rec, ConflictDecision.KEEP)
                    break
                if raw_choice in ("s", "substituir", "replace"):
                    ContentMapper.apply_conflict_decision(rec, ConflictDecision.REPLACE)
                    break
                if raw_choice in ("i", "ignorar", "ignore"):
                    ContentMapper.apply_conflict_decision(rec, ConflictDecision.IGNORE)
                    break
                self.console.print("[red]Opção inválida. Digite 'm' (manter), 's' (substituir) ou 'i' (ignorar).[/red]")

        return records

    def display_preview_table(self, records: List[FieldExecutionRecord]) -> None:
        """
        Apresenta a tabela de prévia contendo:
        campo, valor resumido, status, confiança, se é sensível e se será preenchido.
        """
        table = Table(
            title="Prévia dos Campos Personalizados do Stays",
            show_lines=False,
            header_style="bold cyan",
        )
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Campo (Stays)", style="bold", max_width=36)
        table.add_column("Valor Proposto (Resumido)", max_width=52)
        table.add_column("Status", justify="center")
        table.add_column("Confiança", justify="right")
        table.add_column("Sensível", justify="center")
        table.add_column("Será Preenchido?", justify="center")

        for idx, rec in enumerate(records, start=1):
            safe_val = self._safe_display(rec.proposed_value, is_sensitive=rec.sensitive)
            short_val = summarize_text(safe_val, max_chars=50)
            sens_label = "Sim" if rec.sensitive else "Não"
            if rec.action == FieldAction.FILL:
                fill_label = "Sim (preencher)"
            elif rec.action == FieldAction.OVERWRITE:
                fill_label = "Sim (substituir)"
            else:
                fill_label = f"Não ({rec.action.value})"

            table.add_row(
                str(idx),
                rec.stays_label,
                short_val,
                rec.status.value,
                f"{rec.confidence:.2f}",
                sens_label,
                fill_label,
            )

        self.console.print(table)

    def confirm_fill_execution(self, records: List[FieldExecutionRecord]) -> bool:
        """Solicita confirmação explícita antes de preencher os campos aprovados no navegador."""
        approved_count = sum(1 for r in records if r.will_be_filled)
        if approved_count == 0:
            self.console.print(
                "[yellow]Nenhum campo elegível para preenchimento nesta execução.[/yellow]"
            )
            return False

        answer = (
            self._input_fn(
                f"\nConfirmar o preenchimento de {approved_count} campo(s) na página do Stays? [s/N]: "
            )
            .strip()
            .lower()
        )
        return answer in ("s", "sim", "y", "yes")

    def confirm_final_save(self, report: ExecutionReport) -> bool:
        """
        Exibe resumo final da operação e solicita confirmação explícita antes de clicar no botão
        'Salvar' quando a flag --save estiver ativa.
        """
        summary_box = (
            f"Imóvel: {report.property_id} ({report.target_url})\n"
            f"Campos preenchidos/atualizados no formulário: {report.filled_count}\n"
            f"Campos ignorados/mantidos: {report.ignored_count}\n"
            "ATENÇÃO: Esta ação clicará no botão SALVAR do Stays e gravará as alterações no sistema."
        )
        self.console.print(
            Panel(
                summary_box,
                title="Confirmação Final de Salvamento (--save)",
                border_style="bold red",
            )
        )
        answer = (
            self._input_fn("Deseja efetivamente SALVAR as alterações no Stays agora? [s/N]: ")
            .strip()
            .lower()
        )
        return answer in ("s", "sim", "y", "yes")

    def format_final_summary_text(self, report: ExecutionReport) -> str:
        """Formata o resumo final exatamente no padrão especificado pelo requisito."""
        source_display = {
            "url": "URL",
            "file": "Arquivo de texto",
            "interactive": "Texto interativo",
            "multi": "Múltiplas fontes",
        }.get(report.source_type, report.source_type)

        save_display = "sim" if report.save_executed else "não"
        lines = [
            f"Imóvel: {report.property_id}",
            f"Fonte: {source_display}",
            f"Campos configurados: {report.configured_fields_count}",
            f"Encontrados: {report.found_count}",
            f"Ausentes: {report.missing_count}",
            f"Conflitantes: {report.conflicting_count}",
            f"Bloqueados por baixa confiança: {report.low_confidence_blocked_count}",
            f"Preenchidos: {report.filled_count}",
            f"Ignorados: {report.ignored_count}",
            f"Salvamento realizado: {save_display}",
            f"Modo: {report.mode}",
            "",
            "Artefatos:",
            f"{report.artifacts_dir}/",
        ]
        return "\n".join(lines)

    def print_final_summary(self, report: ExecutionReport) -> str:
        """Imprime o resumo final no console e retorna o texto formatado."""
        summary_text = self.format_final_summary_text(report)
        self.console.print("\n" + summary_text)
        return summary_text

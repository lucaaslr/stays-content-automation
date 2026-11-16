"""Geração e persistência segura de artefatos de execução, CSV, progresso e relatórios."""

from __future__ import annotations

import base64
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from stays_automation.models import (
    ClaudeExtractionResponse,
    ExecutionReport,
    FieldExecutionRecord,
)
from stays_automation.security import (
    SecretScrubber,
    mask_sensitive_property_content,
    sanitize_html_document,
)

CSV_COLUMNS = [
    "property_id",
    "target_url",
    "field_key",
    "stays_label",
    "current_value",
    "proposed_value",
    "status",
    "confidence",
    "sensitive",
    "action",
    "result",
    "warning",
]

# PNG 1x1 transparente válido para fallback em ambientes de teste sem navegador gráfico
PLACEHOLDER_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class ArtifactManager:
    """Gerencia a pasta de artefatos da execução com mascaramento de dados sensíveis e segredos."""

    def __init__(
        self,
        base_output_dir: Path,
        property_id: str,
        scrubber: Optional[SecretScrubber] = None,
        *,
        mask_phones: bool = False,
        resume_dir: Optional[Path] = None,
    ) -> None:
        self.scrubber = scrubber or SecretScrubber()
        self.mask_phones = mask_phones
        self.property_id = property_id.upper()

        if resume_dir is not None:
            self.run_dir = Path(resume_dir).expanduser().resolve()
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder_name = f"{timestamp}_{self.property_id}"
            self.run_dir = Path(base_output_dir).expanduser().resolve() / folder_name

        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def log_file_path(self) -> Path:
        return self.run_dir / "execution.log"

    @property
    def progress_file_path(self) -> Path:
        return self.run_dir / "progress.json"

    @property
    def before_fill_screenshot_path(self) -> Path:
        return self.run_dir / "before_fill.png"

    @property
    def after_fill_screenshot_path(self) -> Path:
        return self.run_dir / "after_fill.png"

    @property
    def error_screenshot_path(self) -> Path:
        return self.run_dir / "error.png"

    def _mask_field_text(
        self,
        text: Optional[str],
        *,
        is_sensitive: bool = False,
    ) -> Optional[str]:
        if text is None:
            return None
        redacted = self.scrubber.redact_secrets(text)
        return mask_sensitive_property_content(
            redacted,
            is_sensitive_field=is_sensitive,
            mask_phones=self.mask_phones,
        )

    def write_input_original(self, raw_input: str, *, preserve_exact: bool = True) -> Path:
        """
        Salva input_original.txt.
        Para texto bruto (--source-file ou interativo), preserva o texto original sem modificá-lo,
        apenas garantindo remoção de eventuais chaves de API/credenciais do .env.
        """
        target = self.run_dir / "input_original.txt"
        safe_content = self.scrubber.redact_secrets(raw_input) if preserve_exact else raw_input
        target.write_text(safe_content, encoding="utf-8")
        return target

    def write_source_extracted(self, extracted_text: str) -> Path:
        """Salva source_extracted.txt com o conteúdo limpo extraído da fonte."""
        target = self.run_dir / "source_extracted.txt"
        safe_content = self.scrubber.redact_secrets(extracted_text)
        target.write_text(safe_content, encoding="utf-8")
        return target

    def write_claude_request_sanitized(self, request_payload: Dict[str, Any]) -> Path:
        """Salva claude_request_sanitized.json sem chaves de API ou credenciais."""
        target = self.run_dir / "claude_request_sanitized.json"
        sanitized = self.scrubber.sanitize_structure(request_payload)
        target.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def write_claude_response(self, response_data: ClaudeExtractionResponse | Dict[str, Any]) -> Path:
        """Salva claude_response.json com mascaramento de campos sensíveis."""
        target = self.run_dir / "claude_response.json"
        if isinstance(response_data, ClaudeExtractionResponse):
            payload = response_data.model_dump(mode="json")
        else:
            payload = self.scrubber.sanitize_structure(response_data)

        for field_item in payload.get("fields", []):
            is_sens = bool(field_item.get("sensitive", False))
            field_item["value"] = self._mask_field_text(
                field_item.get("value"),
                is_sensitive=is_sens,
            )
            for ev in field_item.get("evidence", []):
                ev["excerpt"] = self._mask_field_text(
                    ev.get("excerpt"),
                    is_sensitive=is_sens,
                ) or ""

        sanitized = self.scrubber.sanitize_structure(payload)
        target.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def _sanitize_record_dict(self, record: FieldExecutionRecord) -> Dict[str, Any]:
        data = record.model_dump(mode="json")
        is_sens = record.sensitive
        data["current_value"] = self._mask_field_text(record.current_value, is_sensitive=is_sens)
        data["proposed_value"] = self._mask_field_text(record.proposed_value, is_sensitive=is_sens)
        data["warning"] = self._mask_field_text(record.warning, is_sensitive=False) or ""
        for ev in data.get("evidence", []):
            ev["excerpt"] = self._mask_field_text(ev.get("excerpt"), is_sensitive=is_sens) or ""
        return self.scrubber.sanitize_structure(data)

    def write_normalized_fields(self, records: List[FieldExecutionRecord]) -> Path:
        """Salva normalized_fields.json com todos os 37 campos normalizados e mascarados."""
        target = self.run_dir / "normalized_fields.json"
        sanitized_list = [self._sanitize_record_dict(r) for r in records]
        target.write_text(
            json.dumps(sanitized_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def write_field_mapping(self, mapping_report: Dict[str, Any]) -> Path:
        """Salva field_mapping.json com os seletores descobertos na página do Stays."""
        target = self.run_dir / "field_mapping.json"
        sanitized = self.scrubber.sanitize_structure(mapping_report)
        target.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def write_sanitized_html(self, raw_html: str, filename: str = "sanitized_page.html") -> Path:
        """Salva uma cópia sanitizada do HTML da página do Stays (usado no modo inspect)."""
        target = self.run_dir / filename
        clean_html = sanitize_html_document(raw_html, scrubber=self.scrubber)
        target.write_text(clean_html, encoding="utf-8")
        return target

    def write_preview_csv(self, records: List[FieldExecutionRecord]) -> Path:
        """
        Gera preview.csv contendo exatamente as colunas exigidas:
        property_id,target_url,field_key,stays_label,current_value,proposed_value,status,confidence,sensitive,action,result,warning
        """
        target = self.run_dir / "preview.csv"
        with target.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for rec in records:
                sanitized = self._sanitize_record_dict(rec)
                row = {
                    "property_id": sanitized["property_id"],
                    "target_url": sanitized["target_url"],
                    "field_key": sanitized["field_key"],
                    "stays_label": sanitized["stays_label"],
                    "current_value": sanitized["current_value"] or "",
                    "proposed_value": sanitized["proposed_value"] or "",
                    "status": sanitized["status"],
                    "confidence": f"{rec.confidence:.2f}",
                    "sensitive": str(rec.sensitive).lower(),
                    "action": sanitized["action"],
                    "result": sanitized["result"],
                    "warning": sanitized["warning"] or "",
                }
                writer.writerow(row)
        return target

    def write_execution_report(self, report: ExecutionReport) -> Path:
        """Salva execution_report.json com resumo final e registros mascarados."""
        target = self.run_dir / "execution_report.json"
        payload = report.model_dump(mode="json")
        payload["fields"] = [self._sanitize_record_dict(r) for r in report.fields]
        payload["errors"] = [self.scrubber.redact_secrets(err) for err in report.errors]
        sanitized = self.scrubber.sanitize_structure(payload)
        target.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def save_field_progress(
        self,
        records: List[FieldExecutionRecord],
        *,
        property_id: str,
        target_url: str,
    ) -> Path:
        """Salva o estado de progresso por campo em progress.json para permitir --resume seguro."""
        completed_keys = [
            r.field_key
            for r in records
            if r.result.value in ("filled", "overwritten", "kept")
        ]
        state = {
            "property_id": property_id,
            "target_url": target_url,
            "updated_at": datetime.now().isoformat(),
            "completed_field_keys": completed_keys,
            "records": [self._sanitize_record_dict(r) for r in records],
        }
        self.progress_file_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.progress_file_path

    def load_completed_fields_for_resume(self) -> Set[str]:
        """Carrega o conjunto de field_keys já preenchidos de uma execução anterior (--resume)."""
        if not self.progress_file_path.exists():
            return set()
        try:
            data = json.loads(self.progress_file_path.read_text(encoding="utf-8"))
            return set(data.get("completed_field_keys", []))
        except Exception:
            return set()

    @staticmethod
    def ensure_screenshot_file(path: Path) -> Path:
        """Garante que o arquivo PNG exista (criando placeholder válido caso executado em mock offline)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(PLACEHOLDER_PNG_BYTES)
        return path

"""Modelos de dados validados com Pydantic para configuração, saída do Claude e relatórios."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldStatus(str, Enum):
    """Valores oficiais aceitos para o status de cada campo extraído."""

    FOUND = "found"
    MISSING = "missing"
    CONFLICTING = "conflicting"
    REQUIRES_REVIEW = "requires_review"
    BLOCKED_SENSITIVE = "blocked_sensitive"
    INVALID_FORMAT = "invalid_format"


class FieldType(str, Enum):
    """Tipos de dados aceitos pelos campos personalizados."""

    TEXT = "text"
    URL = "url"


class FieldAction(str, Enum):
    """Ação decidida para o preenchimento de um campo no Stays."""

    FILL = "fill"
    OVERWRITE = "overwrite"
    KEEP_EXISTING = "keep_existing"
    SKIP_EMPTY = "skip_empty"
    SKIP_LOW_CONFIDENCE = "skip_low_confidence"
    SKIP_CONFLICT = "skip_conflict"
    SKIP_DISABLED = "skip_disabled"
    SKIP_INVALID = "skip_invalid"
    BLOCKED_DOT_FIELD = "blocked_dot_field"


class ExecutionResultStatus(str, Enum):
    """Resultado efetivo da operação por campo."""

    PENDING = "pending"
    FILLED = "filled"
    OVERWRITTEN = "overwritten"
    KEPT = "kept"
    SKIPPED = "skipped"
    ERROR = "error"


class ConflictDecision(str, Enum):
    """Decisões possíveis do usuário diante de conflito com valor existente no Stays."""

    KEEP = "keep"
    REPLACE = "replace"
    IGNORE = "ignore"


def validate_http_url(url_value: str, *, allow_localhost: bool = True) -> str:
    """Valida se uma string representa uma URL HTTP/HTTPS bem formada."""
    cleaned = url_value.strip()
    if not cleaned or " " in cleaned:
        raise ValueError(f"URL inválida (contém espaços ou está vazia): {url_value!r}")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"URL deve iniciar com http:// ou https:// e conter domínio válido: {url_value!r}")
    host = parsed.netloc.split(":")[0].lower()
    if allow_localhost and host in ("localhost", "127.0.0.1", "::1"):
        return cleaned
    if "." not in parsed.netloc:
        raise ValueError(f"URL deve iniciar com http:// ou https:// e conter domínio válido: {url_value!r}")
    return cleaned


class CustomFieldConfig(BaseModel):
    """Configuração individual de um campo personalizado do Stays."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(..., min_length=1)
    stays_label: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    instructions: str = Field(default="")
    aliases: List[str] = Field(default_factory=list)
    field_type: FieldType = Field(default=FieldType.TEXT)
    sensitive: bool = Field(default=False)
    required: bool = Field(default=False)
    max_length: int = Field(default=2000, ge=1, le=20000)
    enabled: bool = Field(default=True)
    allow_auto_fill: bool = Field(default=True)


class CustomFieldsCatalog(BaseModel):
    """Catálogo completo de campos personalizados carregado de config/custom_fields.yaml."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1.0")
    expected_count: int = Field(default=36)
    divergence_note: str = Field(default="")
    fields: List[CustomFieldConfig]

    @property
    def actual_count(self) -> int:
        return len(self.fields)

    @property
    def has_count_divergence(self) -> bool:
        return self.actual_count != self.expected_count

    @property
    def divergence_message(self) -> Optional[str]:
        if not self.has_count_divergence:
            return None
        return (
            f"DIVERGÊNCIA DETECTADA: O requisito menciona {self.expected_count} campos, "
            f"mas a especificação oficial e a configuração contêm {self.actual_count} entradas "
            f"(incluindo o campo '.'). Nenhum campo foi excluído silenciosamente."
        )

    def get_by_key(self, field_key: str) -> Optional[CustomFieldConfig]:
        for field in self.fields:
            if field.field_key == field_key:
                return field
        return None

    @classmethod
    def from_yaml(cls, path: Path) -> "CustomFieldsCatalog":
        with path.open("r", encoding="utf-8") as handle:
            raw_data = yaml.safe_load(handle)
        return cls.model_validate(raw_data)


class SelectorsConfig(BaseModel):
    """Configuração de seletores e fallbacks do Stays carregada de config/selectors.yaml."""

    schema_version: str = Field(default="1.0")
    login: Dict[str, List[str]] = Field(default_factory=dict)
    navigation: Dict[str, List[str]] = Field(default_factory=dict)
    actions: Dict[str, List[str]] = Field(default_factory=dict)
    field_overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "SelectorsConfig":
        with path.open("r", encoding="utf-8") as handle:
            raw_data = yaml.safe_load(handle) or {}
        return cls.model_validate(raw_data)


class EvidenceItem(BaseModel):
    """Trecho comprobatório utilizado pelo Claude para sustentar o valor extraído."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1)
    excerpt: str = Field(..., min_length=1)


class FieldExtractionResult(BaseModel):
    """Estrutura de saída do Claude para um campo personalizado específico."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(..., min_length=1)
    stays_label: str = Field(..., min_length=1)
    value: Optional[str] = Field(default=None)
    status: FieldStatus
    confidence: float = Field(..., ge=0.0, le=1.0)
    sensitive: bool = Field(default=False)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @field_validator("value")
    @classmethod
    def normalize_empty_string_to_none(cls, val: Optional[str]) -> Optional[str]:
        if val is not None:
            stripped = val.strip()
            if not stripped:
                return None
            return stripped
        return None


class PropertyMetadata(BaseModel):
    """Metadados do imóvel e da fonte de informação processada."""

    model_config = ConfigDict(extra="forbid")

    target_url: str = Field(..., min_length=1)
    property_id: str = Field(..., min_length=1)
    source_type: Literal["url", "file", "interactive", "multi"]
    source_reference: str = Field(..., min_length=1)

    @field_validator("target_url")
    @classmethod
    def check_target_url(cls, val: str) -> str:
        return validate_http_url(val)


class ExtractionSummary(BaseModel):
    """Resumo estatístico da extração estruturada retornada pelo Claude."""

    model_config = ConfigDict(extra="forbid")

    total_fields: int = Field(..., ge=0)
    found: int = Field(..., ge=0)
    missing: int = Field(..., ge=0)
    conflicting: int = Field(..., ge=0)
    requires_review: int = Field(..., ge=0)
    blocked_sensitive: int = Field(default=0, ge=0)
    invalid_format: int = Field(default=0, ge=0)


class ClaudeExtractionResponse(BaseModel):
    """Resposta JSON completa retornada pelo Claude e validada com Pydantic."""

    model_config = ConfigDict(extra="forbid")

    property: PropertyMetadata
    fields: List[FieldExtractionResult]
    summary: ExtractionSummary

    @model_validator(mode="after")
    def verify_non_empty_fields(self) -> "ClaudeExtractionResponse":
        if not self.fields:
            raise ValueError("A lista 'fields' na resposta do Claude não pode estar vazia.")
        return self


class SourceExtractionArtifact(BaseModel):
    """Resultado da etapa de extração de conteúdo da fonte (URL pública ou texto bruto)."""

    source_type: Literal["url", "file", "interactive", "multi"]
    source_reference: str
    raw_input_text: str
    extracted_text: str
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    used_playwright: bool = False
    title: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DiscoveredFieldSelector(BaseModel):
    """Mapeamento descoberto para um campo personalizado na interface do Stays."""

    field_key: Optional[str] = None
    stays_label: str
    normalized_label: str
    element_tag: str
    element_id: Optional[str] = None
    element_name: Optional[str] = None
    data_testid: Optional[str] = None
    recommended_selector: str
    fallback_selectors: List[str] = Field(default_factory=list)
    strategy_used: str
    current_value_preview: Optional[str] = None


class FieldExecutionRecord(BaseModel):
    """Registro consolidado por campo para prévia, CSV, progresso e relatório final."""

    property_id: str
    target_url: str
    field_key: str
    stays_label: str
    current_value: Optional[str] = None
    proposed_value: Optional[str] = None
    status: FieldStatus
    confidence: float = Field(ge=0.0, le=1.0)
    sensitive: bool = False
    action: FieldAction
    result: ExecutionResultStatus = ExecutionResultStatus.PENDING
    warning: str = ""
    evidence: List[EvidenceItem] = Field(default_factory=list)
    selector_used: Optional[str] = None

    @property
    def will_be_filled(self) -> bool:
        return self.action in (FieldAction.FILL, FieldAction.OVERWRITE)


class ExecutionReport(BaseModel):
    """Relatório final auditável da execução salvo em execution_report.json."""

    property_id: str
    target_url: str
    source_type: str
    source_reference: str
    started_at: str
    finished_at: Optional[str] = None
    mode: Literal["dry-run", "save"]
    confidence_threshold: float
    configured_fields_count: int
    expected_fields_count: int
    count_divergence_warning: Optional[str] = None
    found_count: int = 0
    missing_count: int = 0
    conflicting_count: int = 0
    low_confidence_blocked_count: int = 0
    filled_count: int = 0
    ignored_count: int = 0
    save_executed: bool = False
    artifacts_dir: str
    fields: List[FieldExecutionRecord] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

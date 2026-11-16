"""Normalização de rótulos, validação de regras de negócio e resolução de conflitos."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional

from stays_automation.models import (
    ClaudeExtractionResponse,
    ConflictDecision,
    CustomFieldConfig,
    CustomFieldsCatalog,
    ExecutionResultStatus,
    FieldAction,
    FieldExecutionRecord,
    FieldExtractionResult,
    FieldStatus,
    FieldType,
    validate_http_url,
)
from stays_automation.security import ACCESS_CODE_PATTERN, WIFI_PASSWORD_PATTERN

URL_EXTRACTION_REGEX = re.compile(r"(https?://[^\s<>\"')]+)")


def normalize_label(label: str) -> str:
    """
    Normaliza um rótulo para comparação resiliente a acentos, caixa e espaços extras.
    Preserva o rótulo especial '.' intacto.
    """
    if label is None:
        return ""
    stripped = label.strip()
    if stripped == ".":
        return "."
    decomposed = unicodedata.normalize("NFKD", stripped)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.category(ch).startswith("M"))
    lowered = without_accents.lower()
    # Mantém letras, dígitos e espaços simples
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return re.sub(r"\s+", " ", cleaned)


def sanitize_url_only_value(raw_value: Optional[str]) -> Optional[str]:
    """Extrai e valida uma URL limpa para campos que exigem exclusivamente links."""
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if not stripped:
        return None

    # Se o valor inteiro já for uma URL válida, retorna-o diretamente
    try:
        return validate_http_url(stripped)
    except ValueError:
        pass

    # Caso haja texto ao redor de uma única URL válida, extrai apenas a URL se for inequívoca
    matches = URL_EXTRACTION_REGEX.findall(stripped)
    if len(matches) == 1:
        candidate = matches[0].rstrip(".,;)")
        return validate_http_url(candidate)

    raise ValueError(f"Valor não contém uma URL única válida: {raw_value!r}")


def contains_sensitive_patterns(text: Optional[str]) -> bool:
    """Detecta se um texto contém padrões de senha de Wi-Fi ou código de fechadura/cofre."""
    if not text:
        return False
    return bool(WIFI_PASSWORD_PATTERN.search(text) or ACCESS_CODE_PATTERN.search(text))


class ContentMapper:
    """Combina o catálogo de 37 campos, a resposta do Claude e os valores atuais da página no Stays."""

    def __init__(
        self,
        catalog: CustomFieldsCatalog,
        *,
        confidence_threshold: float = 0.80,
        skip_empty: bool = True,
    ) -> None:
        self.catalog = catalog
        self.confidence_threshold = confidence_threshold
        self.skip_empty = skip_empty
        self._label_lookup: Dict[str, CustomFieldConfig] = {}
        for cfg in self.catalog.fields:
            self._label_lookup[normalize_label(cfg.stays_label)] = cfg
            for alias in cfg.aliases:
                self._label_lookup[normalize_label(alias)] = cfg

    def match_field_config_by_label(self, visible_label: str) -> Optional[CustomFieldConfig]:
        """Localiza a configuração do campo a partir do texto visível do rótulo na página do Stays."""
        norm = normalize_label(visible_label)
        if not norm:
            return None
        if norm in self._label_lookup:
            return self._label_lookup[norm]

        # Fallback por prefixo caso o rótulo no Stays inclua sufixo como "(Opcional)" ou "*"
        for candidate_norm, cfg in self._label_lookup.items():
            if candidate_norm != "." and len(candidate_norm) >= 6:
                if norm == candidate_norm or norm.startswith(candidate_norm + " "):
                    return cfg
        return None

    def _find_extracted_field(
        self,
        field_cfg: CustomFieldConfig,
        claude_fields: List[FieldExtractionResult],
    ) -> FieldExtractionResult:
        for item in claude_fields:
            if item.field_key == field_cfg.field_key:
                return item
        target_norm = normalize_label(field_cfg.stays_label)
        for item in claude_fields:
            if normalize_label(item.stays_label) == target_norm:
                return item
        return FieldExtractionResult(
            field_key=field_cfg.field_key,
            stays_label=field_cfg.stays_label,
            value=None,
            status=FieldStatus.MISSING,
            confidence=0.0,
            sensitive=field_cfg.sensitive,
            evidence=[],
            warnings=["Campo não retornado explicitamente pela LLM; registrado como ausente."],
        )

    def build_execution_records(
        self,
        *,
        property_id: str,
        target_url: str,
        claude_response: ClaudeExtractionResponse,
        existing_stays_values: Optional[Dict[str, str]] = None,
    ) -> List[FieldExecutionRecord]:
        """
        Gera a lista normalizada e validada para todos os 37 campos configurados,
        comparando os valores propostos com os valores já existentes no Stays.
        """
        existing_map = existing_stays_values or {}
        records: List[FieldExecutionRecord] = []

        for field_cfg in self.catalog.fields:
            extracted = self._find_extracted_field(field_cfg, claude_response.fields)
            current_val_raw = existing_map.get(field_cfg.field_key)
            current_val = current_val_raw.strip() if current_val_raw and current_val_raw.strip() else None

            proposed_val = extracted.value
            status = extracted.status
            confidence = extracted.confidence
            warnings = list(extracted.warnings)
            is_sensitive = (
                field_cfg.sensitive
                or extracted.sensitive
                or contains_sensitive_patterns(proposed_val)
            )

            # 1. Regra especial para o campo "." ou campos com allow_auto_fill=False
            if field_cfg.stays_label.strip() == "." or not field_cfg.allow_auto_fill:
                if proposed_val is not None:
                    warnings.append(
                        f"Preenchimento automático bloqueado para o campo '{field_cfg.stays_label}' por regra de segurança."
                    )
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=None,
                        status=FieldStatus.MISSING if proposed_val is None else status,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=FieldAction.BLOCKED_DOT_FIELD,
                        result=ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 2. Campo desativado na configuração YAML
            if not field_cfg.enabled:
                warnings.append("Campo desativado em config/custom_fields.yaml.")
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=proposed_val,
                        status=status,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=FieldAction.SKIP_DISABLED,
                        result=ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 3. Valor ausente na fonte
            if proposed_val is None or status == FieldStatus.MISSING:
                if field_cfg.required:
                    warnings.append("Campo obrigatório sem informação encontrada na fonte.")
                action = FieldAction.KEEP_EXISTING if current_val else FieldAction.SKIP_EMPTY
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=None,
                        status=FieldStatus.MISSING,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=action,
                        result=ExecutionResultStatus.KEPT if current_val else ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 4. Validação de formato para campos exclusivos de URL
            if field_cfg.field_type == FieldType.URL:
                try:
                    proposed_val = sanitize_url_only_value(proposed_val)
                except ValueError as url_err:
                    status = FieldStatus.INVALID_FORMAT
                    warnings.append(str(url_err))
                    records.append(
                        FieldExecutionRecord(
                            property_id=property_id,
                            target_url=target_url,
                            field_key=field_cfg.field_key,
                            stays_label=field_cfg.stays_label,
                            current_value=current_val,
                            proposed_value=proposed_val,
                            status=status,
                            confidence=confidence,
                            sensitive=is_sensitive,
                            action=FieldAction.SKIP_INVALID,
                            result=ExecutionResultStatus.SKIPPED,
                            warning="; ".join(warnings),
                            evidence=extracted.evidence,
                        )
                    )
                    continue

            # 5. Validação de limite máximo de caracteres
            if proposed_val is not None and len(proposed_val) > field_cfg.max_length:
                status = FieldStatus.INVALID_FORMAT
                warnings.append(
                    f"Texto proposto ({len(proposed_val)} caracteres) excede o limite configurado de {field_cfg.max_length} caracteres."
                )
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=proposed_val,
                        status=status,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=FieldAction.SKIP_INVALID,
                        result=ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 6. Status conflitante na própria fonte, bloqueio sensível ou revisão humana
            if status in (
                FieldStatus.CONFLICTING,
                FieldStatus.BLOCKED_SENSITIVE,
                FieldStatus.INVALID_FORMAT,
            ):
                action = (
                    FieldAction.SKIP_CONFLICT
                    if status == FieldStatus.CONFLICTING
                    else FieldAction.SKIP_INVALID
                )
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=proposed_val,
                        status=status,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=action,
                        result=ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 7. Verificação do limiar de confiança (--confidence-threshold)
            if confidence < self.confidence_threshold or status == FieldStatus.REQUIRES_REVIEW:
                warnings.append(
                    f"Bloqueado para preenchimento automático (confiança={confidence:.2f}, limiar={self.confidence_threshold:.2f}, status={status.value})."
                )
                records.append(
                    FieldExecutionRecord(
                        property_id=property_id,
                        target_url=target_url,
                        field_key=field_cfg.field_key,
                        stays_label=field_cfg.stays_label,
                        current_value=current_val,
                        proposed_value=proposed_val,
                        status=status,
                        confidence=confidence,
                        sensitive=is_sensitive,
                        action=FieldAction.SKIP_LOW_CONFIDENCE,
                        result=ExecutionResultStatus.SKIPPED,
                        warning="; ".join(warnings),
                        evidence=extracted.evidence,
                    )
                )
                continue

            # 8. Comparação com valor existente na página do Stays (idempotência e detecção de conflito)
            if current_val is not None:
                if current_val == proposed_val:
                    warnings.append("Valor atual no Stays já é idêntico ao valor proposto.")
                    records.append(
                        FieldExecutionRecord(
                            property_id=property_id,
                            target_url=target_url,
                            field_key=field_cfg.field_key,
                            stays_label=field_cfg.stays_label,
                            current_value=current_val,
                            proposed_value=proposed_val,
                            status=status,
                            confidence=confidence,
                            sensitive=is_sensitive,
                            action=FieldAction.KEEP_EXISTING,
                            result=ExecutionResultStatus.KEPT,
                            warning="; ".join(warnings),
                            evidence=extracted.evidence,
                        )
                    )
                else:
                    warnings.append(
                        "Conflito detectado: o campo no Stays já possui valor diferente do proposto."
                    )
                    records.append(
                        FieldExecutionRecord(
                            property_id=property_id,
                            target_url=target_url,
                            field_key=field_cfg.field_key,
                            stays_label=field_cfg.stays_label,
                            current_value=current_val,
                            proposed_value=proposed_val,
                            status=FieldStatus.CONFLICTING,
                            confidence=confidence,
                            sensitive=is_sensitive,
                            action=FieldAction.SKIP_CONFLICT,
                            result=ExecutionResultStatus.PENDING,
                            warning="; ".join(warnings),
                            evidence=extracted.evidence,
                        )
                    )
                continue

            # 9. Campo vazio no Stays, encontrado com alta confiança e validado -> pronto para preencher
            records.append(
                FieldExecutionRecord(
                    property_id=property_id,
                    target_url=target_url,
                    field_key=field_cfg.field_key,
                    stays_label=field_cfg.stays_label,
                    current_value=None,
                    proposed_value=proposed_val,
                    status=status,
                    confidence=confidence,
                    sensitive=is_sensitive,
                    action=FieldAction.FILL,
                    result=ExecutionResultStatus.PENDING,
                    warning="; ".join(warnings),
                    evidence=extracted.evidence,
                )
            )

        return records

    @staticmethod
    def apply_conflict_decision(
        record: FieldExecutionRecord,
        decision: ConflictDecision,
    ) -> FieldExecutionRecord:
        """Atualiza a ação do registro conforme decisão explícita do usuário para um conflito."""
        if decision == ConflictDecision.REPLACE:
            record.action = FieldAction.OVERWRITE
            record.result = ExecutionResultStatus.PENDING
        elif decision == ConflictDecision.KEEP:
            record.action = FieldAction.KEEP_EXISTING
            record.result = ExecutionResultStatus.KEPT
        else:
            record.action = FieldAction.SKIP_CONFLICT
            record.result = ExecutionResultStatus.SKIPPED
        return record

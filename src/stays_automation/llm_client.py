"""Módulo de integração unificada com Provedores de LLM: Ollama (local) e Anthropic Claude (API)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import httpx
from pydantic import ValidationError

from stays_automation.models import (
    ClaudeExtractionResponse,
    CustomFieldsCatalog,
    FieldStatus,
    SourceExtractionArtifact,
)
from stays_automation.formatting import load_style_guide
from stays_automation.security import SecretScrubber

if TYPE_CHECKING:
    from stays_automation.settings import AppSettings

logger = logging.getLogger("stays_automation")

SYSTEM_PROMPT = """Você é um especialista sênior em operações de hospitalidade e documentação técnica de imóveis de temporada no Brasil.
Sua única função é organizar e redigir as informações estritamente presentes no conteúdo-fonte fornecido, mapeando-as para os campos personalizados do sistema Stays.

REGRAS OBRIGATÓRIAS E INEGOCIÁVEIS:
1. NÃO INVENTE INFORMAÇÕES em nenhuma hipótese.
2. NÃO INFIRA endereço, senha, telefone, link, horário ou política sem evidência explícita na fonte.
3. NÃO PESQUISE nem adicione dados pessoais externos.
4. NÃO COMPLETE campos ausentes com informações "prováveis" ou genéricas.
5. Quando uma informação não existir na fonte, retorne obrigatoriamente `"value": null`, `"status": "missing"` e `"confidence": 0.0`.
6. Diferencie claramente os estados de cada campo no atributo `"status"`:
   - `"found"`: informação explícita e sustentada por trecho real da fonte.
   - `"requires_review"`: informação inferida com baixo risco operacional ou telefone/emergência que exige revisão humana.
   - `"conflicting"`: a própria fonte apresenta valores contraditórios (ex.: dois horários de check-out diferentes). NUNCA escolha silenciosamente; descreva a divergência em `"warnings"`.
   - `"missing"`: informação ausente na fonte.
   - `"blocked_sensitive"`: dado sensível que viola política explícita de exposição.
   - `"invalid_format"`: a fonte menciona o item, mas o formato é inválido (ex.: campo exige URL e a fonte só traz texto sem link).
7. Informações sensíveis (senhas de Wi-Fi, códigos de cofre, senhas de fechadura eletrônica, links diretos de WhatsApp pessoal) devem ter `"sensitive": true`.
8. Todo o conteúdo de `"value"` e `"warnings"` deve ser escrito em português do Brasil (pt-BR), com linguagem clara, objetiva e adequada para hóspedes.
9. Evite linguagem promocional ou adjetivos publicitários nos campos operacionais.
10. Não repita o mesmo texto em vários campos, exceto quando estritamente necessário para o contexto operacional daquele campo.
11. Nos campos configurados com `"field_type": "url"`, devolva SOMENTE a URL limpa (iniciando com http:// ou https://), sem nenhum texto adicional antes ou depois.
12. NUNCA invente telefones de emergência locais (hospitais, polícia, bombeiros, clínicas). Só preencha se constarem na fonte; se houver dúvida sobre verificação local, marque `"status": "requires_review"`.
13. Preserve fielmente nomes próprios, números, horários, quantidades, voltagem, valores de taxas e regras existentes na fonte.
14. Para o campo cujo rótulo no Stays é `"."` (`field_key: "dot_placeholder"`), NUNCA presuma finalidade nem preencha automaticamente; retorne `"value": null` e `"status": "missing"`.
15. Retorne EXCLUSIVAMENTE um objeto JSON válido seguindo exatamente o schema solicitado, sem blocos markdown adicionais fora do JSON."""


def extract_json_object_from_text(raw_text: str) -> Dict[str, Any]:
    """Extrai e faz o parse de um objeto JSON a partir do texto retornado pelo modelo."""
    if not raw_text or not raw_text.strip():
        raise ValueError("Resposta vazia recebida do modelo LLM.")

    cleaned = raw_text.strip()
    # Remove cercas markdown ```json ... ``` caso o modelo as inclua
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            cleaned = cleaned[first_brace : last_brace + 1]

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"A resposta da LLM não é um JSON válido: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("O JSON retornado pela LLM deve ser um objeto na raiz.")
    return parsed


def normalize_extraction_response(
    raw_json_dict: Dict[str, Any],
    *,
    catalog: CustomFieldsCatalog,
    target_url: str,
    property_id: str,
    source_artifact: SourceExtractionArtifact,
) -> Dict[str, Any]:
    """
    Normaliza a saída JSON de LLMs (especialmente modelos locais menores que podem
    omitir campos ausentes, resumo estatístico ou metadados da propriedade) garantindo
    que ela esteja 100% em conformidade com o schema estrito ClaudeExtractionResponse.
    """
    prop_in = raw_json_dict.get("property")
    if not isinstance(prop_in, dict):
        prop_in = {}

    clean_property = {
        "target_url": prop_in.get("target_url") or target_url,
        "property_id": prop_in.get("property_id") or property_id,
        "source_type": prop_in.get("source_type") or source_artifact.source_type,
        "source_reference": prop_in.get("source_reference") or source_artifact.source_reference,
    }

    raw_fields = raw_json_dict.get("fields")
    if not isinstance(raw_fields, list):
        raw_fields = []

    returned_by_key: Dict[str, Dict[str, Any]] = {}
    returned_by_label: Dict[str, Dict[str, Any]] = {}
    valid_status_values = {s.value for s in FieldStatus}

    for item in raw_fields:
        if isinstance(item, dict):
            key = item.get("field_key")
            if key and isinstance(key, str):
                returned_by_key[key.strip().lower()] = item
            label = item.get("stays_label")
            if label and isinstance(label, str):
                returned_by_label[label.strip().lower()] = item

    normalized_fields: List[Dict[str, Any]] = []

    for cat_field in catalog.fields:
        f_key = cat_field.field_key
        raw_item = returned_by_key.get(f_key.lower()) or returned_by_label.get(cat_field.stays_label.lower())
        if raw_item is None:
            for alias in cat_field.aliases:
                if alias.lower() in returned_by_label:
                    raw_item = returned_by_label[alias.lower()]
                    break

        # Regra de segurança inegociável para o campo "." (dot_placeholder)
        if f_key == "dot_placeholder":
            normalized_fields.append(
                {
                    "field_key": f_key,
                    "stays_label": cat_field.stays_label,
                    "value": None,
                    "status": FieldStatus.MISSING.value,
                    "confidence": 0.0,
                    "sensitive": False,
                    "evidence": [],
                    "warnings": ["Campo reservado '.' bloqueado para preenchimento automático."],
                }
            )
            continue

        if raw_item is not None:
            raw_val = raw_item.get("value")
            if raw_val is not None:
                raw_val = str(raw_val).strip()
                if not raw_val or raw_val.lower() in ("null", "none", "n/a", "não informado"):
                    raw_val = None

            raw_status = str(raw_item.get("status", "")).strip().lower()
            if raw_status not in valid_status_values:
                raw_status = FieldStatus.FOUND.value if raw_val else FieldStatus.MISSING.value

            try:
                raw_conf = float(raw_item.get("confidence", 0.0))
                raw_conf = max(0.0, min(raw_conf, 1.0))
            except (ValueError, TypeError):
                raw_conf = 0.90 if raw_status == FieldStatus.FOUND.value else 0.0

            raw_ev = raw_item.get("evidence")
            evidence_list: List[Dict[str, str]] = []
            if isinstance(raw_ev, list):
                for ev in raw_ev:
                    if isinstance(ev, dict) and "source" in ev and "excerpt" in ev:
                        evidence_list.append(
                            {
                                "source": str(ev["source"]),
                                "excerpt": str(ev["excerpt"]),
                            }
                        )
            if not evidence_list and raw_status == FieldStatus.FOUND.value and raw_val:
                evidence_list.append(
                    {
                        "source": f"source_{clean_property['source_type']}",
                        "excerpt": str(raw_val)[:120],
                    }
                )

            raw_warn = raw_item.get("warnings")
            warnings_list: List[str] = []
            if isinstance(raw_warn, list):
                warnings_list = [str(w) for w in raw_warn if w]

            is_sensitive = bool(raw_item.get("sensitive", cat_field.sensitive))

            normalized_fields.append(
                {
                    "field_key": f_key,
                    "stays_label": cat_field.stays_label,
                    "value": raw_val,
                    "status": raw_status,
                    "confidence": raw_conf,
                    "sensitive": is_sensitive,
                    "evidence": evidence_list,
                    "warnings": warnings_list,
                }
            )
        else:
            # Campo ausente na resposta do modelo (omitido ou não encontrado)
            normalized_fields.append(
                {
                    "field_key": f_key,
                    "stays_label": cat_field.stays_label,
                    "value": None,
                    "status": FieldStatus.MISSING.value,
                    "confidence": 0.0,
                    "sensitive": cat_field.sensitive,
                    "evidence": [],
                    "warnings": [],
                }
            )

    # 3. Sumário Estatístico
    found_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.FOUND.value)
    missing_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.MISSING.value)
    conflicting_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.CONFLICTING.value)
    requires_review_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.REQUIRES_REVIEW.value)
    blocked_sensitive_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.BLOCKED_SENSITIVE.value)
    invalid_format_count = sum(1 for f in normalized_fields if f["status"] == FieldStatus.INVALID_FORMAT.value)

    summary = {
        "total_fields": len(normalized_fields),
        "found": found_count,
        "missing": missing_count,
        "conflicting": conflicting_count,
        "requires_review": requires_review_count,
        "blocked_sensitive": blocked_sensitive_count,
        "invalid_format": invalid_format_count,
    }

    return {
        "property": clean_property,
        "fields": normalized_fields,
        "summary": summary,
    }


class BaseLLMClient(ABC):
    """Classe base para clientes de LLM (Claude API e Ollama local)."""

    def __init__(
        self,
        *,
        model: str,
        scrubber: Optional[SecretScrubber] = None,
        max_retries: int = 2,
        timeout_seconds: float = 90.0,
        max_tokens: int = 8192,
    ) -> None:
        self.model = model
        self.scrubber = scrubber or SecretScrubber()
        self.max_retries = max(0, min(max_retries, 3))
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    def build_user_prompt(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> str:
        """Constrói o prompt estruturado com as especificações dos campos e o texto da fonte."""
        style_guide = load_style_guide()
        fields_spec = []
        for f in catalog.fields:
            fields_spec.append(
                {
                    "field_key": f.field_key,
                    "stays_label": f.stays_label,
                    "description": f.description,
                    "instructions": f.instructions,
                    "field_type": f.field_type.value,
                    "sensitive": f.sensitive,
                    "required": f.required,
                    "max_length": f.max_length,
                    "enabled": f.enabled,
                    "allow_auto_fill": f.allow_auto_fill,
                    "style": style_guide.format_for(f.field_key),
                }
            )

        schema_example = {
            "property": {
                "target_url": target_url,
                "property_id": property_id,
                "source_type": source_artifact.source_type,
                "source_reference": source_artifact.source_reference,
            },
            "fields": [
                {
                    "field_key": "property_address",
                    "stays_label": "Endereço da Propriedade",
                    "value": "Endereço encontrado na fonte ou null",
                    "status": "found",
                    "confidence": 0.98,
                    "sensitive": False,
                    "evidence": [
                        {
                            "source": f"source_{source_artifact.source_type}",
                            "excerpt": "Trecho curto literal da fonte que sustenta o valor",
                        }
                    ],
                    "warnings": [],
                }
            ],
            "summary": {
                "total_fields": catalog.actual_count,
                "found": 0,
                "missing": 0,
                "conflicting": 0,
                "requires_review": 0,
                "blocked_sensitive": 0,
                "invalid_format": 0,
            },
        }

        sanitized_source_text = self.scrubber.redact_secrets(source_artifact.extracted_text)

        prompt = (
            "Analise o CONTEÚDO DA FONTE abaixo e preencha a estrutura JSON para todos os "
            f"{catalog.actual_count} campos personalizados configurados.\n\n"
            "METADADOS DO IMÓVEL:\n"
            f"- target_url: {target_url}\n"
            f"- property_id: {property_id}\n"
            f"- source_type: {source_artifact.source_type}\n"
            f"- source_reference: {source_artifact.source_reference}\n\n"
            "ESPECIFICAÇÃO DOS CAMPOS PERSONALIZADOS (JSON):\n"
            f"{json.dumps(fields_spec, ensure_ascii=False, indent=2)}\n\n"
            "FORMATO EXATO DE SAÍDA ESPERADO (JSON):\n"
            f"{json.dumps(schema_example, ensure_ascii=False, indent=2)}\n\n"
            f"{style_guide.as_prompt_section()}\n"
            "CONTEÚDO DA FONTE:\n"
            "=== INÍCIO DA FONTE ===\n"
            f"{sanitized_source_text}\n"
            "=== FIM DA FONTE ===\n"
        )
        return self.scrubber.redact_secrets(prompt)

    @abstractmethod
    def build_sanitized_request_payload(self, *, user_prompt: str) -> Dict[str, Any]:
        """Gera o payload sanitizado da requisição para salvar como artefato JSON."""
        pass

    @abstractmethod
    def classify_and_structure(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], ClaudeExtractionResponse]:
        """Executa a chamada e retorna (sanitized_request, raw_response, validated_response)."""
        pass


class OllamaClient(BaseLLMClient):
    """Cliente de integração com modelos locais via Ollama HTTP API."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "llama3:latest",
        scrubber: Optional[SecretScrubber] = None,
        max_retries: int = 2,
        timeout_seconds: float = 600.0,
        max_tokens: int = 8192,
        num_ctx: int = 16384,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            model=model,
            scrubber=scrubber,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self._http_client = http_client

    def build_user_prompt(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> str:
        """Prompt otimizado para modelos locais, permitindo omissão de campos ausentes para maior velocidade."""
        style_guide = load_style_guide()
        fields_spec = []
        for f in catalog.fields:
            fields_spec.append(
                {
                    "field_key": f.field_key,
                    "stays_label": f.stays_label,
                    "description": f.description,
                    "instructions": f.instructions,
                    "field_type": f.field_type.value,
                    "sensitive": f.sensitive,
                    "style": style_guide.format_for(f.field_key),
                }
            )

        schema_example = {
            "property": {
                "target_url": target_url,
                "property_id": property_id,
                "source_type": source_artifact.source_type,
                "source_reference": source_artifact.source_reference,
            },
            "fields": [
                {
                    "field_key": "property_address",
                    "stays_label": "Endereço da Propriedade",
                    "value": "Endereço encontrado na fonte",
                    "status": "found",
                    "confidence": 0.98,
                    "sensitive": False,
                    "evidence": [
                        {
                            "source": f"source_{source_artifact.source_type}",
                            "excerpt": "Trecho curto literal da fonte",
                        }
                    ],
                    "warnings": [],
                }
            ],
        }

        sanitized_source_text = self.scrubber.redact_secrets(source_artifact.extracted_text)

        prompt = (
            "Analise o CONTEÚDO DA FONTE abaixo e extraia as informações dos campos do sistema Stays.\n\n"
            "METADADOS DO IMÓVEL:\n"
            f"- target_url: {target_url}\n"
            f"- property_id: {property_id}\n"
            f"- source_type: {source_artifact.source_type}\n"
            f"- source_reference: {source_artifact.source_reference}\n\n"
            "ESPECIFICAÇÃO DOS CAMPOS:\n"
            f"{json.dumps(fields_spec, ensure_ascii=False)}\n\n"
            "FORMATO DE SAÍDA ESPERADO (JSON):\n"
            f"{json.dumps(schema_example, ensure_ascii=False, indent=2)}\n\n"
            "ORIENTAÇÃO: Inclua no array 'fields' os campos encontrados na fonte (status: 'found', 'requires_review' ou 'conflicting'). "
            "Campos ausentes na fonte podem ser omitidos da lista.\n\n"
            f"{style_guide.as_prompt_section()}\n"
            "CONTEÚDO DA FONTE:\n"
            "=== INÍCIO DA FONTE ===\n"
            f"{sanitized_source_text}\n"
            "=== FIM DA FONTE ===\n"
        )
        return self.scrubber.redact_secrets(prompt)

    def build_sanitized_request_payload(self, *, user_prompt: str) -> Dict[str, Any]:
        """Gera o payload sanitizado da requisição ao Ollama."""
        payload = {
            "model": self.model,
            "format": "json",
            "options": {
                "temperature": 0.0,
                "num_predict": self.max_tokens,
                "num_ctx": self.num_ctx,
            },
            "messages": [
                {"role": "system", "content": self.scrubber.redact_secrets(SYSTEM_PROMPT)},
                {"role": "user", "content": self.scrubber.redact_secrets(user_prompt)},
            ],
        }
        return self.scrubber.sanitize_structure(payload)

    def _invoke_ollama_api(self, user_prompt: str) -> str:
        """Executa a chamada HTTP à API do Ollama (/api/chat)."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0,
                "num_predict": self.max_tokens,
                "num_ctx": self.num_ctx,
            },
        }

        if self._http_client is not None:
            resp = self._http_client.post(url, json=payload, timeout=self.timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "").strip()

        with httpx.Client(timeout=self.timeout_seconds) as client:
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
            except httpx.ConnectError as conn_err:
                raise RuntimeError(
                    f"Não foi possível conectar ao servidor Ollama em {self.base_url}. "
                    "Verifique se o Ollama está rodando (ex.: 'ollama serve')."
                ) from conn_err
            except httpx.TimeoutException as timeout_err:
                raise RuntimeError(
                    f"Tempo limite ({self.timeout_seconds}s) excedido ao consultar o Ollama. "
                    "Para modelos locais em CPU, aumente OLLAMA_TIMEOUT_SECONDS ou use um modelo menor (ex.: llama3.2:3b)."
                ) from timeout_err

            data = resp.json()
            return data.get("message", {}).get("content", "").strip()

    def classify_and_structure(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], ClaudeExtractionResponse]:
        """
        Envia o conteúdo ao modelo local via Ollama, normaliza campos e validação Pydantic.
        """
        user_prompt = self.build_user_prompt(
            target_url=target_url,
            property_id=property_id,
            source_artifact=source_artifact,
            catalog=catalog,
        )
        sanitized_request = self.build_sanitized_request_payload(user_prompt=user_prompt)

        last_error: Optional[Exception] = None
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "Consultando modelo Ollama local (%s em %s) - tentativa %d/%d...",
                    self.model,
                    self.base_url,
                    attempt,
                    total_attempts,
                )
                raw_text = self._invoke_ollama_api(user_prompt)
                raw_json_dict = extract_json_object_from_text(raw_text)
                normalized_dict = normalize_extraction_response(
                    raw_json_dict,
                    catalog=catalog,
                    target_url=target_url,
                    property_id=property_id,
                    source_artifact=source_artifact,
                )
                validated = ClaudeExtractionResponse.model_validate(normalized_dict)
                logger.info(
                    "Resposta do Ollama normalizada e validada com sucesso (%d campos no total).",
                    len(validated.fields),
                )
                return sanitized_request, raw_json_dict, validated
            except (ValidationError, ValueError) as val_err:
                logger.error("Erro de validação na resposta do Ollama: %s", val_err)
                raise ValueError(f"Resposta inválida do Ollama: {val_err}") from val_err
            except Exception as api_err:
                last_error = api_err
                safe_err_msg = self.scrubber.redact_secrets(str(api_err))
                logger.warning(
                    "Falha na chamada ao Ollama (tentativa %d/%d): %s",
                    attempt,
                    total_attempts,
                    safe_err_msg,
                )
                if attempt < total_attempts:
                    time.sleep(1.0 * attempt)

        safe_final_err = self.scrubber.redact_secrets(str(last_error))
        raise RuntimeError(
            f"Não foi possível obter resposta do Ollama após {total_attempts} tentativas: {safe_final_err}"
        )


class ClaudeClient(BaseLLMClient):
    """Cliente de integração com o Claude (Anthropic SDK) com sanitização e retry limitado."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-3-5-sonnet-latest",
        scrubber: Optional[SecretScrubber] = None,
        max_retries: int = 2,
        timeout_seconds: float = 90.0,
        max_tokens: int = 8192,
        sdk_client: Optional[Any] = None,
    ) -> None:
        super().__init__(
            model=model,
            scrubber=scrubber or SecretScrubber([api_key]),
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        self.api_key = api_key
        self.scrubber.register_secret(api_key)
        self._sdk_client = sdk_client

    def build_sanitized_request_payload(
        self,
        *,
        user_prompt: str,
    ) -> Dict[str, Any]:
        """Gera o payload sanitizado da requisição para salvar em claude_request_sanitized.json."""
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "system": self.scrubber.redact_secrets(SYSTEM_PROMPT),
            "messages": [
                {
                    "role": "user",
                    "content": self.scrubber.redact_secrets(user_prompt),
                }
            ],
        }
        return self.scrubber.sanitize_structure(payload)

    def _invoke_anthropic_api(self, user_prompt: str) -> str:
        """Executa a chamada à API do Claude usando o SDK oficial da Anthropic (ou fallback HTTPS)."""
        if self._sdk_client is not None:
            response = self._sdk_client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return self._extract_text_from_sdk_response(response)

        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=self.api_key, timeout=self.timeout_seconds, max_retries=0)
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return self._extract_text_from_sdk_response(response)
        except ImportError:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": 0.0,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            with httpx.Client(timeout=self.timeout_seconds) as http_client:
                resp = http_client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                blocks = data.get("content", [])
                texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                return "\n".join(texts).strip()

    @staticmethod
    def _extract_text_from_sdk_response(response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            blocks = response.get("content", [])
            return "\n".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and "text" in b
            ).strip()
        content_blocks = getattr(response, "content", None)
        if content_blocks:
            extracted_parts = []
            for block in content_blocks:
                text_val = getattr(block, "text", None)
                if text_val is None and isinstance(block, dict):
                    text_val = block.get("text")
                if text_val:
                    extracted_parts.append(str(text_val))
            return "\n".join(extracted_parts).strip()
        raise ValueError("Formato de resposta inesperado retornado pelo SDK da Anthropic.")

    def classify_and_structure(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], ClaudeExtractionResponse]:
        """
        Envia o conteúdo extraído ao Claude com retry limitado, valida o JSON via Pydantic e
        retorna (sanitized_request_dict, raw_response_dict, validated_response).
        """
        if not self.api_key or not self.api_key.strip():
            raise ValueError("Chave da API da Anthropic (ANTHROPIC_API_KEY) não informada.")

        user_prompt = self.build_user_prompt(
            target_url=target_url,
            property_id=property_id,
            source_artifact=source_artifact,
            catalog=catalog,
        )
        sanitized_request = self.build_sanitized_request_payload(user_prompt=user_prompt)

        last_error: Optional[Exception] = None
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "Consultando modelo Claude (%s) - tentativa %d/%d...",
                    self.model,
                    attempt,
                    total_attempts,
                )
                raw_text = self._invoke_anthropic_api(user_prompt)
                raw_json_dict = extract_json_object_from_text(raw_text)
                normalized_dict = normalize_extraction_response(
                    raw_json_dict,
                    catalog=catalog,
                    target_url=target_url,
                    property_id=property_id,
                    source_artifact=source_artifact,
                )
                validated = ClaudeExtractionResponse.model_validate(normalized_dict)
                logger.info(
                    "Resposta do Claude validada com sucesso (%d campos recebidos).",
                    len(validated.fields),
                )
                return sanitized_request, raw_json_dict, validated
            except (ValidationError, ValueError) as val_err:
                logger.error("Erro de validação na resposta do Claude: %s", val_err)
                raise ValueError(f"Resposta inválida do Claude: {val_err}") from val_err
            except Exception as api_err:
                last_error = api_err
                safe_err_msg = self.scrubber.redact_secrets(str(api_err))
                logger.warning(
                    "Falha na chamada à API do Claude (tentativa %d/%d): %s",
                    attempt,
                    total_attempts,
                    safe_err_msg,
                )
                if attempt < total_attempts:
                    time.sleep(1.0 * attempt)

        safe_final_err = self.scrubber.redact_secrets(str(last_error))
        raise RuntimeError(
            f"Não foi possível obter resposta da API do Claude após {total_attempts} tentativas: {safe_final_err}"
        )


class ClaudeCliClient(BaseLLMClient):
    """
    Cliente que usa o Claude Code CLI em modo headless (`claude -p`).

    Autentica com o login da assinatura do Claude Code (Pro/Max), sem ANTHROPIC_API_KEY.
    As ferramentas do agente são desativadas: o modelo apenas responde texto.
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        cli_path: str = "claude",
        scrubber: Optional[SecretScrubber] = None,
        max_retries: int = 1,
        timeout_seconds: float = 300.0,
        runner: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
    ) -> None:
        super().__init__(
            model=model,
            scrubber=scrubber,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
        )
        self.cli_path = cli_path
        self._runner = runner or subprocess.run

    def build_command(self) -> List[str]:
        """Monta o comando do CLI (o prompt do usuário é enviado via stdin)."""
        return [
            self.cli_path,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--system-prompt",
            SYSTEM_PROMPT,
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "",
        ]

    def build_sanitized_request_payload(self, *, user_prompt: str) -> Dict[str, Any]:
        """Gera o payload sanitizado (comando + prompts) para salvar como artefato JSON."""
        payload = {
            "provider": "claude_cli",
            "model": self.model,
            "command": [self.cli_path, "-p", "--output-format", "json", "--model", self.model],
            "system": self.scrubber.redact_secrets(SYSTEM_PROMPT),
            "messages": [
                {"role": "user", "content": self.scrubber.redact_secrets(user_prompt)},
            ],
        }
        return self.scrubber.sanitize_structure(payload)

    @staticmethod
    def _build_child_env() -> Dict[str, str]:
        """Remove a chave da API para forçar o uso do login da assinatura do Claude Code."""
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _invoke_cli(self, user_prompt: str) -> str:
        """Executa `claude -p` e retorna o texto da resposta (campo `result`)."""
        try:
            with tempfile.TemporaryDirectory(prefix="stays_claude_cli_") as isolated_cwd:
                completed = self._runner(
                    self.build_command(),
                    input=user_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=isolated_cwd,
                    env=self._build_child_env(),
                    check=False,
                )
        except FileNotFoundError as missing_err:
            raise ClaudeCliFatalError(
                f"Executável do Claude Code não encontrado ('{self.cli_path}'). "
                "Instale o Claude Code e faça login com 'claude' antes de usar LLM_PROVIDER=claude_cli."
            ) from missing_err
        except subprocess.TimeoutExpired as timeout_err:
            raise ClaudeCliFatalError(
                f"Tempo limite ({self.timeout_seconds}s) excedido ao aguardar o Claude Code CLI. "
                "Aumente CLAUDE_CLI_TIMEOUT_SECONDS se necessário."
            ) from timeout_err

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        try:
            envelope = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            envelope = None

        if not isinstance(envelope, dict):
            detail = stderr or stdout or "saída vazia"
            raise RuntimeError(
                f"Saída inesperada do Claude Code CLI (código {completed.returncode}): {detail[:500]}"
            )

        result_text = envelope.get("result")
        if envelope.get("is_error") or completed.returncode != 0:
            detail = result_text or stderr or envelope.get("subtype") or "erro desconhecido"
            raise RuntimeError(f"Claude Code CLI retornou erro: {str(detail)[:500]}")

        if not isinstance(result_text, str) or not result_text.strip():
            raise RuntimeError("Claude Code CLI retornou resposta vazia.")
        return result_text.strip()

    def classify_and_structure(
        self,
        *,
        target_url: str,
        property_id: str,
        source_artifact: SourceExtractionArtifact,
        catalog: CustomFieldsCatalog,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], ClaudeExtractionResponse]:
        """Envia o conteúdo ao Claude via CLI, normaliza e valida a resposta com Pydantic."""
        user_prompt = self.build_user_prompt(
            target_url=target_url,
            property_id=property_id,
            source_artifact=source_artifact,
            catalog=catalog,
        )
        sanitized_request = self.build_sanitized_request_payload(user_prompt=user_prompt)

        last_error: Optional[Exception] = None
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "Consultando Claude via Claude Code CLI (%s) - tentativa %d/%d...",
                    self.model,
                    attempt,
                    total_attempts,
                )
                raw_text = self._invoke_cli(user_prompt)
                raw_json_dict = extract_json_object_from_text(raw_text)
                normalized_dict = normalize_extraction_response(
                    raw_json_dict,
                    catalog=catalog,
                    target_url=target_url,
                    property_id=property_id,
                    source_artifact=source_artifact,
                )
                validated = ClaudeExtractionResponse.model_validate(normalized_dict)
                logger.info(
                    "Resposta do Claude (CLI) validada com sucesso (%d campos no total).",
                    len(validated.fields),
                )
                return sanitized_request, raw_json_dict, validated
            except ClaudeCliFatalError:
                raise
            except (ValidationError, ValueError) as val_err:
                logger.error("Erro de validação na resposta do Claude (CLI): %s", val_err)
                raise ValueError(f"Resposta inválida do Claude (CLI): {val_err}") from val_err
            except Exception as cli_err:
                last_error = cli_err
                logger.warning(
                    "Falha na chamada ao Claude Code CLI (tentativa %d/%d): %s",
                    attempt,
                    total_attempts,
                    self.scrubber.redact_secrets(str(cli_err)),
                )
                if attempt < total_attempts:
                    time.sleep(1.0 * attempt)

        safe_final_err = self.scrubber.redact_secrets(str(last_error))
        raise RuntimeError(
            f"Não foi possível obter resposta do Claude Code CLI após {total_attempts} tentativas: {safe_final_err}"
        )


class ClaudeCliFatalError(RuntimeError):
    """Erro do Claude Code CLI que não deve ser repetido (executável ausente ou timeout)."""


def create_llm_client(
    settings: AppSettings,
    scrubber: Optional[SecretScrubber] = None,
) -> BaseLLMClient:
    """Cria a instância apropriada de LLMClient de acordo com o provedor selecionado nas configurações."""
    provider = settings.llm_provider.lower().strip()
    if provider == "ollama":
        settings.validate_for_llm()
        return OllamaClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
            num_ctx=settings.ollama_num_ctx,
            scrubber=scrubber,
        )
    elif provider == "anthropic":
        settings.validate_for_llm()
        return ClaudeClient(
            api_key=settings.anthropic_api_key,
            model=settings.claude_model,
            scrubber=scrubber,
        )
    elif provider == "claude_cli":
        settings.validate_for_llm()
        return ClaudeCliClient(
            model=settings.claude_cli_model,
            cli_path=settings.claude_cli_path,
            timeout_seconds=settings.claude_cli_timeout_seconds,
            scrubber=scrubber,
        )
    else:
        raise ValueError(
            f"Provedor LLM desconhecido: '{settings.llm_provider}'. "
            "Escolha 'ollama', 'anthropic' ou 'claude_cli'."
        )

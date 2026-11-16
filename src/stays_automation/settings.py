"""Carregamento e validação de configurações e variáveis de ambiente (.env e CLI)."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stays_automation.models import validate_http_url


VALID_LLM_PROVIDERS = ("ollama", "anthropic", "claude_cli")
DEFAULT_STAYS_PROPERTY_URL_TEMPLATE = "https://ssmt.stays.com.br/i/apartment/{property_id}/setup"
DEFAULT_SOURCE_URL_TEMPLATE = "https://www.multitemporada.com/pt/apartment/{property_id}"


def build_url_from_template(template: str, property_id: str) -> str:
    """Monta uma URL a partir de um modelo com o marcador {property_id}."""
    if "{property_id}" not in template:
        raise ValueError(f"Modelo de URL sem o marcador {{property_id}}: '{template}'.")
    return validate_http_url(template.replace("{property_id}", property_id.strip().upper()))


def extract_property_id_from_url(target_url: str) -> str:
    """Extrai o código do imóvel a partir da URL do Stays (ex.: /i/apartment/AB25I/setup -> AB25I)."""
    parsed = urlparse(target_url.strip())
    match = re.search(r"/apartment/([A-Za-z0-9_-]+)", parsed.path)
    if match:
        return match.group(1).upper()
    parts = [segment for segment in parsed.path.split("/") if segment and segment.lower() != "setup"]
    if parts:
        candidate = re.sub(r"[^A-Za-z0-9_-]", "", parts[-1])
        if candidate:
            return candidate.upper()
    return "UNKNOWN"


class AppSettings(BaseModel):
    """Configurações globais da aplicação combinando .env e parâmetros de CLI."""

    model_config = ConfigDict(extra="ignore")

    stays_base_url: str = Field(default="https://ssmt.stays.com.br/i/home")
    stays_login: str = Field(default="")
    stays_password: str = Field(default="")
    llm_provider: str = Field(default="ollama")
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3:latest")
    ollama_timeout_seconds: float = Field(default=600.0, ge=5.0)
    ollama_num_ctx: int = Field(default=16384, ge=2048)
    anthropic_api_key: str = Field(default="")
    claude_model: str = Field(default="claude-3-5-sonnet-latest")
    claude_cli_path: str = Field(default="claude")
    claude_cli_model: str = Field(default="sonnet")
    claude_cli_timeout_seconds: float = Field(default=300.0, ge=5.0)
    stays_property_url_template: str = Field(default=DEFAULT_STAYS_PROPERTY_URL_TEMPLATE)
    default_source_url_template: str = Field(default=DEFAULT_SOURCE_URL_TEMPLATE)
    headless: bool = Field(default=False)
    dry_run: bool = Field(default=True)
    save_changes: bool = Field(default=False)
    confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    skip_empty: bool = Field(default=True)
    mask_phone_numbers: bool = Field(default=False)

    project_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2]
    )
    output_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "artifacts"
    )
    custom_fields_config_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "config" / "custom_fields.yaml"
    )
    selectors_config_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "config" / "selectors.yaml"
    )
    session_state_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / ".session" / "stays_state.json"
    )

    @field_validator("stays_base_url")
    @classmethod
    def validate_base_url(cls, val: str) -> str:
        return validate_http_url(val)

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, val: str) -> str:
        cleaned = val.strip().lower()
        if cleaned in VALID_LLM_PROVIDERS:
            return cleaned
        raise ValueError(
            f"Provedor LLM inválido: '{val}'. Escolha 'ollama', 'anthropic' ou 'claude_cli'."
        )

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, val: str) -> str:
        return validate_http_url(val)

    @field_validator("ollama_model")
    @classmethod
    def default_ollama_model_if_blank(cls, val: str) -> str:
        cleaned = val.strip()
        return cleaned if cleaned else "llama3:latest"

    @field_validator("claude_cli_path")
    @classmethod
    def default_claude_cli_path_if_blank(cls, val: str) -> str:
        cleaned = val.strip()
        return cleaned if cleaned else "claude"

    @field_validator("claude_cli_model")
    @classmethod
    def default_claude_cli_model_if_blank(cls, val: str) -> str:
        cleaned = val.strip()
        return cleaned if cleaned else "sonnet"

    @field_validator("claude_model")
    @classmethod
    def default_claude_model_if_blank(cls, val: str) -> str:
        cleaned = val.strip()
        return cleaned if cleaned else "claude-3-5-sonnet-latest"

    @model_validator(mode="after")
    def enforce_save_vs_dry_run_exclusivity(self) -> "AppSettings":
        if self.save_changes and self.dry_run:
            raise ValueError(
                "Conflito de parâmetros: --save e --dry-run não podem ser utilizados simultaneamente."
            )
        return self

    def validate_for_claude(self) -> None:
        """Garante que a chave da API da Anthropic foi informada antes de chamar a LLM."""
        if not self.anthropic_api_key or not self.anthropic_api_key.strip():
            raise ValueError(
                "Variável de ambiente ANTHROPIC_API_KEY não configurada no arquivo .env."
            )

    def validate_for_llm(self) -> None:
        """Valida as configurações do provedor de LLM configurado (Ollama ou Anthropic)."""
        provider = self.llm_provider.strip().lower()
        if provider == "anthropic":
            self.validate_for_claude()
        elif provider == "ollama":
            if not self.ollama_base_url or not self.ollama_base_url.strip():
                raise ValueError("Configuração OLLAMA_BASE_URL não informada.")
            if not self.ollama_model or not self.ollama_model.strip():
                raise ValueError("Configuração OLLAMA_MODEL não informada.")
        elif provider == "claude_cli":
            if shutil.which(self.claude_cli_path) is None:
                raise ValueError(
                    f"Executável do Claude Code não encontrado ('{self.claude_cli_path}'). "
                    "Instale o Claude Code, faça login com 'claude' ou ajuste CLAUDE_CLI_PATH."
                )
        else:
            raise ValueError(
                f"Provedor LLM desconhecido: '{self.llm_provider}'. "
                "Escolha 'ollama', 'anthropic' ou 'claude_cli'."
            )

    def validate_for_stays_auth(self) -> None:
        """Garante que as credenciais do Stays foram configuradas quando necessário."""
        if not self.stays_login.strip() or not self.stays_password.strip():
            raise ValueError(
                "Credenciais STAYS_LOGIN e STAYS_PASSWORD devem estar definidas no arquivo .env."
            )


def parse_bool_env(value: Optional[str], default: bool) -> bool:
    """Converte strings de variáveis de ambiente em booleano seguro."""
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "sim", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "nao", "não", "n", "off"):
        return False
    return default


def load_settings(
    env_file: Optional[Path] = None,
    *,
    headless_override: Optional[bool] = None,
    dry_run_flag: bool = False,
    save_flag: bool = False,
    output_dir_override: Optional[Path] = None,
    confidence_threshold_override: Optional[float] = None,
    skip_empty_override: Optional[bool] = None,
    llm_provider_override: Optional[str] = None,
    ollama_url_override: Optional[str] = None,
    ollama_model_override: Optional[str] = None,
    ollama_timeout_override: Optional[float] = None,
    model_override: Optional[str] = None,
) -> AppSettings:
    """Carrega configurações do arquivo .env e aplica sobrescritas da CLI com validação."""
    if day_env := env_file:
        load_dotenv(dotenv_path=day_env, override=False)
    else:
        load_dotenv(override=False)

    if save_flag and dry_run_flag:
        raise ValueError(
            "Execução rejeitada: as opções --save e --dry-run são mutuamente exclusivas."
        )

    env_headless = parse_bool_env(os.getenv("HEADLESS"), default=False)
    env_dry_run = parse_bool_env(os.getenv("DRY_RUN"), default=True)
    env_mask_phones = parse_bool_env(os.getenv("MASK_PHONE_NUMBERS"), default=False)

    if save_flag:
        effective_save = True
        effective_dry_run = False
    elif dry_run_flag:
        effective_save = False
        effective_dry_run = True
    else:
        effective_dry_run = env_dry_run
        effective_save = not effective_dry_run

    effective_headless = headless_override if headless_override is not None else env_headless
    effective_threshold = (
        confidence_threshold_override if confidence_threshold_override is not None else 0.80
    )
    effective_skip_empty = skip_empty_override if skip_empty_override is not None else True

    raw_provider = llm_provider_override or os.getenv("LLM_PROVIDER")
    effective_provider = raw_provider.strip().lower() if raw_provider else "ollama"

    effective_ollama_url = (
        ollama_url_override
        if ollama_url_override is not None
        else os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    effective_ollama_model = (
        ollama_model_override
        if ollama_model_override is not None
        else os.getenv("OLLAMA_MODEL", "llama3:latest")
    )

    raw_timeout = os.getenv("OLLAMA_TIMEOUT_SECONDS", "600")
    try:
        env_timeout = float(raw_timeout)
    except (ValueError, TypeError):
        env_timeout = 600.0
    effective_ollama_timeout = (
        ollama_timeout_override if ollama_timeout_override is not None else env_timeout
    )

    effective_claude_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
    effective_claude_cli_model = os.getenv("CLAUDE_CLI_MODEL", "sonnet")

    raw_cli_timeout = os.getenv("CLAUDE_CLI_TIMEOUT_SECONDS", "300")
    try:
        effective_claude_cli_timeout = float(raw_cli_timeout)
    except (ValueError, TypeError):
        effective_claude_cli_timeout = 300.0

    if model_override and model_override.strip():
        if effective_provider == "ollama":
            effective_ollama_model = model_override.strip()
        elif effective_provider == "anthropic":
            effective_claude_model = model_override.strip()
        elif effective_provider == "claude_cli":
            effective_claude_cli_model = model_override.strip()

    kwargs = {
        "stays_base_url": os.getenv("STAYS_BASE_URL", "https://ssmt.stays.com.br/i/home"),
        "stays_login": os.getenv("STAYS_LOGIN", ""),
        "stays_password": os.getenv("STAYS_PASSWORD", ""),
        "llm_provider": effective_provider,
        "ollama_base_url": effective_ollama_url,
        "ollama_model": effective_ollama_model,
        "ollama_timeout_seconds": effective_ollama_timeout,
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "claude_model": effective_claude_model,
        "claude_cli_path": os.getenv("CLAUDE_CLI_PATH", "claude"),
        "claude_cli_model": effective_claude_cli_model,
        "claude_cli_timeout_seconds": effective_claude_cli_timeout,
        "stays_property_url_template": os.getenv(
            "STAYS_PROPERTY_URL_TEMPLATE", DEFAULT_STAYS_PROPERTY_URL_TEMPLATE
        ).strip()
        or DEFAULT_STAYS_PROPERTY_URL_TEMPLATE,
        "default_source_url_template": os.getenv(
            "DEFAULT_SOURCE_URL_TEMPLATE", DEFAULT_SOURCE_URL_TEMPLATE
        ).strip()
        or DEFAULT_SOURCE_URL_TEMPLATE,
        "headless": effective_headless,
        "dry_run": effective_dry_run,
        "save_changes": effective_save,
        "confidence_threshold": effective_threshold,
        "skip_empty": effective_skip_empty,
        "mask_phone_numbers": env_mask_phones,
    }
    if output_dir_override is not None:
        kwargs["output_dir"] = Path(output_dir_override).resolve()

    return AppSettings(**kwargs)


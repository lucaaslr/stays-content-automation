"""Utilitários de segurança, sanitização de credenciais e mascaramento de dados sensíveis."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Optional, Set

# Padrões universais de chaves de API, tokens e cabeçalhos de sessão
ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}")
BEARER_TOKEN_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*['\"]?bearer\s+)[A-Za-z0-9\-._~+/]+=*"
)
COOKIE_HEADER_PATTERN = re.compile(
    r"(?i)((?:set-cookie|cookie)\s*[:=]\s*['\"]?)[^'\"\r\n]+"
)
GENERIC_SECRET_KV_PATTERN = re.compile(
    r"(?i)((?:stays_password|anthropic_api_key|api_key|access_token|session_token|csrf_token|password)\s*[:=]\s*['\"]?)([^'\"\s,;}\]]+)"
)

# Padrões de conteúdo sensível operacional do imóvel (Wi-Fi, fechaduras, cofres, telefones)
WIFI_PASSWORD_PATTERN = re.compile(
    r"(?i)(senha(?:\s+do\s+wi-?fi|\s+wi-?fi|\s+da\s+rede)?\s*[:=]\s*)([^\s|,;\r\n]+)"
)
ACCESS_CODE_PATTERN = re.compile(
    r"(?i)((?:c[óo]digo(?:\s+do\s+cofre|\s+de\s+acesso|\s+da\s+porta)?|"
    r"senha(?:\s+tempor[áa]ria|\s+da\s+fechadura|\s+da\s+porta|\s+do\s+port[ãa]o|\s+de\s+acesso))\s*[:=]\s*)"
    r"([A-Za-z0-9#*@!\-_]{3,20})"
)
PHONE_PATTERN = re.compile(
    r"(?:(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?\d{4,5}[-\s]?\d{4})"
)
WHATSAPP_LINK_PHONE_PATTERN = re.compile(
    r"(https?://(?:wa\.me/|api\.whatsapp\.com/send\?phone=))(\d{8,15})"
)

SENSITIVE_DICT_KEYS = {
    "password",
    "stays_password",
    "stays_login",
    "anthropic_api_key",
    "api_key",
    "x-api-key",
    "authorization",
    "cookie",
    "set-cookie",
    "session_token",
    "access_token",
    "refresh_token",
    "csrf_token",
}


class SecretScrubber:
    """Gerencia o registro de segredos em memória e remove valores sensíveis de textos e estruturas."""

    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._literal_secrets: Set[str] = set()
        if secrets:
            for item in secrets:
                self.register_secret(item)

    def register_secret(self, secret_value: Optional[str]) -> None:
        """Registra um valor literal secreto (mínimo de 4 caracteres) para ocultação automática."""
        if secret_value:
            cleaned = secret_value.strip()
            if len(cleaned) >= 4:
                self._literal_secrets.add(cleaned)

    def redact_secrets(self, text: Optional[str]) -> str:
        """Oculta chaves de API, senhas do .env, cookies e tokens em qualquer string."""
        if not text:
            return ""
        redacted = text
        for literal in sorted(self._literal_secrets, key=len, reverse=True):
            redacted = redacted.replace(literal, "[REDACTED]")

        redacted = ANTHROPIC_KEY_PATTERN.sub("sk-ant-********", redacted)
        redacted = BEARER_TOKEN_PATTERN.sub(r"\1[REDACTED]", redacted)
        redacted = COOKIE_HEADER_PATTERN.sub(r"\1[REDACTED]", redacted)
        redacted = GENERIC_SECRET_KV_PATTERN.sub(r"\1[REDACTED]", redacted)
        return redacted

    def sanitize_structure(self, data: Any) -> Any:
        """Sanitiza recursivamente dicionários, listas e strings removendo segredos."""
        if isinstance(data, dict):
            sanitized_dict: Dict[str, Any] = {}
            for key, value in data.items():
                key_lower = str(key).lower().strip()
                if key_lower in SENSITIVE_DICT_KEYS:
                    sanitized_dict[key] = "[REDACTED]"
                else:
                    sanitized_dict[key] = self.sanitize_structure(value)
            return sanitized_dict
        if isinstance(data, list):
            return [self.sanitize_structure(item) for item in data]
        if isinstance(data, str):
            return self.redact_secrets(data)
        return copy.deepcopy(data)


def mask_sensitive_property_content(
    value: Optional[str],
    *,
    is_sensitive_field: bool = False,
    mask_phones: bool = False,
) -> Optional[str]:
    """
    Mascara senhas de Wi-Fi, códigos de fechadura/cofre e opcionalmente telefones
    para exibição segura em prévias, logs e artefatos públicos.
    """
    if value is None:
        return None

    masked = WIFI_PASSWORD_PATTERN.sub(r"\1********", value)
    masked = ACCESS_CODE_PATTERN.sub(r"\1****", masked)

    if mask_phones:
        masked = WHATSAPP_LINK_PHONE_PATTERN.sub(r"\1********", masked)
        masked = PHONE_PATTERN.sub("(XX) XXXXX-XXXX", masked)

    # Caso um campo seja marcado como sensível e não tenha rótulo explícito "senha:" ou "código:",
    # mas contenha sequências numéricas de PIN (4 a 8 dígitos isolados com ou sem #/*), mascara o PIN.
    if is_sensitive_field and masked == value and not value.startswith("http"):
        masked = re.sub(r"\b\d{4,8}[#*]\b", "****", masked)
        masked = re.sub(r"(?i)(\bpin\s*[:=]?\s*)(\d{4,8}\b)", r"\1****", masked)

    return masked


def sanitize_html_document(
    raw_html: str,
    scrubber: Optional[SecretScrubber] = None,
) -> str:
    """
    Sanitiza um documento HTML capturado da página do Stays no modo `inspect`,
    removendo scripts, valores de campos de senha, tokens CSRF, cookies e segredos.
    """
    if not raw_html:
        return ""

    active_scrubber = scrubber or SecretScrubber()
    html_text = active_scrubber.redact_secrets(raw_html)

    # Remove blocos <script>...</script> que podem conter tokens de sessão embutidos
    html_text = re.sub(
        r"(?is)<script\b[^>]*>.*?</script>",
        "<!-- script removido por seguranca -->",
        html_text,
    )

    # Oculta valores em inputs do tipo password ou hidden
    html_text = re.sub(
        r'(?i)(<input\b[^>]*type=["\'](?:password|hidden)["\'][^>]*value=["\'])([^"\']*)(["\'])',
        r"\1[REDACTED]\3",
        html_text,
    )
    html_text = re.sub(
        r'(?i)(<input\b[^>]*value=["\'])([^"\']*)(["\'][^>]*type=["\'](?:password|hidden)["\'])',
        r"\1[REDACTED]\3",
        html_text,
    )

    # Oculta senhas de Wi-Fi e códigos de acesso que já estejam preenchidos em textareas/inputs
    html_text = WIFI_PASSWORD_PATTERN.sub(r"\1********", html_text)
    html_text = ACCESS_CODE_PATTERN.sub(r"\1****", html_text)

    return html_text

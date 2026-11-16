"""Testes de segurança, ocultação de segredos em logs/artefatos e mascaramento de dados sensíveis."""

from __future__ import annotations

import logging
from pathlib import Path

from stays_automation.logging_config import configure_logging
from stays_automation.security import (
    SecretScrubber,
    mask_sensitive_property_content,
    sanitize_html_document,
)

FIXTURE_HTML_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_stays_setup.html"


def test_secret_scrubber_redacts_credentials_and_api_keys() -> None:
    """Garante que senhas do Stays, chaves da API do Claude e tokens nunca apareçam em texto claro."""
    scrubber = SecretScrubber(["MinhaSenhaSuperSecreta123", "usuario_stays_secreto"])
    raw_text = (
        "Erro ao autenticar usuario_stays_secreto com senha MinhaSenhaSuperSecreta123 "
        "e chave sk-ant-api03-ABCDEF1234567890XYZ e Authorization: Bearer tok_987654"
    )
    redacted = scrubber.redact_secrets(raw_text)

    assert "MinhaSenhaSuperSecreta123" not in redacted
    assert "usuario_stays_secreto" not in redacted
    assert "sk-ant-api03-ABCDEF1234567890XYZ" not in redacted
    assert "tok_987654" not in redacted
    assert "[REDACTED]" in redacted
    assert "sk-ant-********" in redacted


def test_mask_sensitive_property_content_wifi_lock_and_phone() -> None:
    """Testa o mascaramento de senhas de Wi-Fi, códigos de cofre/fechadura e telefones."""
    wifi_text = "Rede Wi-Fi: Cabana_5G | Senha do Wi-Fi: Serra@2026! | Velocidade: 300 Mbps"
    masked_wifi = mask_sensitive_property_content(wifi_text, is_sensitive_field=True)
    assert masked_wifi is not None
    assert "Serra@2026!" not in masked_wifi
    assert "********" in masked_wifi
    assert "Cabana_5G" in masked_wifi

    lock_text = "O cofre lateral usa código do cofre: 4829 e fechadura com senha temporária: 91735#"
    masked_lock = mask_sensitive_property_content(lock_text, is_sensitive_field=True)
    assert masked_lock is not None
    assert "4829" not in masked_lock
    assert "91735#" not in masked_lock
    assert "****" in masked_lock

    phone_text = "Fale com o anfitrião em (49) 99912-3456 ou https://wa.me/5549999123456"
    masked_phone = mask_sensitive_property_content(
        phone_text,
        is_sensitive_field=True,
        mask_phones=True,
    )
    assert masked_phone is not None
    assert "99912-3456" not in masked_phone
    assert "5549999123456" not in masked_phone


def test_sanitize_html_removes_scripts_and_passwords() -> None:
    """Garante que o HTML salvo no modo inspect não contenha scripts, tokens CSRF nem senhas."""
    raw_html = FIXTURE_HTML_PATH.read_text(encoding="utf-8")
    scrubber = SecretScrubber(["secret-session-jwt-token-999"])
    sanitized = sanitize_html_document(raw_html, scrubber=scrubber)

    assert "secret-session-jwt-token-999" not in sanitized
    assert "csrf-secret-value-12345" not in sanitized
    assert "SuperSecretAdminPass" not in sanitized
    assert "<script>" not in sanitized
    assert "Esteja Ciente (Pós reserva)" in sanitized


def test_logging_filter_never_writes_secrets_to_log_file(tmp_path: Path) -> None:
    """Verifica que o arquivo execution.log oculta automaticamente qualquer segredo registrado."""
    log_file = tmp_path / "execution.log"
    scrubber = SecretScrubber(["SegredoDoEnv999"])
    logger = configure_logging(scrubber=scrubber, log_file=log_file, level=logging.DEBUG)

    logger.info("Tentando login com SegredoDoEnv999 e chave sk-ant-1234567890abcdef")
    for handler in logger.handlers:
        handler.flush()

    log_content = log_file.read_text(encoding="utf-8")
    assert "SegredoDoEnv999" not in log_content
    assert "sk-ant-1234567890abcdef" not in log_content
    assert "[REDACTED]" in log_content

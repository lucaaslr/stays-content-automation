"""Cliente de automação Playwright para autenticação, inspeção e preenchimento no Stays."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from stays_automation.artifacts import ArtifactManager
from stays_automation.field_discovery import FieldDiscoveryService
from stays_automation.formatting import to_plain_text, to_stays_html
from stays_automation.models import (
    CustomFieldsCatalog,
    DiscoveredFieldSelector,
    ExecutionResultStatus,
    FieldAction,
    FieldExecutionRecord,
    SelectorsConfig,
)
from stays_automation.security import SecretScrubber
from stays_automation.settings import AppSettings

logger = logging.getLogger("stays_automation")


class StaysAutomationClient:
    """Orquestra a sessão Playwright no Stays com reutilização segura de sessão, dry-run e screenshots."""

    def __init__(
        self,
        settings: AppSettings,
        catalog: CustomFieldsCatalog,
        selectors_config: SelectorsConfig,
        artifact_manager: ArtifactManager,
        scrubber: Optional[SecretScrubber] = None,
        *,
        page_override: Optional[Any] = None,
        manual_auth_prompt_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.selectors_config = selectors_config
        self.artifact_manager = artifact_manager
        self.scrubber = scrubber or SecretScrubber(
            [settings.stays_login, settings.stays_password, settings.anthropic_api_key]
        )
        self.discovery = FieldDiscoveryService(
            catalog=catalog,
            selectors_config=selectors_config,
            scrubber=self.scrubber,
        )
        self._page = page_override
        self._playwright_ctx: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._context: Optional[Any] = None
        self._manual_auth_prompt = manual_auth_prompt_fn or input

    def __enter__(self) -> "StaysAutomationClient":
        if self._page is None:
            self._start_browser()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self.capture_error_screenshot()
        self.close()

    def _start_browser(self, force_visible: bool = False) -> None:
        """Inicia o navegador Chromium via Playwright, reutilizando storage_state se existir."""
        from playwright.sync_api import sync_playwright

        self._playwright_ctx = sync_playwright().start()
        headless_mode = False if force_visible else self.settings.headless
        logger.info("Iniciando navegador Chromium (headless=%s)...", headless_mode)
        self._browser = self._playwright_ctx.chromium.launch(headless=headless_mode)

        context_kwargs: Dict[str, Any] = {
            "locale": "pt-BR",
            "viewport": {"width": 1440, "height": 900},
        }
        state_file = self.settings.session_state_path
        if state_file.exists() and state_file.is_file():
            logger.info("Reutilizando sessão autenticada existente de forma segura.")
            context_kwargs["storage_state"] = str(state_file)

        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()

    def close(self) -> None:
        """Fecha recursos do Playwright de maneira limpa."""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright_ctx is not None:
                self._playwright_ctx.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright_ctx = None

    def _save_session_state(self) -> None:
        """Persiste o estado da sessão autenticada com permissão restrita ao usuário no Linux (0600)."""
        if self._context is None:
            return
        try:
            state_path = self.settings.session_state_path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(state_path))
            os.chmod(state_path, 0o600)
        except Exception as exc:
            logger.debug("Não foi possível persistir storage_state: %s", exc)

    def _first_visible_selector(self, selectors: List[str], timeout_ms: int = 1500) -> Optional[str]:
        """
        Aguarda até `timeout_ms` e retorna o primeiro seletor da lista presente e visível.

        `locator.is_visible()` do Playwright não espera (o parâmetro timeout é ignorado);
        em páginas SPA os elementos existem invisíveis logo após o carregamento, por isso
        a verificação é repetida até o prazo.
        """
        if self._page is None or not selectors:
            return None
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
        while True:
            for sel in selectors:
                try:
                    locator = self._page.locator(sel).first
                    if locator.count() > 0 and locator.is_visible():
                        return sel
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def _wait_for_login_form_hidden(self, timeout_ms: int) -> bool:
        """Aguarda o formulário de login desaparecer (login concluído). Retorna True se sumiu."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if not self._is_login_form_present(timeout_ms=0):
                return True
            time.sleep(0.5)
        return not self._is_login_form_present(timeout_ms=0)

    def _detect_captcha_or_mfa(self) -> bool:
        """Detecta se a página apresenta desafio CAPTCHA ou autenticação multifator (MFA/2FA)."""
        if self._page is None:
            return False
        indicators = self.selectors_config.login.get("captcha_or_mfa_indicators", [])
        if self._first_visible_selector(indicators, timeout_ms=500):
            return True
        try:
            html_lower = self._page.content().lower()
            tokens = ("g-recaptcha", "h-captcha", "cf-turnstile", "código de verificação", "autenticação em duas etapas")
            return any(t in html_lower for t in tokens)
        except Exception:
            return False

    def _is_login_form_present(self, timeout_ms: int = 1000) -> bool:
        pwd_selectors = self.selectors_config.login.get(
            "password_selectors",
            ["input[type='password']"],
        )
        return self._first_visible_selector(pwd_selectors, timeout_ms=timeout_ms) is not None

    def _wait_for_login_or_app(self, timeout_ms: int = 15000) -> str:
        """
        Aguarda a SPA renderizar e informa o estado: 'login' (formulário visível),
        'app' (indicador de sessão autenticada visível) ou 'unknown' (nenhum dos dois).
        """
        pwd_selectors = self.selectors_config.login.get("password_selectors", ["input[type='password']"])
        app_selectors = self.selectors_config.login.get("logged_in_indicators", [])
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if self._first_visible_selector(pwd_selectors, timeout_ms=0):
                return "login"
            if self._first_visible_selector(app_selectors, timeout_ms=0):
                return "app"
            if time.monotonic() >= deadline:
                return "unknown"
            time.sleep(0.25)

    def authenticate_if_needed(self) -> None:
        """
        Acessa a página inicial do Stays e realiza login com variáveis de ambiente.
        Se detectar CAPTCHA ou MFA, não tenta contornar: solicita conclusão manual ao usuário.
        """
        if self._page is None:
            raise RuntimeError("Página do navegador não inicializada.")

        logger.info("Acessando página inicial do Stays: %s", self.settings.stays_base_url)
        self._page.goto(self.settings.stays_base_url, wait_until="domcontentloaded", timeout=30000)

        if self._detect_captcha_or_mfa():
            logger.warning(
                "Desafio CAPTCHA ou MFA detectado! A automação jamais tenta contornar desafios de segurança."
            )
            self._manual_auth_prompt(
                "Conclua a autenticação/verificação manualmente na janela do navegador e pressione ENTER para continuar..."
            )
            self._save_session_state()
            return

        page_state = self._wait_for_login_or_app(timeout_ms=15000)
        if page_state != "login":
            if page_state == "app":
                logger.info("Sessão já autenticada no Stays.")
            else:
                logger.warning(
                    "Formulário de login não apareceu em 15s; assumindo sessão autenticada "
                    "(será verificado novamente na página do imóvel)."
                )
            return

        self.settings.validate_for_stays_auth()
        user_selectors = self.selectors_config.login.get(
            "username_selectors",
            ["input[name='login']", "input[type='email']", "input[name='username']"],
        )
        pwd_selectors = self.selectors_config.login.get(
            "password_selectors",
            ["input[type='password']", "input[name='password']"],
        )
        submit_selectors = self.selectors_config.login.get(
            "submit_selectors",
            ["button[type='submit']", "input[type='submit']"],
        )

        user_sel = self._first_visible_selector(user_selectors, timeout_ms=3000)
        pwd_sel = self._first_visible_selector(pwd_selectors, timeout_ms=3000)
        submit_sel = self._first_visible_selector(submit_selectors, timeout_ms=3000)

        if not user_sel or not pwd_sel or not submit_sel:
            logger.warning(
                "Campos de login padrão não localizados automaticamente. Solicitando login manual."
            )
            self._manual_auth_prompt(
                "Realize o login manualmente no navegador aberto e pressione ENTER quando concluir..."
            )
            self._save_session_state()
            return

        logger.info("Preenchendo credenciais de login a partir do ambiente (.env)...")
        self._page.locator(user_sel).first.fill(self.settings.stays_login)
        self._page.locator(pwd_sel).first.fill(self.settings.stays_password)
        self._page.locator(submit_sel).first.click()

        try:
            self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if self._detect_captcha_or_mfa():
            logger.warning(
                "Autenticação multifator (MFA) ou CAPTCHA solicitado após envio de credenciais."
            )
            self._manual_auth_prompt(
                "Conclua a verificação manualmente no navegador e pressione ENTER para prosseguir..."
            )

        if not self._wait_for_login_form_hidden(timeout_ms=20000):
            raise RuntimeError(
                "Login no Stays não foi concluído: o formulário de login continua visível após o envio. "
                "Verifique STAYS_LOGIN e STAYS_PASSWORD no .env (ou se há mensagem de erro na tela)."
            )

        logger.info("Login no Stays realizado com sucesso.")
        self._save_session_state()

    def navigate_to_property_custom_fields(
        self,
        target_url: str,
        expected_property_id: str,
    ) -> None:
        """
        Navega até a URL de configuração do imóvel, confirma o código do imóvel e
        abre a seção 'Conteúdo descritivo' -> 'Campos personalizados'.
        """
        if self._page is None:
            raise RuntimeError("Página do navegador não inicializada.")

        for attempt in range(1, 3):
            try:
                logger.info("Acessando página do imóvel (%s) - tentativa %d/2...", target_url, attempt)
                self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                break
            except Exception as nav_err:
                if attempt == 2:
                    raise RuntimeError(f"Timeout/erro ao acessar {target_url}: {nav_err}") from nav_err
                time.sleep(1.0)

        try:
            self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        if self._is_login_form_present(timeout_ms=3000):
            raise RuntimeError(
                "Sessão não autenticada: a página do imóvel exibiu o formulário de login do Stays. "
                "Nenhum campo foi preenchido."
            )

        current_url = getattr(self._page, "url", target_url)
        page_html = self._page.content()

        # Confirma que a página corresponde ao imóvel esperado
        if (
            expected_property_id.lower() not in current_url.lower()
            and expected_property_id.lower() not in page_html.lower()
        ):
            raise RuntimeError(
                f"Verificação de segurança falhou: a página carregada ({current_url}) "
                f"não parece corresponder ao imóvel esperado '{expected_property_id}'."
            )

        logger.info("Página confirmada para o imóvel %s.", expected_property_id)

        # Navega para a aba/seção "Conteúdo descritivo" se clicável
        desc_selectors = self.selectors_config.navigation.get("descriptive_content_selectors", [])
        desc_sel = self._first_visible_selector(desc_selectors, timeout_ms=1500)
        if desc_sel:
            logger.info("Abrindo seção 'Conteúdo descritivo' (%s)...", desc_sel)
            self._page.locator(desc_sel).first.click()
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        # Garante visualização da área "Campos personalizados"
        cf_selectors = self.selectors_config.navigation.get("custom_fields_section_selectors", [])
        cf_sel = self._first_visible_selector(cf_selectors, timeout_ms=1500)
        if cf_sel:
            logger.info("Seção 'Campos personalizados' localizada (%s).", cf_sel)
            try:
                self._page.locator(cf_sel).first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

    def inspect_and_discover_fields(
        self,
        target_url: str,
        property_id: str,
        *,
        save_sanitized_html: bool = False,
    ) -> Tuple[List[DiscoveredFieldSelector], Dict[str, str], Dict[str, Any]]:
        """
        Extrai o HTML atual da página do Stays, descobre todos os rótulos e inputs/textareas,
        salva field_mapping.json e opcionalmente o HTML sanitizado.
        """
        if self._page is None:
            raise RuntimeError("Página do navegador não inicializada.")

        raw_html = self._page.content()
        if save_sanitized_html:
            html_path = self.artifact_manager.write_sanitized_html(raw_html)
            logger.info("HTML sanitizado salvo em: %s", html_path)

        discovered, current_values = self.discovery.discover_from_html(
            raw_html,
            mask_phones=self.settings.mask_phone_numbers,
        )
        mapping_report = self.discovery.build_field_mapping_report(
            discovered=discovered,
            target_url=target_url,
            property_id=property_id,
        )
        self.artifact_manager.write_field_mapping(mapping_report)
        logger.info(
            "Descoberta de campos concluída: %d controles encontrados (%d mapeados ao catálogo).",
            len(discovered),
            mapping_report["mapped_to_catalog_count"],
        )
        return discovered, current_values, mapping_report

    def capture_screenshot(self, path: Path) -> Path:
        """Captura screenshot da página atual ou gera arquivo válido de diagnóstico."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._page is not None and hasattr(self._page, "screenshot"):
            try:
                self._page.screenshot(path=str(path), full_page=True)
                return path
            except Exception as exc:
                logger.warning("Aviso ao capturar screenshot em %s: %s", path, exc)
        return self.artifact_manager.ensure_screenshot_file(path)

    def capture_error_screenshot(self) -> Path:
        """Captura screenshot de erro (error.png) preservando o estado para diagnóstico."""
        err_path = self.artifact_manager.error_screenshot_path
        logger.error("Gerando captura de tela de diagnóstico de falha em: %s", err_path)
        return self.capture_screenshot(err_path)

    def _resolve_working_locator(
        self,
        discovered_item: DiscoveredFieldSelector,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Testa o seletor recomendado e os fallbacks até encontrar um elemento visível/editável."""
        if self._page is None:
            return None, None

        all_candidates = [discovered_item.recommended_selector] + discovered_item.fallback_selectors
        for candidate in all_candidates:
            try:
                loc = self._page.locator(candidate).first
                if loc.count() > 0:
                    return loc, candidate
            except Exception:
                continue
        return None, None

    # Os campos personalizados do Stays usam o editor Summernote: o <textarea name=...> fica
    # oculto e o conteúdo visível é um div.note-editable. O preenchimento é feito pela API do
    # Summernote (quando disponível) para manter editor, textarea e evento de alteração em sincronia.
    _SUMMERNOTE_FILL_JS = """
    (el, html) => {
        const editor = el.nextElementSibling;
        if (el.tagName !== 'TEXTAREA' || !editor || !editor.classList.contains('note-editor')) {
            return null;
        }
        const jq = window.jQuery || window.$;
        if (jq && jq(el).data('summernote')) {
            jq(el).summernote('code', html);
            jq(el).trigger('change');
            return 'summernote_api';
        }
        const editable = editor.querySelector('.note-editable');
        if (!editable) {
            return null;
        }
        editable.innerHTML = html;
        el.value = html;
        editable.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return 'summernote_dom';
    }
    """

    _SUMMERNOTE_READ_JS = """
    (el) => {
        const editor = el.nextElementSibling;
        if (el.tagName === 'TEXTAREA' && editor && editor.classList.contains('note-editor')) {
            const editable = editor.querySelector('.note-editable');
            return editable ? editable.innerText : null;
        }
        return null;
    }
    """

    @classmethod
    def _read_live_value(cls, locator: Any) -> str:
        """Lê o valor atual do campo (texto do editor Summernote ou valor do input/textarea)."""
        if hasattr(locator, "evaluate"):
            try:
                rich_text = locator.evaluate(cls._SUMMERNOTE_READ_JS)
                if rich_text is not None:
                    return str(rich_text).strip()
            except Exception:
                pass
        if hasattr(locator, "input_value"):
            return (locator.input_value() or "").strip()
        return ""

    @classmethod
    def _write_value(cls, locator: Any, value: str) -> str:
        """
        Preenche o campo. Em editores Summernote, insere o HTML no padrão de estilo do Stays
        (parágrafos, linhas em branco e negrito); em campos simples, insere o texto puro.
        """
        if hasattr(locator, "evaluate"):
            method = locator.evaluate(cls._SUMMERNOTE_FILL_JS, to_stays_html(value))
            if method:
                return str(method)
        locator.fill(to_plain_text(value))
        return "fill"

    def fill_approved_fields(
        self,
        records: List[FieldExecutionRecord],
        discovered_fields: List[DiscoveredFieldSelector],
        *,
        property_id: str,
        target_url: str,
        already_completed_keys: Optional[Set[str]] = None,
    ) -> List[FieldExecutionRecord]:
        """
        Preenche na página do Stays exclusivamente os campos aprovados (FILL ou OVERWRITE),
        respeitando idempotência, --resume e gravando before_fill.png / after_fill.png.
        """
        completed = set(already_completed_keys or set())
        discovered_by_key: Dict[str, DiscoveredFieldSelector] = {
            d.field_key: d for d in discovered_fields if d.field_key
        }

        self.capture_screenshot(self.artifact_manager.before_fill_screenshot_path)

        for rec in records:
            if rec.field_key in completed:
                logger.info(
                    "Campo '%s' (%s) já concluído em execução anterior (--resume); ignorando repetição.",
                    rec.stays_label,
                    rec.field_key,
                )
                if rec.result == ExecutionResultStatus.PENDING:
                    rec.result = ExecutionResultStatus.FILLED
                continue

            if not rec.will_be_filled or rec.proposed_value is None:
                if rec.result == ExecutionResultStatus.PENDING:
                    rec.result = (
                        ExecutionResultStatus.KEPT
                        if rec.action == FieldAction.KEEP_EXISTING
                        else ExecutionResultStatus.SKIPPED
                    )
                continue

            disc = discovered_by_key.get(rec.field_key)
            if disc is None:
                msg = f"Seletor não encontrado na página para o rótulo '{rec.stays_label}'."
                logger.warning(msg)
                rec.warning = f"{rec.warning}; {msg}".strip("; ")
                rec.result = ExecutionResultStatus.ERROR
                continue

            locator, used_selector = self._resolve_working_locator(disc)
            if locator is None:
                msg = f"Elemento HTML não pôde ser localizado pelos seletores de '{rec.stays_label}'."
                logger.warning(msg)
                rec.warning = f"{rec.warning}; {msg}".strip("; ")
                rec.result = ExecutionResultStatus.ERROR
                continue

            try:
                # Verifica valor atual em tempo real para evitar preenchimento duplicado ou perda acidental
                live_value = self._read_live_value(locator)

                if live_value and to_plain_text(live_value) == to_plain_text(rec.proposed_value):
                    rec.action = FieldAction.KEEP_EXISTING
                    rec.result = ExecutionResultStatus.KEPT
                    rec.selector_used = used_selector
                else:
                    fill_method = self._write_value(locator, rec.proposed_value)
                    rec.selector_used = used_selector
                    rec.result = (
                        ExecutionResultStatus.OVERWRITTEN
                        if rec.action == FieldAction.OVERWRITE
                        else ExecutionResultStatus.FILLED
                    )
                    logger.info(
                        "Campo preenchido com sucesso: '%s' (%s) via seletor [%s] (%s)",
                        rec.stays_label,
                        rec.field_key,
                        used_selector,
                        fill_method,
                    )

                self.artifact_manager.save_field_progress(
                    records,
                    property_id=property_id,
                    target_url=target_url,
                )
            except Exception as fill_err:
                safe_err = self.scrubber.redact_secrets(str(fill_err))
                logger.error("Erro ao preencher o campo '%s': %s", rec.stays_label, safe_err)
                rec.warning = f"{rec.warning}; Erro ao preencher: {safe_err}".strip("; ")
                rec.result = ExecutionResultStatus.ERROR
                self.capture_error_screenshot()
                self.artifact_manager.save_field_progress(
                    records,
                    property_id=property_id,
                    target_url=target_url,
                )

        self.capture_screenshot(self.artifact_manager.after_fill_screenshot_path)
        return records

    def save_changes_on_page(self) -> bool:
        """
        Clica no botão 'Salvar' do formulário de campos personalizados e confirma a gravação.

        Exige uma requisição de escrita (POST/PUT/PATCH, no Stays: POST /jsonrpc) com HTTP de
        sucesso e sem campo 'error' no corpo JSON-RPC. O aviso "Salvo com sucesso" é verificado
        em seguida; elementos de sucesso já visíveis antes do clique são ignorados.
        Bloqueado por segurança se settings.dry_run for True ou settings.save_changes for False.
        """
        if self.settings.dry_run or not self.settings.save_changes:
            logger.info("Modo dry-run ativo: o botão Salvar NÃO será acionado.")
            return False

        if self._page is None:
            raise RuntimeError("Página do navegador não inicializada.")
        page = self._page

        save_selectors = self.selectors_config.actions.get(
            "save_button_selectors",
            ["form:has(h4:has-text('Campos personalizados')) button[type='submit']"],
        )
        save_sel = self._first_visible_selector(save_selectors, timeout_ms=5000)
        if not save_sel:
            self.capture_error_screenshot()
            raise RuntimeError(
                "Botão 'Salvar' de 'Campos personalizados' não ficou visível. "
                "O Stays pode não ter detectado as alterações; nada foi salvo."
            )

        success_selectors = self.selectors_config.actions.get("success_message_selectors", [])
        already_visible_success = {
            css for css in success_selectors if self._first_visible_selector([css], timeout_ms=0)
        }

        save_button = page.locator(save_sel).first
        page_host = urlparse(getattr(page, "url", "") or "").netloc

        def is_save_request(response: Any) -> bool:
            request = response.request
            if request.method not in ("POST", "PUT", "PATCH"):
                return False
            if request.resource_type not in ("xhr", "fetch", "document"):
                return False
            return not page_host or urlparse(response.url).netloc == page_host

        logger.info("Clicando no botão Salvar (%s)...", save_sel)
        try:
            with page.expect_response(is_save_request, timeout=30000) as response_info:
                save_button.click()
            response = response_info.value
        except Exception as wait_err:
            raise RuntimeError(
                "Nenhuma requisição de salvamento ao Stays foi detectada em 30s após clicar em Salvar; "
                f"as alterações podem não ter sido gravadas ({wait_err})."
            ) from wait_err

        logger.info(
            "Requisição de salvamento: %s %s -> HTTP %s",
            response.request.method,
            urlparse(response.url).path,
            response.status,
        )
        if response.status >= 400:
            raise RuntimeError(
                f"O Stays recusou o salvamento (HTTP {response.status} em {urlparse(response.url).path})."
            )

        rpc_error = self._extract_rpc_error(response)
        if rpc_error:
            raise RuntimeError(f"O Stays retornou erro ao salvar: {self.scrubber.redact_secrets(rpc_error)[:300]}")

        # O aviso "Salvo com sucesso" some em poucos segundos: verificar logo após a resposta.
        new_success = self._first_visible_selector(
            [css for css in success_selectors if css not in already_visible_success],
            timeout_ms=8000,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if new_success:
            logger.info("Salvamento confirmado pelo Stays (%s).", new_success)
        else:
            logger.warning(
                "Requisição de salvamento concluída com HTTP %s, mas o aviso 'Salvo com sucesso' não foi "
                "detectado. Confira os campos na página do Stays.",
                response.status,
            )
        return True

    @staticmethod
    def _extract_rpc_error(response: Any) -> Optional[str]:
        """Retorna a mensagem de erro de uma resposta JSON-RPC (HTTP 200 com campo 'error'), se houver."""
        try:
            body = response.json()
        except Exception:
            return None
        items = body if isinstance(body, list) else [body]
        for item in items:
            if isinstance(item, dict) and item.get("error"):
                err = item["error"]
                if isinstance(err, dict):
                    return str(err.get("message") or err)
                return str(err)
        return None

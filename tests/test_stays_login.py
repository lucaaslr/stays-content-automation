"""Testes do fluxo de login no Stays considerando renderização tardia da SPA."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from stays_automation.artifacts import ArtifactManager
from stays_automation.models import CustomFieldsCatalog, SelectorsConfig
from stays_automation.settings import AppSettings
from stays_automation.stays_client import StaysAutomationClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SpaLocator:
    def __init__(self, page: "SpaLoginPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "SpaLocator":
        return self

    def _kind(self) -> str:
        sel = self._selector
        if "type='password'" in sel or "name='password'" in sel:
            return "password"
        if sel == "input[name='login']":
            return "login"
        if sel == "button[type='submit']":
            return "submit"
        return "other"

    def count(self) -> int:
        kind = self._kind()
        if kind == "other":
            return 0
        if kind == "password" and self._page.logged_in:
            return 0
        return 1

    def is_visible(self) -> bool:
        if self.count() == 0:
            return False
        # Simula SPA: o formulário existe mas fica invisível nas primeiras verificações
        self._page.visibility_checks += 1
        return self._page.visibility_checks > self._page.invisible_checks

    def fill(self, value: str) -> None:
        self._page.filled[self._kind()] = value

    def click(self) -> None:
        if self._kind() == "submit" and self._page.accept_credentials:
            self._page.logged_in = True


class SpaLoginPage:
    def __init__(self, *, invisible_checks: int, accept_credentials: bool) -> None:
        self.invisible_checks = invisible_checks
        self.accept_credentials = accept_credentials
        self.visibility_checks = 0
        self.logged_in = False
        self.filled: Dict[str, str] = {}
        self.url = ""

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def content(self) -> str:
        return "<html><body>login</body></html>"

    def locator(self, selector: str) -> SpaLocator:
        return SpaLocator(self, selector)


def _client(tmp_path: Path, page: SpaLoginPage, prompts: List[str]) -> StaysAutomationClient:
    settings = AppSettings(
        stays_login="usuario@example.com",
        stays_password="senha-secreta",
        output_dir=tmp_path,
        session_state_path=tmp_path / "state.json",
    )
    catalog = CustomFieldsCatalog.from_yaml(PROJECT_ROOT / "config" / "custom_fields.yaml")
    selectors = SelectorsConfig.from_yaml(PROJECT_ROOT / "config" / "selectors.yaml")
    artifacts = ArtifactManager(tmp_path, "MN01I")
    return StaysAutomationClient(
        settings,
        catalog,
        selectors,
        artifacts,
        page_override=page,
        manual_auth_prompt_fn=lambda msg: prompts.append(msg) or "",
    )


def test_login_waits_for_spa_form_and_submits_credentials(tmp_path: Path) -> None:
    page = SpaLoginPage(invisible_checks=3, accept_credentials=True)
    prompts: List[str] = []
    _client(tmp_path, page, prompts).authenticate_if_needed()

    assert page.filled.get("login") == "usuario@example.com"
    assert page.filled.get("password") == "senha-secreta"
    assert page.logged_in is True
    assert prompts == []


def test_login_raises_when_form_stays_visible_after_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        StaysAutomationClient,
        "_wait_for_login_form_hidden",
        lambda self, timeout_ms: not self._is_login_form_present(timeout_ms=0),
    )
    page = SpaLoginPage(invisible_checks=0, accept_credentials=False)
    with pytest.raises(RuntimeError, match="Login no Stays não foi concluído"):
        _client(tmp_path, page, []).authenticate_if_needed()


def test_property_page_showing_login_form_aborts(tmp_path: Path) -> None:
    page = SpaLoginPage(invisible_checks=0, accept_credentials=False)
    with pytest.raises(RuntimeError, match="Sessão não autenticada"):
        _client(tmp_path, page, []).navigate_to_property_custom_fields(
            "https://ssmt.stays.com.br/i/apartment/MN01I/setup", "MN01I"
        )

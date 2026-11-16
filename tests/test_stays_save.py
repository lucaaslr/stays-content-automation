"""Testes da confirmação de salvamento dos campos personalizados no Stays."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional

import pytest

from stays_automation.artifacts import ArtifactManager
from stays_automation.models import CustomFieldsCatalog, SelectorsConfig
from stays_automation.settings import AppSettings
from stays_automation.stays_client import StaysAutomationClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAVE_SELECTOR_TOKEN = "Campos personalizados"


class SaveLocator:
    def __init__(self, page: "SavePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "SaveLocator":
        return self

    def _is_save_button(self) -> bool:
        return SAVE_SELECTOR_TOKEN in self._selector and "button" in self._selector

    def count(self) -> int:
        if self._is_save_button():
            return 1
        if self._selector == ".alert-success":
            return 1  # selo "AI" sempre visível na página real
        if self._selector == "text=Salvo com sucesso":
            return 1 if self._page.toast_shown else 0
        return 0

    def is_visible(self) -> bool:
        # No Stays real o botão Salvar continua visível mesmo após salvar
        return self.count() > 0

    def click(self) -> None:
        if self._is_save_button():
            self._page.clicks += 1
            self._page.on_click()


class SavePage:
    url = "https://ssmt.stays.com.br/i/apartment/MN01I/setup"

    def __init__(self, *, status: Optional[int], shows_toast: bool = True, rpc_error: Any = None) -> None:
        self.status = status
        self.shows_toast = shows_toast
        self.rpc_error = rpc_error
        self.toast_shown = False
        self.clicks = 0
        self._pending: List[Any] = []

    def on_click(self) -> None:
        if self.status is not None:
            request = SimpleNamespace(method="POST", resource_type="xhr")
            body = {"jsonrpc": "2.0", "id": 1, "result": None if self.rpc_error else True, "error": self.rpc_error}
            self._pending.append(
                SimpleNamespace(
                    request=request,
                    url="https://ssmt.stays.com.br/jsonrpc",
                    status=self.status,
                    json=lambda: body,
                )
            )
            if self.shows_toast and self.status < 400 and not self.rpc_error:
                self.toast_shown = True

    @contextmanager
    def expect_response(self, predicate: Callable[[Any], bool], timeout: int = 30000):
        holder = SimpleNamespace(value=None)
        yield holder
        matches = [r for r in self._pending if predicate(r)]
        if not matches:
            raise TimeoutError("Timeout waiting for response")
        holder.value = matches[0]

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> SaveLocator:
        return SaveLocator(self, selector)

    def screenshot(self, path: str, full_page: bool = True) -> None:
        from stays_automation.artifacts import PLACEHOLDER_PNG_BYTES

        Path(path).write_bytes(PLACEHOLDER_PNG_BYTES)


def _client(tmp_path: Path, page: SavePage) -> StaysAutomationClient:
    settings = AppSettings(
        output_dir=tmp_path,
        session_state_path=tmp_path / "state.json",
        dry_run=False,
        save_changes=True,
    )
    return StaysAutomationClient(
        settings,
        CustomFieldsCatalog.from_yaml(PROJECT_ROOT / "config" / "custom_fields.yaml"),
        SelectorsConfig.from_yaml(PROJECT_ROOT / "config" / "selectors.yaml"),
        ArtifactManager(tmp_path, "MN01I"),
        page_override=page,
    )


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acelera as esperas de 15s simulando a passagem do tempo."""
    clock = {"now": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr("stays_automation.stays_client.time.sleep", fake_sleep)
    monkeypatch.setattr("stays_automation.stays_client.time.monotonic", lambda: clock["now"])


def test_save_confirmed_by_http_response_and_toast(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    page = SavePage(status=200)
    with caplog.at_level("INFO", logger="stays_automation"):
        assert _client(tmp_path, page).save_changes_on_page() is True
    assert page.clicks == 1
    assert "Salvamento confirmado pelo Stays" in caplog.text


def test_save_ok_without_toast_only_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    page = SavePage(status=200, shows_toast=False)
    assert _client(tmp_path, page).save_changes_on_page() is True
    assert "não foi detectado" in caplog.text


def test_save_ignores_preexisting_alert_and_fails_without_request(tmp_path: Path) -> None:
    page = SavePage(status=None)
    with pytest.raises(RuntimeError, match="Nenhuma requisição de salvamento"):
        _client(tmp_path, page).save_changes_on_page()


def test_save_fails_on_http_error(tmp_path: Path) -> None:
    page = SavePage(status=500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        _client(tmp_path, page).save_changes_on_page()


def test_save_fails_on_jsonrpc_error_with_http_200(tmp_path: Path) -> None:
    page = SavePage(status=200, rpc_error={"code": -32000, "message": "Campo inválido"})
    with pytest.raises(RuntimeError, match="Campo inválido"):
        _client(tmp_path, page).save_changes_on_page()


def test_dry_run_never_clicks(tmp_path: Path) -> None:
    page = SavePage(status=200)
    client = _client(tmp_path, page)
    client.settings.dry_run = True
    client.settings.save_changes = False
    assert client.save_changes_on_page() is False
    assert page.clicks == 0

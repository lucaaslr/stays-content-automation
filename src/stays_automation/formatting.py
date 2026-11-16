"""Guia de estilo dos campos personalizados e conversão do texto para o HTML do Summernote (Stays)."""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_STYLE_GUIDE_PATH = Path(__file__).resolve().parents[2] / "config" / "style_guide.yaml"

_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_DASH_WITHOUT_SPACE = re.compile(r"^-(?=[^\s-])")


class StyleGuide(BaseModel):
    """Regras de redação/formatação carregadas de config/style_guide.yaml."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="1.0")
    reference_property: str = Field(default="")
    markup_rules: str = Field(default="")
    writing_rules: str = Field(default="")
    field_formats: Dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "StyleGuide":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})

    def format_for(self, field_key: str) -> Optional[str]:
        value = self.field_formats.get(field_key)
        return value.strip() if value else None

    def as_prompt_section(self) -> str:
        """Bloco de instruções de estilo para o prompt da LLM."""
        if not (self.markup_rules or self.writing_rules):
            return ""
        return (
            "PADRÃO DE ESTILO OBRIGATÓRIO PARA O CAMPO \"value\" "
            f"(baseado no imóvel de referência {self.reference_property or 'padrão'}; "
            "copie apenas o formato, nunca dados do imóvel de referência):\n"
            "Marcação:\n"
            f"{self.markup_rules.strip()}\n"
            "Redação:\n"
            f"{self.writing_rules.strip()}\n"
            "O formato específico de cada campo está no atributo \"style\" da especificação dos campos.\n"
        )


@lru_cache(maxsize=4)
def load_style_guide(path: Optional[Path] = None) -> StyleGuide:
    """Carrega o guia de estilo; retorna guia vazio se o arquivo não existir."""
    target = Path(path) if path else DEFAULT_STYLE_GUIDE_PATH
    if not target.exists():
        return StyleGuide()
    return StyleGuide.from_yaml(target)


def _normalize_lines(text: str) -> list[str]:
    """Normaliza quebras, remove espaços sobrando, padroniza '- ' e colapsa linhas vazias repetidas."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        line = _DASH_WITHOUT_SPACE.sub("- ", raw.strip())
        if not line and (not lines or not lines[-1]):
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def to_stays_html(text: str) -> str:
    """
    Converte o texto marcado (linhas, linhas vazias e **negrito**) no HTML usado pelo Stays:
    cada linha vira <p>, linha vazia vira <p><br></p> e **texto** vira <b>texto</b>.
    """
    paragraphs = []
    for line in _normalize_lines(text):
        if not line:
            paragraphs.append("<p><br></p>")
            continue
        escaped = html.escape(line, quote=False)
        with_bold = _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)
        paragraphs.append(f"<p>{with_bold}</p>")
    return "".join(paragraphs)


def to_plain_text(text: str) -> str:
    """Remove a marcação de negrito e normaliza linhas (usado para comparar com o valor da página)."""
    lines = [_BOLD_PATTERN.sub(r"\1", line) for line in _normalize_lines(text)]
    return "\n".join(line for line in lines if line)

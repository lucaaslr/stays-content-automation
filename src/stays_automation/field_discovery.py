"""Descoberta dinâmica de campos personalizados, rótulos e seletores robustos no Stays."""

from __future__ import annotations

import html
import logging
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from stays_automation.content_mapper import ContentMapper, normalize_label
from stays_automation.models import (
    CustomFieldsCatalog,
    DiscoveredFieldSelector,
    SelectorsConfig,
)
from stays_automation.security import SecretScrubber, mask_sensitive_property_content

logger = logging.getLogger("stays_automation")


class _FieldControlNode:
    """Representa um elemento <textarea> ou <input> descoberto no HTML."""

    def __init__(
        self,
        tag: str,
        attrs: Dict[str, str],
        group_id: int,
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.group_id = group_id
        self.value: str = attrs.get("value", "")
        self.element_id: Optional[str] = attrs.get("id") or None
        self.element_name: Optional[str] = attrs.get("name") or None
        self.data_testid: Optional[str] = attrs.get("data-testid") or None
        self.aria_label: Optional[str] = attrs.get("aria-label") or None
        self.data_label: Optional[str] = attrs.get("data-label") or None


class _LabelNode:
    """Representa um elemento <label> descoberto no HTML."""

    def __init__(
        self,
        for_id: Optional[str],
        text: str,
        group_id: int,
        nested_control: Optional[_FieldControlNode] = None,
    ) -> None:
        self.for_id = for_id or None
        self.text = text.strip()
        self.group_id = group_id
        self.nested_control = nested_control


class _StaysFormDiscoveryParser(HTMLParser):
    """Analisa o HTML da seção de campos personalizados para associar rótulos a inputs/textareas."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: List[_LabelNode] = []
        self.controls: List[_FieldControlNode] = []
        self._group_counter = 0
        self._group_stack: List[int] = [0]
        self._in_label = False
        self._label_for: Optional[str] = None
        self._label_group_id: int = 0
        self._label_text_parts: List[str] = []
        self._label_nested_control: Optional[_FieldControlNode] = None
        self._active_textarea: Optional[_FieldControlNode] = None
        self._textarea_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs_list}

        if tag_lower in ("div", "fieldset", "li", "tr", "section"):
            self._group_counter += 1
            self._group_stack.append(self._group_counter)

        current_group = self._group_stack[-1] if self._group_stack else 0

        if tag_lower == "label":
            self._in_label = True
            self._label_for = attrs.get("for") or None
            self._label_group_id = current_group
            self._label_text_parts.clear()
            self._label_nested_control = None
        elif tag_lower == "textarea":
            node = _FieldControlNode("textarea", attrs, current_group)
            self.controls.append(node)
            self._active_textarea = node
            self._textarea_parts.clear()
            if self._in_label and self._label_nested_control is None:
                self._label_nested_control = node
        elif tag_lower == "input":
            input_type = (attrs.get("type") or "text").lower()
            if input_type not in ("hidden", "submit", "button", "checkbox", "radio", "password", "file"):
                node = _FieldControlNode("input", attrs, current_group)
                self.controls.append(node)
                if self._in_label and self._label_nested_control is None:
                    self._label_nested_control = node

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "textarea" and self._active_textarea is not None:
            self._active_textarea.value = "".join(self._textarea_parts).strip()
            self._active_textarea = None
            self._textarea_parts.clear()
        elif tag_lower == "label" and self._in_label:
            label_text = " ".join("".join(self._label_text_parts).split()).strip()
            if label_text:
                self.labels.append(
                    _LabelNode(
                        for_id=self._label_for,
                        text=label_text,
                        group_id=self._label_group_id,
                        nested_control=self._label_nested_control,
                    )
                )
            self._in_label = False
            self._label_for = None
            self._label_text_parts.clear()
            self._label_nested_control = None
        elif tag_lower in ("div", "fieldset", "li", "tr", "section"):
            if len(self._group_stack) > 1:
                self._group_stack.pop()

    def handle_data(self, data: str) -> None:
        unescaped = html.unescape(data)
        if self._active_textarea is not None:
            self._textarea_parts.append(unescaped)
        elif self._in_label:
            self._label_text_parts.append(unescaped)


def _escape_css_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_selectors_for_control(
    label_text: str,
    control: _FieldControlNode,
    override_selector: Optional[str] = None,
) -> Tuple[str, List[str], str]:
    """
    Constrói o seletor recomendado e lista de fallbacks priorizando:
    1. Override explícito configurado em selectors.yaml
    2. data-testid
    3. Relação label[for] / id
    4. Atributo name
    5. Relação por texto visível do rótulo (label:has-text + textarea/input)
    """
    candidates: List[Tuple[str, str]] = []
    escaped_label = _escape_css_string(label_text)

    if override_selector:
        candidates.append(("config_override", override_selector))

    if control.data_testid:
        esc_tid = _escape_css_string(control.data_testid)
        candidates.append(("data-testid", f"{control.tag}[data-testid='{esc_tid}']"))

    if control.element_id:
        esc_id = _escape_css_string(control.element_id)
        candidates.append(("id", f"{control.tag}[id='{esc_id}']"))

    if control.element_name:
        esc_name = _escape_css_string(control.element_name)
        candidates.append(("name", f"{control.tag}[name='{esc_name}']"))

    if label_text == ".":
        candidates.append(
            (
                "visible_label_relation",
                f"xpath=//label[normalize-space(.)='.']/following::{control.tag}[1]",
            )
        )
    else:
        candidates.append(
            (
                "visible_label_relation",
                f"xpath=//label[normalize-space(.)='{escaped_label}']/following::{control.tag}[1]",
            )
        )
        candidates.append(
            (
                "label_has_text",
                f"div:has(> label:text-is('{escaped_label}')) {control.tag}",
            )
        )

    recommended_strategy, recommended_selector = candidates[0]
    fallback_selectors = [sel for _, sel in candidates[1:]]
    return recommended_selector, fallback_selectors, recommended_strategy


class FieldDiscoveryService:
    """Serviço de inspeção e descoberta de mapeamento entre rótulos visíveis e campos do Stays."""

    def __init__(
        self,
        catalog: CustomFieldsCatalog,
        selectors_config: Optional[SelectorsConfig] = None,
        scrubber: Optional[SecretScrubber] = None,
    ) -> None:
        self.catalog = catalog
        self.selectors_config = selectors_config or SelectorsConfig()
        self.scrubber = scrubber or SecretScrubber()
        self.mapper = ContentMapper(catalog)

    def discover_from_html(
        self,
        raw_html: str,
        *,
        mask_phones: bool = False,
    ) -> Tuple[List[DiscoveredFieldSelector], Dict[str, str]]:
        """
        Analisa o HTML da página do Stays e retorna:
        1. Lista de DiscoveredFieldSelector (com seletores recomendados e valores mascarados)
        2. Dicionário {field_key: valor_atual_bruto} dos campos reconhecidos na página
        """
        parser = _StaysFormDiscoveryParser()
        parser.feed(raw_html or "")
        parser.close()

        controls_by_id: Dict[str, _FieldControlNode] = {
            ctrl.element_id: ctrl for ctrl in parser.controls if ctrl.element_id
        }
        used_controls: set[int] = set()
        discovered: List[DiscoveredFieldSelector] = []
        raw_values_by_key: Dict[str, str] = {}

        # Associa cada <label> ao seu respectivo <textarea> ou <input>
        for idx, lbl in enumerate(parser.labels):
            matched_ctrl: Optional[_FieldControlNode] = None

            # Prioridade 1: atributo for="id"
            if lbl.for_id and lbl.for_id in controls_by_id:
                matched_ctrl = controls_by_id[lbl.for_id]
            # Prioridade 2: controle aninhado dentro do próprio <label>
            elif lbl.nested_control is not None:
                matched_ctrl = lbl.nested_control
            # Prioridade 3: controle no mesmo container/grupo HTML
            else:
                for ctrl in parser.controls:
                    if id(ctrl) not in used_controls and ctrl.group_id == lbl.group_id:
                        matched_ctrl = ctrl
                        break
            # Prioridade 4: próximo controle imediatamente subsequente no DOM
            if matched_ctrl is None and idx < len(parser.controls):
                for ctrl in parser.controls:
                    if id(ctrl) not in used_controls:
                        matched_ctrl = ctrl
                        break

            if matched_ctrl is None:
                continue

            used_controls.add(id(matched_ctrl))
            matched_cfg = self.mapper.match_field_config_by_label(lbl.text)
            field_key = matched_cfg.field_key if matched_cfg else None
            is_sensitive = matched_cfg.sensitive if matched_cfg else False

            override_sel: Optional[str] = None
            if field_key and field_key in self.selectors_config.field_overrides:
                override_sel = self.selectors_config.field_overrides[field_key].get("selector")

            rec_sel, fallbacks, strategy = build_selectors_for_control(
                lbl.text,
                matched_ctrl,
                override_selector=override_sel,
            )

            raw_val = matched_ctrl.value.strip() if matched_ctrl.value else ""
            if field_key and raw_val:
                raw_values_by_key[field_key] = raw_val

            safe_preview: Optional[str] = None
            if raw_val:
                redacted = self.scrubber.redact_secrets(raw_val)
                safe_preview = mask_sensitive_property_content(
                    redacted,
                    is_sensitive_field=is_sensitive,
                    mask_phones=mask_phones,
                )

            discovered.append(
                DiscoveredFieldSelector(
                    field_key=field_key,
                    stays_label=matched_cfg.stays_label if matched_cfg else lbl.text,
                    normalized_label=normalize_label(lbl.text),
                    element_tag=matched_ctrl.tag,
                    element_id=matched_ctrl.element_id,
                    element_name=matched_ctrl.element_name,
                    data_testid=matched_ctrl.data_testid,
                    recommended_selector=rec_sel,
                    fallback_selectors=fallbacks,
                    strategy_used=strategy,
                    current_value_preview=safe_preview,
                )
            )

        # Verifica controles que possuem aria-label ou data-label sem tag <label> explícita
        for ctrl in parser.controls:
            if id(ctrl) in used_controls:
                continue
            alt_label = ctrl.aria_label or ctrl.data_label
            if not alt_label:
                continue
            matched_cfg = self.mapper.match_field_config_by_label(alt_label)
            field_key = matched_cfg.field_key if matched_cfg else None
            is_sensitive = matched_cfg.sensitive if matched_cfg else False
            override_sel = None
            if field_key and field_key in self.selectors_config.field_overrides:
                override_sel = self.selectors_config.field_overrides[field_key].get("selector")

            rec_sel, fallbacks, strategy = build_selectors_for_control(
                alt_label,
                ctrl,
                override_selector=override_sel,
            )
            raw_val = ctrl.value.strip() if ctrl.value else ""
            if field_key and raw_val:
                raw_values_by_key[field_key] = raw_val

            safe_preview = None
            if raw_val:
                redacted = self.scrubber.redact_secrets(raw_val)
                safe_preview = mask_sensitive_property_content(
                    redacted,
                    is_sensitive_field=is_sensitive,
                    mask_phones=mask_phones,
                )

            discovered.append(
                DiscoveredFieldSelector(
                    field_key=field_key,
                    stays_label=matched_cfg.stays_label if matched_cfg else alt_label,
                    normalized_label=normalize_label(alt_label),
                    element_tag=ctrl.tag,
                    element_id=ctrl.element_id,
                    element_name=ctrl.element_name,
                    data_testid=ctrl.data_testid,
                    recommended_selector=rec_sel,
                    fallback_selectors=fallbacks,
                    strategy_used=strategy,
                    current_value_preview=safe_preview,
                )
            )

        return discovered, raw_values_by_key

    def build_field_mapping_report(
        self,
        discovered: List[DiscoveredFieldSelector],
        target_url: str,
        property_id: str,
    ) -> Dict[str, Any]:
        """Gera o dicionário estruturado para ser salvo em field_mapping.json."""
        by_key = {d.field_key: d for d in discovered if d.field_key}
        unmapped_catalog_fields = [
            {"field_key": f.field_key, "stays_label": f.stays_label}
            for f in self.catalog.fields
            if f.field_key not in by_key
        ]
        return {
            "property_id": property_id,
            "target_url": target_url,
            "configured_catalog_count": self.catalog.actual_count,
            "expected_catalog_count": self.catalog.expected_count,
            "count_divergence_warning": self.catalog.divergence_message,
            "discovered_total": len(discovered),
            "mapped_to_catalog_count": len(by_key),
            "unmapped_catalog_fields_count": len(unmapped_catalog_fields),
            "unmapped_catalog_fields": unmapped_catalog_fields,
            "fields": [d.model_dump() for d in discovered],
        }

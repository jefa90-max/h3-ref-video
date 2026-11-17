"use strict";

function frameCharacterReferences(frame) {
  return state.frameReferenceDrafts.get(frame.key) ?? (frame.references || []).filter((reference) => reference.kind === "character");
}

function frameGenerationReferences(frame) {
  const references = frame.references || [];
  return [...references.filter((reference) => reference.kind === "board"), ...frameCharacterReferences(frame)];
}

function frameReferencesChanged(frame) {
  const saved = (frame.references || []).filter((reference) => reference.kind === "character");
  return JSON.stringify(frameCharacterReferences(frame).map((reference) => reference.path)) !== JSON.stringify(saved.map((reference) => reference.path));
}

function createFrameReferenceEditor(frame, changed) {
  const section = element("section", "frame-references");
  let pickerOpen = false;
  function renderReferences() {
    section.replaceChildren(element("h4", "", t("frameReferences")), element("p", "hint", t("frameReferencesHint")));
    const references = frameGenerationReferences(frame);
    const list = element("div", "frame-reference-list");
    for (const [index, reference] of references.entries()) {
      const item = element("div", "frame-reference-item");
      const role = t(`frameReference_${reference.kind}`);
      const name = reference.kind === "character" ? reference.name || role : role;
      const title = t("frameReferenceTitle", { index: index + 1, name });
      if (reference.url) {
        const link = element("a", "frame-reference-preview");
        link.href = reference.url;
        link.target = "_blank";
        link.rel = "noopener";
        const image = element("img");
        image.src = reference.url;
        image.alt = title;
        image.loading = "lazy";
        link.append(image);
        item.append(link);
      } else item.append(element("span", "frame-reference-missing", t("frameReferenceMissing")));
      const caption = element("div", "frame-reference-caption");
      caption.append(element("strong", "", title));
      caption.append(element("span", "hint", reference.kind === "character" ? role : t("frameReferenceFixed")));
      item.append(caption);
      if (reference.kind === "character") {
        const remove = element("button", "button text-button frame-reference-action", t("removeImage"));
        remove.type = "button";
        remove.dataset.enabled = String(Boolean(frame.references_editable));
        remove.setAttribute("aria-label", t("removeNamedReference", { name: title }));
        remove.addEventListener("click", () => {
          if (remove.disabled) return;
          const characters = [...frameCharacterReferences(frame)];
          characters.splice(index - references.filter((entry) => entry.kind === "board").length, 1);
          state.frameReferenceDrafts.set(frame.key, characters);
          renderReferences();
          changed();
          renderResultActions();
        });
        item.append(remove);
      }
      list.append(item);
    }
    section.append(list);
    if (!frameCharacterReferences(frame).length) section.append(element("p", "hint", t("frameCharactersEmpty")));
    if (references.some((reference) => !reference.url)) section.append(element("p", "notice error", t("frameReferencesUnavailable")));
    const catalog = state.selectedJob?.character_references || [];
    const selected = new Set(frameCharacterReferences(frame).map((reference) => reference.path));
    const available = catalog.filter((reference) => !selected.has(reference.path));
    const toggle = element("button", "button secondary frame-reference-action frame-character-toggle", t("addFrameCharacters"));
    toggle.type = "button";
    toggle.dataset.enabled = String(Boolean(frame.references_editable && available.length));
    toggle.setAttribute("aria-expanded", String(pickerOpen));
    toggle.addEventListener("click", () => {
      if (toggle.disabled) return;
      pickerOpen = !pickerOpen;
      renderReferences();
      renderResultActions();
    });
    section.append(toggle);
    if (!catalog.length) section.append(element("p", "hint", t("frameCharacterCatalogEmpty")));
    else if (!available.length) section.append(element("p", "hint", t("frameCharactersAllSelected")));
    if (pickerOpen && available.length) {
      const picker = element("div", "frame-character-picker");
      for (const reference of available) {
        const name = reference.name || t("frameReference_character");
        const option = element("button", "frame-character-option frame-reference-action");
        option.type = "button";
        option.dataset.enabled = String(Boolean(frame.references_editable && reference.url));
        option.setAttribute("aria-label", t("addNamedFrameCharacter", { name }));
        if (reference.url) {
          const preview = element("span", "frame-reference-preview");
          const image = element("img");
          image.src = reference.url;
          image.alt = "";
          image.loading = "lazy";
          preview.append(image);
          option.append(preview);
        }
        option.append(element("span", "", name));
        if (!reference.url) option.append(element("span", "hint", t("frameReferenceMissing")));
        option.addEventListener("click", () => {
          if (option.disabled || frameCharacterReferences(frame).some((entry) => entry.path === reference.path)) return;
          state.frameReferenceDrafts.set(frame.key, [...frameCharacterReferences(frame), reference]);
          renderReferences();
          changed();
          renderResultActions();
        });
        picker.append(option);
      }
      section.append(picker);
    }
    if (frameReferencesChanged(frame)) section.append(element("p", "hint frame-reference-warning", t("frameReferenceNumberingHint")));
  }
  renderReferences();
  return section;
}

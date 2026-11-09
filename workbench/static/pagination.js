"use strict";

function renderPagination(prefix, page, pages) {
  const navigation = $(`${prefix}-pagination`);
  const previousPage = Number(navigation.dataset.page);
  navigation.dataset.page = String(page);
  navigation.dataset.pages = String(pages);
  for (const action of ["first", "previous"]) $(`${prefix}-${action}`).disabled = page <= 1;
  for (const action of ["next", "last"]) $(`${prefix}-${action}`).disabled = page >= pages;
  const input = $(`${prefix}-input`);
  input.max = String(pages);
  if (previousPage !== page) {
    input.value = String(page);
    input.setCustomValidity("");
  }
}

function initializePagination(prefix, changePage) {
  const navigation = $(`${prefix}-pagination`);
  const input = $(`${prefix}-input`);
  function navigate(page) {
    const pages = Number(navigation.dataset.pages);
    if (!Number.isSafeInteger(page) || page < 1 || page > pages) return;
    input.setCustomValidity("");
    input.value = String(page);
    if (page !== Number(navigation.dataset.page)) changePage(page);
  }
  for (const action of ["first", "previous", "next", "last"]) {
    const button = $(`${prefix}-${action}`);
    button.addEventListener("click", () => {
      if (button.disabled) return;
      const page = Number(navigation.dataset.page);
      navigate({ first: 1, previous: page - 1, next: page + 1, last: Number(navigation.dataset.pages) }[action]);
    });
  }
  input.addEventListener("input", () => input.setCustomValidity(""));
  $(`${prefix}-jump-form`).addEventListener("submit", (event) => {
    event.preventDefault();
    const page = Number(input.value);
    const pages = Number(navigation.dataset.pages);
    if (!/^\d+$/.test(input.value) || !Number.isSafeInteger(page) || page < 1 || page > pages) {
      input.setCustomValidity(t("pageNumberInvalid", { value: input.value || t("pageNumberEmpty"), pages }));
      input.reportValidity();
      return;
    }
    navigate(page);
  });
}

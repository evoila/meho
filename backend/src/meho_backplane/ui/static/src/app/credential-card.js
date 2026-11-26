// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Two small helpers for the credential card of the target page.
//
// 1. A file pick button next to a "several lines" field. The browser reads
//    the file and puts its text into the text area. The file input has no
//    name, so the file itself is never sent. MEHO stores no file.
// 2. When a connector declares more than one way to sign in, the radio
//    choice shows the fields of the chosen way and hides the others.
//
// The card is swapped in by htmx, so both helpers are delegated listeners
// on the document. Plain first-party JS, no inline script (CSP posture).

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;

  const fileFor = target.dataset.credentialFileFor;
  if (target.type === "file" && fileFor) {
    const area = document.getElementById(fileFor);
    const file = target.files && target.files[0];
    if (!area || !file) return;
    const reader = new FileReader();
    reader.onload = () => {
      area.value = typeof reader.result === "string" ? reader.result : "";
      target.value = "";
    };
    reader.readAsText(file);
    return;
  }

  if (target.type === "radio" && "credentialWayChoice" in target.dataset) {
    const form = target.closest("form");
    if (!form) return;
    for (const panel of form.querySelectorAll("[data-credential-way]")) {
      panel.hidden = panel.dataset.credentialWay !== target.value;
    }
  }
});

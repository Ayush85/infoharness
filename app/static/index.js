(function () {
  const forms = document.querySelectorAll("[data-scan-form]");

  forms.forEach((form) => {
    const input = form.querySelector("input[name=url]");
    const btn = form.querySelector("button[type=submit]");
    const note = form.querySelector("[data-note]");
    const defaultNote = note.textContent;
    const defaultLabel = btn.textContent;

    async function start(url) {
      note.classList.remove("is-error");
      if (!url) {
        note.textContent = "Enter the address of your docs or help center.";
        note.classList.add("is-error");
        input.focus();
        return;
      }
      btn.disabled = true;
      btn.textContent = "Starting…";
      try {
        const res = await fetch("/api/scans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "The scan couldn't start. Try again in a minute.");
        window.location.href = data.report_url;
      } catch (err) {
        note.textContent = err.message;
        note.classList.add("is-error");
        btn.disabled = false;
        btn.textContent = defaultLabel;
      }
    }

    form.addEventListener("submit", (e) => { e.preventDefault(); start(input.value.trim()); });
    input.addEventListener("input", () => {
      if (note.classList.contains("is-error")) { note.classList.remove("is-error"); note.textContent = defaultNote; }
    });
    form.start = start;
  });

  // Example chips fill the first form and scan straight away.
  document.querySelectorAll("[data-example]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const form = forms[0];
      form.querySelector("input[name=url]").value = chip.dataset.example;
      form.start(chip.dataset.example);
    });
  });

  // /?url=... (used by "Try again") pre-fills the form.
  const preset = new URLSearchParams(location.search).get("url");
  if (preset && forms[0]) forms[0].querySelector("input[name=url]").value = preset;
})();

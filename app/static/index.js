(function () {
  const form = document.getElementById("scan-form");
  const input = document.getElementById("url");
  const btn = document.getElementById("scan-btn");
  const note = document.getElementById("form-note");
  const defaultNote = note.textContent;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = input.value.trim();
    note.classList.remove("is-error");
    if (!url) {
      note.textContent = "Enter the address of your docs or help center.";
      note.classList.add("is-error");
      input.focus();
      return;
    }
    btn.disabled = true;
    btn.textContent = "Starting scan…";
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
      btn.textContent = "Scan my docs";
    }
  });

  input.addEventListener("input", () => {
    if (note.classList.contains("is-error")) {
      note.classList.remove("is-error");
      note.textContent = defaultNote;
    }
  });
})();

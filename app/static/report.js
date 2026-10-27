(function () {
  const root = document.querySelector("[data-report]");
  if (root && ["queued", "running"].includes(root.dataset.status)) poll(root.dataset.scanId);

  async function poll(id) {
    const stage = document.getElementById("stage");
    const fill = document.getElementById("bar-fill");
    const pages = document.getElementById("pages");
    const bar = fill.parentElement;
    const steps = [...document.querySelectorAll("#stepper li")];
    let delay = 1500;
    for (;;) {
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay + 250, 4000);
      let data;
      try {
        const res = await fetch(`/api/scans/${id}`, { cache: "no-store" });
        if (!res.ok) continue;
        data = await res.json();
      } catch (e) { continue; }
      if (data.status === "done" || data.status === "failed") { window.location.reload(); return; }
      if (data.status === "queued") {
        stage.textContent = data.queue_position > 1
          ? `Waiting for a free scanner (${data.queue_position - 1} ahead of you)…`
          : "Waiting for a free scanner…";
      } else {
        stage.textContent = "Scanning your docs…";
        let active = 0;
        steps.forEach((li, i) => { if (data.progress >= Number(li.dataset.start)) active = i; });
        steps.forEach((li, i) => { li.className = i < active ? "done" : i === active ? "active" : ""; });
      }
      fill.style.width = `${data.progress}%`;
      bar.setAttribute("aria-valuenow", data.progress);
      pages.textContent = data.pages_crawled;
    }
  }

  async function copy(text, btn) {
    const label = btn.textContent;
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; }
    catch (e) { btn.textContent = "Select and press Ctrl+C"; }
    setTimeout(() => (btn.textContent = label), 1800);
  }
  document.querySelectorAll("[data-copy-text]").forEach((b) => b.addEventListener("click", () => copy(b.dataset.copyText, b)));
  document.querySelectorAll("[data-copy-from]").forEach((b) =>
    b.addEventListener("click", () => copy(document.getElementById(b.dataset.copyFrom).value, b)));
  document.querySelectorAll("[data-print]").forEach((b) => b.addEventListener("click", () => window.print()));

  // Filter chips: show one type at a time, or all.
  const filters = [...document.querySelectorAll("[data-filter]")];
  const groups = [...document.querySelectorAll("[data-group]")];
  function applyFilter(type) {
    filters.forEach((f) => f.setAttribute("aria-pressed", String(f.dataset.filter === type)));
    groups.forEach((g) => (g.hidden = type !== "all" && g.dataset.group !== type));
  }
  filters.forEach((f) => f.addEventListener("click", () => {
    applyFilter(f.dataset.filter);
    if (f.dataset.filter !== "all") history.replaceState(null, "", "#" + f.dataset.filter); else history.replaceState(null, "", location.pathname);
  }));
  const fromHash = location.hash.slice(1);
  if (fromHash && groups.some((g) => g.dataset.group === fromHash)) applyFilter(fromHash);
})();

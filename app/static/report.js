(function () {
  const head = document.querySelector(".report-head");
  if (head) {
    const status = head.dataset.status;
    const id = head.dataset.scanId;
    if (status === "queued" || status === "running") poll(id);
  }

  async function poll(id) {
    const stage = document.getElementById("stage");
    const fill = document.getElementById("bar-fill");
    const pages = document.getElementById("pages");
    const bar = fill && fill.parentElement;
    let delay = 1500;
    for (;;) {
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay + 250, 4000);
      let data;
      try {
        const res = await fetch(`/api/scans/${id}`, { cache: "no-store" });
        if (!res.ok) continue;
        data = await res.json();
      } catch (e) {
        continue;
      }
      if (data.status === "done" || data.status === "failed") {
        window.location.reload();
        return;
      }
      if (data.status === "queued") {
        stage.textContent = data.queue_position > 1
          ? `Waiting for a free scanner (${data.queue_position - 1} ahead of you)…`
          : "Waiting for a free scanner…";
      } else {
        stage.textContent = data.stage.charAt(0).toUpperCase() + data.stage.slice(1) + "…";
      }
      fill.style.width = `${data.progress}%`;
      bar.setAttribute("aria-valuenow", data.progress);
      pages.textContent = data.pages_crawled;
    }
  }

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const field = document.getElementById(btn.dataset.copy);
      const label = btn.textContent;
      try {
        await navigator.clipboard.writeText(field.value);
        btn.textContent = "Copied";
      } catch (e) {
        field.select();
        btn.textContent = "Press Ctrl+C";
      }
      setTimeout(() => (btn.textContent = label), 1800);
    });
  });
})();

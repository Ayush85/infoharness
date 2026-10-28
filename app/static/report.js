(function () {
  const root = document.querySelector("[data-report]");
  if (root && ["queued", "running"].includes(root.dataset.status)) poll(root.dataset.scanId);

  async function poll(id) {
    const $ = (x) => document.getElementById(x);
    const stage = $("stage"), fill = $("bar-fill"), pages = $("pages"), found = $("pages-found"), links = $("links");
    const elapsed = $("elapsed"), eta = $("eta"), now = $("now"), feed = $("feed");
    const bar = fill.parentElement;
    const steps = [...document.querySelectorAll("#stepper li")];
    const label = { crawling: "Reading your pages", "analysing content": "Comparing what pages say", "checking links": "Checking links",
      "verifying contradictions": "Verifying contradictions", scoring: "Scoring", "saving report": "Writing the report" };
    const fmt = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`);
    const short = (u) => { try { const x = new URL(u); return (x.pathname + x.search) || "/"; } catch (e) { return u; } };
    let shown = Number(fill.style.width.replace("%", "")) || 0, target = shown, startedAt = Date.now(), samples = [];
    // glide the bar toward the latest value between server updates so it never looks stuck
    setInterval(() => {
      shown += (target - shown) * 0.12;
      fill.style.width = `${shown.toFixed(1)}%`;
      elapsed.textContent = fmt((Date.now() - startedAt) / 1000);
    }, 120);
    for (;;) {
      await new Promise((r) => setTimeout(r, 1000));
      let data;
      try {
        const res = await fetch(`/api/scans/${id}`, { cache: "no-store" });
        if (!res.ok) continue;
        data = await res.json();
      } catch (e) { continue; }
      if (data.status === "done" || data.status === "failed") { target = 100; fill.style.width = "100%"; window.location.reload(); return; }
      if (data.status === "queued") {
        stage.textContent = data.queue_position > 1
          ? `Waiting for a free scanner (${data.queue_position - 1} ahead of you)…` : "Waiting for a free scanner…";
        continue;
      }
      if (data.started_at) { const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(data.started_at) ? data.started_at : data.started_at + "Z"; startedAt = Math.min(Date.now(), new Date(iso).getTime()); }
      stage.textContent = (label[data.stage] || "Scanning your docs") + "…";
      let active = 0;
      steps.forEach((li, i) => { if (data.progress >= Number(li.dataset.start)) active = i; });
      steps.forEach((li, i) => { li.className = i < active ? "done" : i === active ? "active" : ""; });
      target = data.progress;
      bar.setAttribute("aria-valuenow", data.progress);
      pages.textContent = data.pages_crawled;
      const live = data.live || {};
      found.textContent = live.pages_found && live.pages_found > data.pages_crawled ? ` of ${live.pages_found} found` : "";
      links.textContent = live.links_total ? `${live.links_done || 0} of ${live.links_total}` : "–";
      now.textContent = live.current ? (data.stage === "checking links" ? "Checking " : "Reading ") + short(live.current) : "";
      feed.innerHTML = "";
      (live.recent || []).slice().reverse().forEach((u) => { const li = document.createElement("li"); li.textContent = short(u); feed.appendChild(li); });
      // time left: from how fast the bar moved over the last ~20 seconds
      samples.push([Date.now(), data.progress]); samples = samples.filter((x) => Date.now() - x[0] < 20000);
      const a = samples[0], b = samples[samples.length - 1];
      if (b[1] > a[1] && b[0] > a[0] && data.progress > 8) {
        const left = (100 - b[1]) / ((b[1] - a[1]) / ((b[0] - a[0]) / 1000));
        eta.textContent = left < 5 ? "a few seconds" : `about ${fmt(left)}`;
      } else eta.textContent = "estimating…";
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

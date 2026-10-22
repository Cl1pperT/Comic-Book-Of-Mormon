/* The review screen. Pages are stacked like the draft reader; clicking a page drops a pin and opens the issue form.
   All state lives on the server (each action is saved as it happens), so a reload or a lost connection loses nothing. */
(() => {
  const D = JSON.parse(document.getElementById("review-data").textContent);
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const readonly = D.review.state !== "active";
  let issues = D.issues.slice();
  const checked = new Set(D.checked);
  let selected = null;     // an issue id, "new", or null
  let draft = null;        // {page_id, x, y} for the issue being created
  let lastActive = Date.now();

  const $ = sel => document.querySelector(sel);
  const el = (tag, props = {}, children = []) => {
    const node = Object.assign(document.createElement(tag), props);
    for (const child of [].concat(children)) node.append(child);
    return node;
  };
  const pageById = id => D.pages.find(p => p.id === id);
  const number = issue => issues.findIndex(i => i.id === issue.id) + 1;
  const where = (page, panel) => (page.number === 0 ? "Cover" : "Page " + page.number)
    + (panel ? " · " + (panel.key === "cover" ? "cover art" : "panel " + Number(panel.key.split("_").pop())) : "");

  async function api(method, url, body) {
    const response = await fetch(url, {
      method, headers: {"Content-Type": "application/json", "X-CSRF-Token": CSRF},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = {};
    try { data = await response.json(); } catch (_) { /* not JSON */ }
    if (!response.ok) throw new Error(data.error || "Something went wrong (" + response.status + ").");
    return data;
  }

  // Same rule as the server: the panel containing the point, else the nearest one on that page.
  function locate(page, x, y) {
    let best = null, bestDistance = Infinity;
    for (const panel of page.panels) {
      const [px, py, pw, ph] = panel.frame;
      const dx = Math.max(px - x, 0, x - (px + pw)), dy = Math.max(py - y, 0, y - (py + ph));
      const distance = Math.hypot(dx, dy);
      if (distance < bestDistance) { best = panel; bestDistance = distance; }
    }
    return best;
  }

  const done = page => checked.has(page.id) || issues.some(i => i.page_id === page.id);
  const required = () => D.pages.filter(p => p.required);
  const remaining = () => required().filter(p => !done(p));

  // ---- pages --------------------------------------------------------------------------------------------------------
  const figures = {};
  function buildPages() {
    const root = $("#pages");
    root.replaceChildren();
    D.pages.forEach((page, index) => {
      const figure = el("div", {className: "page" + (page.required ? "" : " optional"), id: "page-" + page.id});
      figure.append(el("img", {src: page.image, alt: page.label, width: page.width, height: page.height,
                               loading: index < 2 ? "eager" : "lazy", draggable: false}));
      for (const panel of page.panels) {
        const [x, y, w, h] = panel.frame;
        const box = el("div", {className: "panel", title: panel.refs});
        Object.assign(box.style, {left: x * 100 + "%", top: y * 100 + "%", width: w * 100 + "%", height: h * 100 + "%"});
        panel.node = box;
        figure.append(box);
      }
      const pins = el("div", {className: "pins"});
      figure.append(pins);
      if (!readonly) figure.addEventListener("click", event => {
        if (event.target.closest(".pin")) return;
        const rect = figure.getBoundingClientRect();
        startNew(page, (event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height);
      });
      const foot = el("div", {className: "page-foot"});
      root.append(el("div", {className: "page-wrap"}, [
        el("span", {className: "page-label", textContent: page.label + (page.required ? "" : " · unchanged since the last review, no need to check")}),
        figure, foot]));
      figures[page.id] = {figure, pins, foot};
    });
  }

  function renderPages() {
    for (const page of D.pages) {
      const {pins, foot} = figures[page.id];
      const mine = issues.filter(i => i.page_id === page.id);
      pins.replaceChildren();
      for (const issue of mine.filter(i => i.x !== null)) {
        const pin = el("button", {className: "pin" + (issue.id === selected ? " selected" : ""), type: "button",
                                  textContent: number(issue), title: issue.description,
                                  ariaLabel: "Issue " + number(issue)});
        Object.assign(pin.style, {left: issue.x * 100 + "%", top: issue.y * 100 + "%"});
        pin.addEventListener("click", event => { event.stopPropagation(); select(issue.id); });
        pins.append(pin);
      }
      if (draft && draft.page_id === page.id && draft.x !== null) {
        const pin = el("div", {className: "pin draft", textContent: "+"});
        Object.assign(pin.style, {left: draft.x * 100 + "%", top: draft.y * 100 + "%"});
        pins.append(pin);
      }
      for (const panel of page.panels) panel.node.classList.toggle("flagged", mine.some(i => i.panel_id === panel.id));
      foot.replaceChildren();
      if (mine.length) {
        foot.append(el("button", {className: "tick issues", type: "button", disabled: true,
                                  textContent: mine.length + (mine.length === 1 ? " issue" : " issues") + " reported on this page"}));
      } else if (!readonly) {
        const tick = el("button", {className: "tick" + (checked.has(page.id) ? " on" : ""), type: "button",
                                   textContent: checked.has(page.id) ? "✓ This page looks good" : "This page looks good"});
        tick.addEventListener("click", () => toggleCheck(page));
        foot.append(tick);
      } else if (checked.has(page.id)) {
        foot.append(el("span", {className: "note", textContent: "✓ Marked as looking good"}));
      }
      if (!readonly) {
        const add = el("button", {className: "step", type: "button", textContent: "＋ Report a problem with this page"});
        add.addEventListener("click", () => startNew(page, null, null));
        foot.append(add);
      }
    }
  }

  async function toggleCheck(page) {
    const next = !checked.has(page.id);
    try {
      await api("PUT", `/api/review/${D.review.id}/pages/${page.id}/check`, {checked: next});
      next ? checked.add(page.id) : checked.delete(page.id);
      renderPages();   // not the editor: someone may be mid-sentence in an issue
      renderSide();
    } catch (error) { flashStatus(error.message, true); }
  }

  // ---- editor -------------------------------------------------------------------------------------------------------
  function startNew(page, x, y) {
    draft = {page_id: page.id, x, y};
    selected = "new";
    renderAll();
    $("#description")?.focus({preventScroll: true});
  }

  function select(id) {
    draft = null;
    selected = id;
    renderAll();
    if (id !== null && id !== "new") {
      const issue = issues.find(i => i.id === id);
      const fig = figures[issue.page_id].figure;
      if (issue.x !== null) fig.scrollIntoView({behavior: "smooth", block: "center"});
    }
  }

  function flashStatus(text, isError = false) {
    const status = $("#status") || $("#submit-status");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("error", isError);
  }

  function versesFor(page, panel) {
    const panels = panel ? [panel] : page.panels;
    const seen = new Set(), list = [];
    for (const p of panels) for (const [ref, text] of p.verses) if (!seen.has(ref) && text) { seen.add(ref); list.push([ref, text]); }
    return list;
  }

  function renderEditor() {
    const editor = $("#editor");
    if (selected === null) {
      editor.replaceChildren(el("p", {className: "hint", style: "margin:0",
        textContent: readonly ? "Click a numbered pin to read an issue."
          : "Scroll through the pages. Click right on the art where something is wrong to drop a pin, or tick “This page looks good” under each page that is fine."}));
      return;
    }
    const issue = selected === "new" ? null : issues.find(i => i.id === selected);
    const source = issue || draft;
    const page = pageById(source.page_id);
    const panel = issue ? page.panels.find(p => p.id === issue.panel_id) : (draft.x !== null ? locate(page, draft.x, draft.y) : null);
    const values = issue || {category: "", severity: "", description: "", suggestion: "", verse_ref: ""};

    const category = el("select", {id: "category", disabled: readonly}, [
      el("option", {value: "", textContent: "What kind of problem is it?"}),
      ...D.categories.map(c => el("option", {value: c.key, textContent: c.label, selected: c.key === values.category}))]);
    const help = el("p", {className: "help"});
    const showHelp = () => { const c = D.categories.find(x => x.key === category.value); help.textContent = c ? c.help : ""; };
    category.addEventListener("change", showHelp);
    showHelp();

    const chips = el("div", {className: "chips", role: "radiogroup", ariaLabel: "How serious is it?"});
    for (const s of D.severities) {
      const input = el("input", {type: "radio", name: "severity", id: "sev-" + s.key, value: s.key, checked: s.key === values.severity, disabled: readonly});
      chips.append(input, el("label", {htmlFor: input.id, textContent: s.label, title: s.help}));
    }
    const description = el("textarea", {id: "description", value: values.description, disabled: readonly,
                                        placeholder: "What is wrong? Be as specific as you can."});
    const suggestion = el("textarea", {id: "suggestion", value: values.suggestion, disabled: readonly,
                                       placeholder: "Optional: how should it look or read instead?"});
    const verse = el("input", {id: "verse_ref", type: "text", value: values.verse_ref || (issue ? "" : panel ? panel.refs : ""), disabled: readonly,
                               placeholder: "Optional, e.g. 1 Nephi 1:5"});
    const status = el("span", {className: "status", id: "status"});

    const save = el("button", {className: "btn primary", type: "button", textContent: issue ? "Save changes" : "Save issue"});
    const cancel = el("button", {className: "btn", type: "button", textContent: readonly ? "Close" : "Cancel"});
    const remove = issue && !readonly ? el("button", {className: "btn danger", type: "button", textContent: "Delete"}) : null;
    cancel.addEventListener("click", () => select(null));
    save.addEventListener("click", () => submitIssue(issue, page, {
      category: category.value, severity: (document.querySelector('input[name="severity"]:checked') || {}).value || "",
      description: description.value, suggestion: suggestion.value, verse_ref: verse.value}, status));
    remove?.addEventListener("click", () => removeIssue(issue, status));

    const verses = versesFor(page, panel);
    const scripture = verses.length ? el("details", {className: "verses"}, [
      el("summary", {textContent: "Show scripture text"}),
      ...verses.map(([ref, text]) => el("p", {}, [el("b", {textContent: ref}), text]))]) : null;

    const heading = el("div", {className: "meta"}, [
      issue ? "Issue " + number(issue) + " " : "New issue ",
      el("span", {textContent: "· " + where(page, panel) + (panel && panel.refs ? " · " + panel.refs : "")})]);
    editor.replaceChildren(heading,
      el("label", {htmlFor: "category", textContent: "Type of problem"}), category, help,
      el("label", {textContent: "How serious is it?"}), chips,
      el("label", {htmlFor: "description", textContent: "What is wrong?"}), description,
      el("label", {htmlFor: "suggestion", textContent: "Suggested correction (optional)"}), suggestion,
      el("label", {htmlFor: "verse_ref", textContent: "Verse (optional)"}), verse,
      ...(scripture ? [scripture] : []),
      el("div", {className: "row", style: "margin-top:14px"}, [...(readonly ? [] : [save]), cancel, ...(remove ? [remove] : [])]),
      status,
      ...(readonly ? [] : [el("div", {className: "keys", textContent: "Ctrl+Enter saves · Esc closes"})]));
  }

  async function submitIssue(issue, page, fields, status) {
    status.textContent = "Saving…";
    status.classList.remove("error");
    try {
      if (issue) {
        const body = {...fields, x: issue.x, y: issue.y};
        const {issue: saved} = await api("PUT", `/api/review/${D.review.id}/issues/${issue.id}`, body);
        issues = issues.map(i => i.id === saved.id ? saved : i);
        selected = saved.id;
      } else {
        const body = {...fields, page_id: page.id, x: draft.x, y: draft.y};
        const {issue: saved} = await api("POST", `/api/review/${D.review.id}/issues`, body);
        issues.push(saved);
        checked.delete(page.id);
        draft = null;
        selected = saved.id;
      }
      renderAll();
      flashStatus("Saved");
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    }
  }

  async function removeIssue(issue, status) {
    if (!confirm("Delete this issue?")) return;
    try {
      await api("DELETE", `/api/review/${D.review.id}/issues/${issue.id}`);
      issues = issues.filter(i => i.id !== issue.id);
      selected = null;
      renderAll();
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    }
  }

  // ---- side panel ---------------------------------------------------------------------------------------------------
  function renderSide() {
    const total = required().length, left = remaining().length;
    $("#summary").textContent = `${total - left} of ${total} pages checked · ${issues.length} issue${issues.length === 1 ? "" : "s"}`;
    $("#progress-bar").style.width = total ? (100 * (total - left) / total) + "%" : "0";
    $("#next-todo").hidden = readonly || !left;
    const list = $("#issue-list");
    if (!issues.length) {
      list.replaceChildren(el("p", {className: "hint", textContent: "None yet."}));
    } else {
      list.replaceChildren(el("ul", {className: "issues"}, issues.map(issue => {
        const page = pageById(issue.page_id);
        const button = el("button", {type: "button", className: issue.id === selected ? "selected" : ""}, [
          el("strong", {textContent: number(issue) + ". " + where(page, page.panels.find(p => p.id === issue.panel_id))}),
          el("span", {className: "note", textContent: issue.description.length > 90 ? issue.description.slice(0, 90) + "…" : issue.description})]);
        button.addEventListener("click", () => select(issue.id));
        return el("li", {}, button);
      })));
    }
    const submit = $("#submit");
    if (submit) {
      submit.disabled = left > 0;
      $("#submit-hint").textContent = left
        ? `${left} page${left === 1 ? "" : "s"} still to check: tick “This page looks good” or report a problem on each.`
        : "Every page is checked. You can submit when you are ready. You cannot change it afterwards.";
    }
  }

  function renderAll() { renderPages(); renderEditor(); renderSide(); }

  $("#next-todo").addEventListener("click", () => {
    const next = remaining()[0];
    if (next) figures[next.id].figure.scrollIntoView({behavior: "smooth", block: "center"});
  });

  $("#submit")?.addEventListener("click", async () => {
    if (!confirm("Submit this review? You will not be able to change it afterwards.")) return;
    const button = $("#submit");
    button.disabled = true;
    try {
      const {redirect} = await api("POST", `/api/review/${D.review.id}/submit`);
      location.href = redirect;
    } catch (error) {
      button.disabled = false;
      flashStatus(error.message, true);
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && selected !== null) select(null);
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && selected !== null && !readonly) $("#editor .btn.primary")?.click();
  });

  // Count time spent (for the editors' sanity checks) and keep the chapter held while someone is actually working.
  for (const name of ["mousemove", "keydown", "scroll", "click", "touchstart"]) {
    window.addEventListener(name, () => { lastActive = Date.now(); }, {passive: true});
  }
  if (!readonly) {
    setInterval(() => {
      if (document.visibilityState !== "visible" || Date.now() - lastActive > 120000) return;
      api("POST", `/api/review/${D.review.id}/heartbeat`, {seconds: 30}).catch(() => {});
    }, 30000);
  }

  buildPages();
  renderAll();
})();

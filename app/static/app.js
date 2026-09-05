(function () {
  "use strict";

  const API_BASE = "/api";
  const STEPS = ["company", "customer", "items", "review", "result"];
  const STEP_TITLES = {
    company: "Company", customer: "Customer", items: "Items",
    review: "Review", result: "Done",
  };

  // Browsing issued invoices is a branch off the wizard, not another step in
  // it: these screens have no linear order and must not disturb the "Step N
  // of 4" numbering. state.view names the active branch screen, or null when
  // the wizard is in charge.
  const VIEW_SCREENS = [
    "mode", "invoiceList", "invoiceDetail", "invoiceEdit", "invoiceEditConfirm",
  ];
  const VIEW_TITLES = {
    mode: "Quick Invoice", invoiceList: "Recent Invoices", invoiceDetail: "Invoice",
    invoiceEdit: "Edit Lines", invoiceEditConfirm: "Confirm Changes",
  };
  // Where Back goes from each branch screen; null returns to the wizard.
  const VIEW_BACK = {
    mode: null,
    invoiceList: "mode",
    invoiceDetail: "invoiceList",
    invoiceEdit: "invoiceDetail",
    invoiceEditConfirm: "invoiceEdit",
  };
  const ALL_SCREENS = STEPS.concat(VIEW_SCREENS);

  // Two different windows, and they must not be confused: the list shows the
  // last few days, while an invoice stays editable for far longer. Both are
  // the server's rules — mirrored here only for the labels, since the server
  // decides what is listed and what is_editable.
  const LIST_WINDOW_DAYS = 2;
  const EDIT_WINDOW_DAYS = 30;

  const state = {
    stepIndex: 0,
    view: null,             // null = wizard, else one of VIEW_SCREENS
    company: null,          // { key, name }
    customer: null,         // { id, code, name }
    address: null,          // { id, label, address_text }
    invoiceDate: todayISO(),
    lines: [],              // [{ item_id, code, name, quantity, unit_price, original_unit_price }]
    idempotencyKey: null,
    issuing: false,
    issueResult: null,      // { invoice_number, ... } | { error }
    invoices: [],           // recent invoice list rows
    viewInvoice: null,      // the invoice open on the detail screen
    loadingInvoices: false,
    editDocNo: null,        // the invoice being edited
    editOriginal: [],       // its line set as loaded, sent as expected_lines
    editLines: [],          // the desired line set being built
    saving: false,
  };

  function todayISO() {
    const d = new Date();
    return d.toISOString().slice(0, 10);
  }

  function uuidv4() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    // Fallback for older WebKit without crypto.randomUUID
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // ---------- API helpers ----------

  async function apiGet(path) {
    const res = await fetch(API_BASE + path);
    const body = await safeJson(res);
    if (!res.ok) throw apiError(body, res.status);
    return body.data;
  }

  async function apiPost(path, payload) {
    const res = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await safeJson(res);
    if (!res.ok) throw apiError(body, res.status);
    return body.data;
  }

  async function apiPut(path, payload) {
    const res = await fetch(API_BASE + path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await safeJson(res);
    if (!res.ok) throw apiError(body, res.status);
    return body.data;
  }

  async function safeJson(res) {
    try { return await res.json(); } catch (e) { return {}; }
  }

  function apiError(body, status) {
    const err = new Error((body && (body.message || body.error)) || ("Request failed (" + status + ")"));
    err.status = status;
    err.body = body;
    return err;
  }

  function debounce(fn, ms) {
    // The timer lives in this closure, not on the module: the customer and
    // item searches debounce independently, so typing in one no longer
    // cancels a pending keystroke in the other.
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // AutoCount returns docDate as a datetime ("2026-08-13T00:00:00"); the time
  // half is always midnight and is noise on screen. Display only — the server
  // echoes docDate back to AutoCount verbatim, never this trimmed form.
  function dateOnly(value) {
    return String(value == null ? "" : value).slice(0, 10);
  }

  // Who the invoice was issued to. A bare debtor code means nothing to whoever
  // is reading the screen, so the name wins wherever an invoice names its
  // customer; the code is the fallback for when AutoCount returned no name.
  function debtorLabel(invoice) {
    return invoice.debtor_name || invoice.debtor_code;
  }

  function money(value) {
    const n = typeof value === "string" ? parseFloat(value) : value;
    if (Number.isNaN(n)) return "0.00";
    return n.toFixed(2);
  }

  function decMul(qtyStr, priceStr) {
    const q = parseFloat(qtyStr || "0");
    const p = parseFloat(priceStr || "0");
    if (Number.isNaN(q) || Number.isNaN(p)) return 0;
    return q * p;
  }

  // ---------- Rendering ----------

  const screenEls = {};
  ALL_SCREENS.forEach((s) => { screenEls[s] = document.querySelector('[data-screen="' + s + '"]'); });

  const headerTitle = document.getElementById("header-title");
  const stepPill = document.getElementById("step-pill");
  const bannerSlot = document.getElementById("banner-slot");
  const backBtn = document.getElementById("back-btn");
  const nextBtn = document.getElementById("next-btn");
  const actionbar = document.getElementById("actionbar");

  function showBanner(message, kind) {
    bannerSlot.innerHTML = "";
    if (!message) return;
    const div = document.createElement("div");
    div.className = "banner " + (kind || "error");
    div.textContent = message;
    bannerSlot.appendChild(div);
  }

  function render() {
    const current = state.view || STEPS[state.stepIndex];
    ALL_SCREENS.forEach((s) => screenEls[s].classList.toggle("active", s === current));

    if (state.view) {
      renderViewScreen(current);
      return;
    }

    headerTitle.textContent = STEP_TITLES[current];
    stepPill.textContent = current === "result" ? "Done" : "Step " + (state.stepIndex + 1) + " of 4";
    stepPill.style.display = current === "result" ? "none" : "inline-block";

    backBtn.style.display = (current === "company" || current === "result") ? "none" : "block";
    nextBtn.style.display = "block";
    nextBtn.textContent = current === "review" ? "Issue Invoice" : "Next";
    nextBtn.disabled = !canAdvance(current);
    actionbar.style.display = current === "result" ? "none" : "flex";

    if (current === "company") renderCompanyScreen();
    if (current === "customer") renderCustomerScreen();
    if (current === "items") renderItemsScreen();
    if (current === "review") renderReviewScreen();
    if (current === "result") renderResultScreen();
  }

  // Most branch screens navigate by tapping a card or a row, so the wizard's
  // Next button has nothing to do and is hidden rather than left dead on
  // screen. The two edit screens are the exception: they advance.
  function renderViewScreen(current) {
    headerTitle.textContent = VIEW_TITLES[current];
    stepPill.style.display = "none";
    backBtn.style.display = "block";
    actionbar.style.display = "flex";

    const advances = current === "invoiceEdit" || current === "invoiceEditConfirm";
    nextBtn.style.display = advances ? "block" : "none";
    if (advances) {
      nextBtn.textContent = current === "invoiceEdit" ? "Review changes" : "Save changes";
      nextBtn.disabled = !canSaveEdit(current);
    }

    if (current === "mode") renderModeScreen();
    if (current === "invoiceList") renderInvoiceList();
    if (current === "invoiceDetail") renderInvoiceDetail();
    if (current === "invoiceEdit") renderEditScreen();
    if (current === "invoiceEditConfirm") renderEditConfirmScreen();
  }

  function canSaveEdit(screen) {
    if (state.saving) return false;
    if (!state.editLines.length) return false;
    if (!linesValid(state.editLines)) return false;
    // Nothing to save if the line set is untouched — the server would reject
    // it as a no-op edit anyway, and it wastes a live write.
    if (screen === "invoiceEditConfirm") return editHasChanges();
    return true;
  }

  function editHasChanges() {
    if (state.editLines.length !== state.editOriginal.length) return true;
    return state.editLines.some((line, i) => {
      const prior = state.editOriginal[i];
      return (
        prior.item_id !== line.item_id ||
        parseFloat(prior.quantity) !== parseFloat(line.quantity) ||
        parseFloat(prior.unit_price) !== parseFloat(line.unit_price)
      );
    });
  }

  function canAdvance(screen) {
    if (screen === "company") return !!state.company;
    if (screen === "customer") return !!(state.customer && state.address);
    if (screen === "items") return state.lines.length > 0 && linesValid(state.lines);
    if (screen === "review") return !state.issuing;
    return true;
  }

  // ---------- Item picker ----------

  // The wizard and the edit screen both search the same product endpoint and
  // render the same result rows; only where they draw and what a tap does
  // differ. Wiring both from here keeps the two from drifting apart -- an
  // endpoint or markup change lands on both at once. Returns a handle so a
  // caller can close the picker on navigation.
  function wireItemPicker(el, onPick) {
    // Each keystroke's response is stamped with a sequence number; a response
    // that arrives after a newer keystroke was already issued is stale and
    // must not overwrite the newer results on screen.
    let searchSeq = 0;
    const search = debounce(async function (q) {
      if (!state.company) return;
      const seq = ++searchSeq;
      el.list.innerHTML = '<div class="empty-hint">Searching...</div>';
      try {
        const results = await apiGet(
          "/" + state.company.key + "/products?q=" + encodeURIComponent(q)
        );
        if (seq !== searchSeq) return;
        el.list.innerHTML = "";
        if (!results.length) {
          el.list.innerHTML = '<div class="empty-hint">No items found</div>';
          return;
        }
        results.forEach((p) => {
          const item = document.createElement("div");
          item.className = "list-item";
          item.innerHTML =
            '<div class="primary">' + escapeHtml(p.name) + "</div>" +
            '<div class="secondary">' + escapeHtml(p.code) + " &middot; RM " +
            money(p.default_price) + "</div>";
          item.onclick = () => {
            hide();
            onPick(p);
          };
          el.list.appendChild(item);
        });
      } catch (e) {
        if (seq !== searchSeq) return;
        el.list.innerHTML = "";
        showBanner("Item search failed: " + e.message);
      }
    }, 300);

    function hide() {
      el.picker.style.display = "none";
    }

    el.toggle.addEventListener("click", () => {
      const opening = el.picker.style.display === "none";
      el.picker.style.display = opening ? "block" : "none";
      el.input.value = "";
      el.list.innerHTML = "";
      if (opening) el.input.focus();
    });
    el.input.addEventListener("input", (e) => search(e.target.value));

    return { hide };
  }

  // A new invoice's lines and an edited invoice's lines are the same shape and
  // carry the same rule: a positive quantity and a non-negative price. One
  // check, so the wizard and the edit screen cannot disagree about what is
  // saveable.
  function linesValid(lines) {
    return lines.every((l) => {
      const q = parseFloat(l.quantity);
      const p = parseFloat(l.unit_price);
      return q > 0 && p >= 0 && !Number.isNaN(q) && !Number.isNaN(p);
    });
  }

  // ---------- Screen 1: Company ----------

  async function loadCompanies() {
    const grid = document.getElementById("company-grid");
    grid.innerHTML = '<div class="empty-hint">Loading...</div>';
    try {
      const companies = await apiGet("/companies");
      grid.innerHTML = "";
      companies.forEach((c) => {
        const card = document.createElement("div");
        card.className = "choice-card" + (state.company && state.company.key === c.key ? " selected" : "");
        card.textContent = c.name;
        card.onclick = () => {
          state.company = c;
          render();
        };
        grid.appendChild(card);
      });
    } catch (e) {
      grid.innerHTML = "";
      showBanner("Could not load companies: " + e.message);
    }
  }

  function renderCompanyScreen() {
    const grid = document.getElementById("company-grid");
    [...grid.children].forEach((card) => {
      const label = card.textContent;
      card.classList.toggle("selected", !!(state.company && state.company.name === label));
    });
  }

  // ---------- Branch: new invoice or browse issued ones ----------

  function renderModeScreen() {
    document.getElementById("mode-context").innerHTML =
      "<b>" + escapeHtml(state.company ? state.company.name : "") + "</b>";
  }

  document.getElementById("mode-new").addEventListener("click", () => {
    state.view = null;
    state.stepIndex = STEPS.indexOf("customer");
    showBanner(null);
    render();
  });

  document.getElementById("mode-view").addEventListener("click", () => {
    state.view = "invoiceList";
    showBanner(null);
    render();
    loadInvoiceList();
  });

  // ---------- Branch: recent invoices ----------

  const invoiceListEl = document.getElementById("invoice-list");

  async function loadInvoiceList() {
    if (!state.company) return;
    state.loadingInvoices = true;
    invoiceListEl.innerHTML = '<div class="empty-hint">Loading...</div>';
    try {
      state.invoices = await apiGet("/" + state.company.key + "/invoices");
    } catch (e) {
      state.invoices = [];
      showBanner("Could not load invoices: " + e.message);
    } finally {
      state.loadingInvoices = false;
    }
    if (state.view === "invoiceList") renderInvoiceList();
  }

  function renderInvoiceList() {
    document.getElementById("invoice-list-context").innerHTML =
      "<b>" + escapeHtml(state.company ? state.company.name : "") + "</b>";
    document.getElementById("invoice-list-window").textContent =
      "Last " + LIST_WINDOW_DAYS + " days";

    if (state.loadingInvoices) {
      invoiceListEl.innerHTML = '<div class="empty-hint">Loading...</div>';
      return;
    }
    if (!state.invoices.length) {
      invoiceListEl.innerHTML =
        '<div class="empty-hint">No invoices issued in the last ' +
        LIST_WINDOW_DAYS + " days.</div>";
      return;
    }
    invoiceListEl.innerHTML = state.invoices
      .map(function (inv, index) {
        return (
          '<div class="list-item" data-invoice-index="' + index + '">' +
          '<div class="primary">' + escapeHtml(inv.doc_no) +
          (inv.is_cancelled ? '<span class="badge">Cancelled</span>' : "") +
          "</div>" +
          '<div class="secondary">' + escapeHtml(dateOnly(inv.doc_date)) + " &middot; " +
          escapeHtml(debtorLabel(inv)) + " &middot; RM " + money(inv.total) +
          " &middot; " + inv.line_count + (inv.line_count === 1 ? " line" : " lines") +
          "</div></div>"
        );
      })
      .join("");
  }

  invoiceListEl.addEventListener("click", (event) => {
    const row = event.target.closest("[data-invoice-index]");
    if (!row) return;
    const invoice = state.invoices[Number(row.dataset.invoiceIndex)];
    if (invoice) openInvoice(invoice.doc_no);
  });

  // ---------- Branch: one issued invoice ----------

  async function openInvoice(docNo) {
    showBanner(null);
    try {
      state.viewInvoice = await apiGet(
        "/" + state.company.key + "/invoices/" + encodeURIComponent(docNo)
      );
      state.view = "invoiceDetail";
      render();
    } catch (e) {
      showBanner("Could not open " + docNo + ": " + e.message);
    }
  }

  function renderInvoiceDetail() {
    const inv = state.viewInvoice;
    if (!inv) return;

    document.getElementById("invoice-detail-head").innerHTML =
      '<div class="selected-summary"><b>' + escapeHtml(inv.doc_no) + "</b>" +
      (inv.is_cancelled ? '<span class="badge">Cancelled</span>' : "") +
      "<br />" + escapeHtml(debtorLabel(inv)) +
      "<br />" + escapeHtml(dateOnly(inv.doc_date)) +
      "</div>";

    document.getElementById("invoice-detail-lines").innerHTML = inv.lines
      .map(function (line) {
        return (
          '<div class="line-card"><div class="line-head">' +
          '<div class="line-title">' + escapeHtml(line.product_code) + "</div>" +
          '<div class="line-total">RM ' + money(decMul(line.quantity, line.unit_price)) + "</div>" +
          "</div>" +
          '<div class="line-sub">' + escapeHtml(line.description) + "</div>" +
          '<div class="line-sub">' + escapeHtml(line.quantity) + " &times; RM " +
          money(line.unit_price) + "</div></div>"
        );
      })
      .join("");

    document.getElementById("invoice-detail-total").textContent = "RM " + money(inv.total);

    // The actions block is rendered into the scoped detail container so its
    // controls never collide with the post-issue result screen. Cancelled and
    // old invoices keep Cloud access because it is read-only; only "Edit lines"
    // is gated on is_editable.
    const actionsEl = document.getElementById("invoice-detail-actions");
    let html = inv.is_editable
      ? '<button type="button" class="add-item-btn" id="edit-invoice-btn">Edit lines</button>'
      : '<div class="readonly-note">' +
        (inv.is_cancelled
          ? "This invoice is cancelled and cannot be changed."
          : "This invoice is more than " + EDIT_WINDOW_DAYS +
            " days old. Correct it in AutoCount directly.") +
        "</div>";
    // The server (not the browser) resolves the Cloud URL from the confirmed
    // AutoCount docKey; the client only hands over company + doc_no.
    html +=
      '<div class="result-actions detail-cloud-actions">' +
      '<button type="button" class="btn btn-primary" id="detail-open-cloud-report-btn">Open Cloud Report</button>' +
      '</div>' +
      '<div class="status-note">Print, Export PDF, and Share are done in the AutoCount Cloud report screen.</div>';
    actionsEl.innerHTML = html;

    const editBtn = document.getElementById("edit-invoice-btn");
    if (editBtn) editBtn.onclick = startEdit;

    document.getElementById("detail-open-cloud-report-btn").onclick = () => {
      const url = "/api/" + encodeURIComponent(state.company.key) +
        "/invoices/" + encodeURIComponent(inv.doc_no) + "/cloud-report";
      window.open(url, "_blank", "noopener,noreferrer");
    };
  }

  // ---------- Branch: edit an issued invoice's lines ----------

  function startEdit() {
    const inv = state.viewInvoice;
    if (!inv || !inv.is_editable) return;
    state.editDocNo = inv.doc_no;
    // The line set exactly as loaded. Sent back as expected_lines so the
    // server can refuse a save built on a view that has since gone stale.
    state.editOriginal = inv.lines.map((line) => ({
      item_id: line.product_code,
      quantity: line.quantity,
      unit_price: line.unit_price,
    }));
    state.editLines = inv.lines.map((line) => ({
      item_id: line.product_code,
      description: line.description,
      quantity: line.quantity,
      unit_price: line.unit_price,
    }));
    state.view = "invoiceEdit";
    showBanner(null);
    editItemPicker.hide();
    render();
  }

  const editLineListEl = document.getElementById("edit-line-list");

  const editItemPicker = wireItemPicker(
    {
      toggle: document.getElementById("edit-add-item-btn"),
      picker: document.getElementById("edit-item-picker"),
      input: document.getElementById("edit-item-search"),
      list: document.getElementById("edit-item-search-list"),
    },
    // Appended, not inserted: a new row lands at the end of the array, which
    // is exactly where AutoCount will add it.
    (p) => {
      state.editLines.push({
        item_id: p.id,
        description: p.name,
        quantity: "1",
        unit_price: p.default_price,
      });
      render();
    }
  );

  function renderEditScreen() {
    document.getElementById("edit-context").innerHTML =
      "<b>" + escapeHtml(state.editDocNo) + "</b><br />" +
      "Changing lines only. The invoice date and customer stay as they are.";

    editLineListEl.innerHTML = "";
    state.editLines.forEach((line, index) => {
      const card = document.createElement("div");
      card.className = "line-card";
      card.innerHTML =
        '<div class="line-head">' +
          '<div><div class="line-title">' + escapeHtml(line.item_id) + "</div>" +
          '<div class="line-sub">' + escapeHtml(line.description || "") + "</div></div>" +
          // An invoice must keep at least one line; the server rejects an
          // empty set, so the last Remove is disabled rather than failing.
          '<button class="remove-btn" data-idx="' + index + '"' +
            (state.editLines.length === 1 ? " disabled" : "") + ">Remove</button>" +
        "</div>" +
        '<div class="qty-price-row">' +
          '<div><label>Quantity</label><input type="number" inputmode="decimal" min="0" step="any" class="qty-input" data-idx="' + index + '" value="' + escapeHtml(line.quantity) + '" /></div>' +
          '<div><label>Unit Price (RM)</label><input type="number" inputmode="decimal" min="0" step="any" class="price-input" data-idx="' + index + '" value="' + escapeHtml(line.unit_price) + '" /></div>' +
        "</div>" +
        '<div class="line-total">RM ' + money(decMul(line.quantity, line.unit_price)) + "</div>";
      editLineListEl.appendChild(card);
    });

    editLineListEl.querySelectorAll(".remove-btn").forEach((btn) => {
      btn.onclick = () => {
        state.editLines.splice(parseInt(btn.dataset.idx, 10), 1);
        render();
      };
    });
    wireLineInputs(editLineListEl, state.editLines, () => {
      nextBtn.disabled = !canSaveEdit("invoiceEdit");
    });
  }

  // ---------- Branch: confirm the change ----------

  function renderEditConfirmScreen() {
    document.getElementById("edit-confirm-context").innerHTML =
      "<b>" + escapeHtml(state.editDocNo) + "</b>";

    const before = state.editOriginal;
    const after = state.editLines;
    const rows = [];

    after.forEach((line, index) => {
      const prior = before[index];
      if (!prior || prior.item_id !== line.item_id) {
        rows.push(diffRow("added", "+ " + line.item_id + "  " +
          line.quantity + " × RM " + money(line.unit_price)));
      } else if (
        parseFloat(prior.quantity) !== parseFloat(line.quantity) ||
        parseFloat(prior.unit_price) !== parseFloat(line.unit_price)
      ) {
        rows.push(diffRow("changed", "~ " + line.item_id + "  " +
          prior.quantity + " × RM " + money(prior.unit_price) + "  →  " +
          line.quantity + " × RM " + money(line.unit_price)));
      }
    });
    before.slice(after.length).forEach((line) => {
      rows.push(diffRow("removed", "− " + line.item_id + " removed"));
    });

    document.getElementById("edit-diff").innerHTML =
      rows.length ? rows.join("") : '<div class="readonly-note">No changes.</div>';

    const total = after.reduce(
      (sum, line) => sum + decMul(line.quantity, line.unit_price), 0
    );
    document.getElementById("edit-new-total").textContent = "RM " + money(total);
  }

  function diffRow(kind, text) {
    return '<div class="diff-row ' + kind + '">' + escapeHtml(text) + "</div>";
  }

  // Both halves of the edit body are the same shape, and both send money as
  // strings so the server parses exact decimals rather than binary floats.
  function editPayloadLines(lines) {
    return lines.map((line) => ({
      item_id: line.item_id,
      quantity: String(line.quantity),
      unit_price: String(line.unit_price),
    }));
  }

  async function saveEdit() {
    if (state.saving) return;
    state.saving = true;
    nextBtn.disabled = true;
    nextBtn.innerHTML = '<span class="spinner"></span> Saving...';
    try {
      const data = await apiPut(
        "/" + state.company.key + "/invoices/" + encodeURIComponent(state.editDocNo),
        {
          company: state.company.key,
          expected_lines: editPayloadLines(state.editOriginal),
          lines: editPayloadLines(state.editLines),
        }
      );
      state.viewInvoice = data;
      state.view = "invoiceDetail";
      render();
      showBanner("Invoice " + data.doc_no + " updated", "success");
    } catch (e) {
      // invoice_changed and edit_unconfirmed both mean the on-screen line set
      // can no longer be trusted, so reload rather than leave the user staring
      // at a form the server has already rejected.
      const code = e.body && e.body.error;
      if (code === "invoice_changed" || code === "edit_unconfirmed") {
        showBanner(e.message, "error");
        await openInvoice(state.editDocNo);
      } else {
        showBanner(e.message || "Could not save the changes", "error");
      }
    } finally {
      state.saving = false;
      nextBtn.innerHTML = "Save changes";
      nextBtn.disabled = !canSaveEdit("invoiceEditConfirm");
    }
  }

  // ---------- Screen 2: Customer + address ----------

  const customerSearchInput = document.getElementById("customer-search");
  const customerListEl = document.getElementById("customer-list");
  const addressSection = document.getElementById("address-section");
  const addressListEl = document.getElementById("address-list");
  const customerSearchBlock = document.getElementById("customer-search-block");
  const customerSelectedBlock = document.getElementById("customer-selected-block");
  const selectedCustomerName = document.getElementById("selected-customer-name");
  const selectedCustomerCode = document.getElementById("selected-customer-code");
  const changeCustomerBtn = document.getElementById("change-customer-btn");

  changeCustomerBtn.addEventListener("click", () => {
    state.customer = null;
    state.address = null;
    addressListEl.innerHTML = "";
    addressSection.style.display = "none";
    render();
    customerSearchInput.focus();
  });

  // Same stale-response guard as the item picker: only the newest
  // keystroke's response may render.
  let customerSearchSeq = 0;
  const doCustomerSearch = debounce(async function (q) {
    if (!state.company) return;
    const seq = ++customerSearchSeq;
    customerListEl.innerHTML = '<div class="empty-hint">Searching...</div>';
    try {
      const results = await apiGet("/" + state.company.key + "/customers?q=" + encodeURIComponent(q));
      if (seq !== customerSearchSeq) return;
      customerListEl.innerHTML = "";
      if (results.length === 0) {
        customerListEl.innerHTML = '<div class="empty-hint">No customers found</div>';
        return;
      }
      results.forEach((c) => {
        const item = document.createElement("div");
        item.className = "list-item" + (state.customer && state.customer.id === c.id ? " selected" : "");
        item.innerHTML = '<div class="primary">' + escapeHtml(c.name) + '</div><div class="secondary">' + escapeHtml(c.code) + "</div>";
        item.onclick = () => selectCustomer(c);
        customerListEl.appendChild(item);
      });
    } catch (e) {
      if (seq !== customerSearchSeq) return;
      customerListEl.innerHTML = "";
      showBanner("Customer search failed: " + e.message);
    }
  }, 300);

  customerSearchInput.addEventListener("input", (e) => doCustomerSearch(e.target.value));

  async function selectCustomer(customer) {
    state.customer = customer;
    state.address = null;
    render();
    addressListEl.innerHTML = '<div class="empty-hint">Loading addresses...</div>';
    addressSection.style.display = "block";
    try {
      const addresses = await apiGet("/" + state.company.key + "/customers/" + encodeURIComponent(customer.id) + "/addresses");
      addressListEl.innerHTML = "";
      if (addresses.length === 0) {
        addressListEl.innerHTML = '<div class="empty-hint">No delivery addresses on file</div>';
        return;
      }
      addresses.forEach((a) => {
        const item = document.createElement("div");
        item.className = "list-item";
        item.innerHTML = '<div class="primary">' + escapeHtml(a.label) + '</div><div class="secondary">' + escapeHtml(a.address_text) + "</div>";
        item.onclick = () => {
          state.address = a;
          render();
        };
        addressListEl.appendChild(item);
      });
    } catch (e) {
      addressListEl.innerHTML = "";
      showBanner("Could not load addresses: " + e.message);
    }
  }

  function renderCustomerScreen() {
    if (state.customer) {
      customerSearchBlock.style.display = "none";
      customerSelectedBlock.style.display = "block";
      selectedCustomerName.textContent = state.customer.name;
      selectedCustomerCode.textContent = state.customer.code;
      addressSection.style.display = "block";
    } else {
      customerSearchBlock.style.display = "block";
      customerSelectedBlock.style.display = "none";
      addressSection.style.display = "none";
    }
  }

  // ---------- Screen 3: Items ----------

  const itemsContext = document.getElementById("items-context");
  const invoiceDateInput = document.getElementById("invoice-date");
  const lineListEl = document.getElementById("line-list");
  const itemPicker = wireItemPicker(
    {
      toggle: document.getElementById("add-item-btn"),
      picker: document.getElementById("item-picker"),
      input: document.getElementById("item-search"),
      list: document.getElementById("item-search-list"),
    },
    (p) => addLine(p)
  );

  invoiceDateInput.value = state.invoiceDate;
  invoiceDateInput.addEventListener("change", (e) => { state.invoiceDate = e.target.value; });

  async function addLine(product) {
    const line = {
      item_id: product.id,
      code: product.code,
      name: product.name,
      quantity: "1",
      unit_price: product.default_price,
      original_unit_price: product.default_price,
      priceSource: "default",
    };
    state.lines.push(line);
    render();
    fetchPriceHistoryFor(line);
  }

  async function fetchPriceHistoryFor(line) {
    if (!state.customer) return;
    try {
      const data = await apiPost("/invoices/preview", {
        company: state.company.key,
        customer_id: state.customer.id,
        item_ids: [line.item_id],
      });
      const entry = (data.items || [])[0];
      if (entry && entry.latest_unit_price) {
        line.unit_price = entry.latest_unit_price;
        line.original_unit_price = entry.latest_unit_price;
        line.priceSource = "history";
        line.priceSourceLabel = "Last sold " + entry.source_invoice_date + " (" + entry.source_invoice_number + ")";
        renderItemsScreen();
      }
    } catch (e) {
      // Price history is advisory; silently keep the default price on failure.
    }
  }

  function removeLine(index) {
    state.lines.splice(index, 1);
    render();
  }

  function renderItemsScreen() {
    itemsContext.innerHTML =
      "<b>" + escapeHtml(state.company.name) + "</b> · " +
      escapeHtml(state.customer.name) + "<br>" +
      escapeHtml(state.address.label);

    lineListEl.innerHTML = "";
    state.lines.forEach((line, index) => {
      const card = document.createElement("div");
      card.className = "line-card";
      const total = decMul(line.quantity, line.unit_price);
      card.innerHTML =
        '<div class="line-head">' +
          '<div><div class="line-title">' + escapeHtml(line.name) + '</div>' +
          '<div class="line-sub">' + escapeHtml(line.code) + '</div></div>' +
          '<button class="remove-btn" data-idx="' + index + '">Remove</button>' +
        '</div>' +
        '<div class="qty-price-row">' +
          '<div><label>Quantity</label><input type="number" inputmode="decimal" min="0" step="any" class="qty-input" data-idx="' + index + '" value="' + line.quantity + '" /></div>' +
          '<div><label>Unit Price (RM)</label><input type="number" inputmode="decimal" min="0" step="any" class="price-input" data-idx="' + index + '" value="' + line.unit_price + '" /></div>' +
        '</div>' +
        (line.priceSourceLabel ? '<div class="price-hint">' + escapeHtml(line.priceSourceLabel) + '</div>' : '') +
        '<div class="line-total">RM ' + money(total) + '</div>';
      lineListEl.appendChild(card);
    });

    lineListEl.querySelectorAll(".remove-btn").forEach((btn) => {
      btn.onclick = () => removeLine(parseInt(btn.dataset.idx, 10));
    });
    wireLineInputs(lineListEl, state.lines, () => {
      nextBtn.disabled = !canAdvance("items");
    });
  }

  // Both line editors carry the same two inputs against the same line shape,
  // so they bind the same way: write the typed value straight back to the
  // line, re-check whether the screen can advance, and refresh that card's
  // total. ``onChange`` is the only difference -- which screen's rule decides.
  function wireLineInputs(listEl, lines, onChange) {
    listEl.querySelectorAll(".qty-input").forEach((input) => {
      input.oninput = () => {
        lines[parseInt(input.dataset.idx, 10)].quantity = input.value;
        onChange();
        updateLineTotal(input);
      };
    });
    listEl.querySelectorAll(".price-input").forEach((input) => {
      input.oninput = () => {
        const line = lines[parseInt(input.dataset.idx, 10)];
        line.unit_price = input.value;
        // A typed price is a manual override, so any inherited price hint no
        // longer describes it. The edit screen shows no hint; harmless there.
        line.priceSourceLabel = null;
        onChange();
        updateLineTotal(input);
      };
    });
  }

  function updateLineTotal(input) {
    const card = input.closest(".line-card");
    const qty = card.querySelector(".qty-input").value;
    const price = card.querySelector(".price-input").value;
    card.querySelector(".line-total").textContent = "RM " + money(decMul(qty, price));
  }

  // ---------- Screen 4: Review ----------

  const reviewContext = document.getElementById("review-context");
  const reviewLines = document.getElementById("review-lines");
  const reviewTotalAmount = document.getElementById("review-total-amount");

  function renderReviewScreen() {
    reviewContext.innerHTML =
      "<b>" + escapeHtml(state.company.name) + "</b> · " + escapeHtml(state.customer.name) + "<br>" +
      escapeHtml(state.address.label) + " · " + state.invoiceDate;

    reviewLines.innerHTML = "";
    let total = 0;
    state.lines.forEach((line) => {
      const lineTotal = decMul(line.quantity, line.unit_price);
      total += lineTotal;
      const row = document.createElement("div");
      row.className = "review-row";
      row.innerHTML =
        '<span class="label">' + escapeHtml(line.name) + " × " + line.quantity + "</span>" +
        "<span>RM " + money(lineTotal) + "</span>";
      reviewLines.appendChild(row);
    });
    reviewTotalAmount.textContent = "RM " + money(total);
  }

  // ---------- Screen 5: Result ----------

  const resultCard = document.getElementById("result-card");

  function renderResultScreen() {
    if (state.issueResult && state.issueResult.ok) {
      const r = state.issueResult.data;
      resultCard.innerHTML =
        '<div class="status-icon">✅</div>' +
        '<div class="status-title">Invoice issued</div>' +
        '<div class="status-detail">Invoice <b>' + escapeHtml(r.invoice_number) + '</b> was created in ' +
        escapeHtml(state.company.name) + '.</div>' +
        '<div class="status-note">e-Invoice request: ' +
        escapeHtml(r.einvoice && r.einvoice.status ? r.einvoice.status : "pending") +
        '. Check AutoCount Cloud for the validation result.</div>' +
        '<div class="result-actions">' +
        '<button class="btn btn-primary" id="open-cloud-report-btn">Open Cloud Report</button>' +
        '</div>' +
        '<div class="status-note">Print, Export PDF, and Share are done in the AutoCount Cloud report screen.</div>';

      const openBtn = document.getElementById("open-cloud-report-btn");
      openBtn.addEventListener("click", () => {
        const url = "/api/" + encodeURIComponent(state.company.key) +
          "/invoices/" + encodeURIComponent(r.invoice_number) + "/cloud-report";
        window.open(url, "_blank", "noopener,noreferrer");
      });
    } else if (state.issueResult && !state.issueResult.ok) {
      resultCard.innerHTML =
        '<div class="status-icon">⚠️</div>' +
        '<div class="status-title">Could not issue invoice</div>' +
        '<div class="status-detail">' + escapeHtml(state.issueResult.message) + '</div>';
    }
  }

  // ---------- Navigation ----------

  backBtn.addEventListener("click", () => {
    showBanner(null);
    if (state.view) {
      // Unwind the branch one screen at a time; leaving it lands back on the
      // company screen, which is where the branch was entered from.
      state.view = VIEW_BACK[state.view];
      if (!state.view) state.stepIndex = STEPS.indexOf("company");
      render();
      return;
    }
    if (state.stepIndex > 0) {
      state.stepIndex -= 1;
      render();
    }
  });

  nextBtn.addEventListener("click", async () => {
    if (state.view === "invoiceEdit") {
      showBanner(null);
      editItemPicker.hide();
      state.view = "invoiceEditConfirm";
      render();
      return;
    }
    if (state.view === "invoiceEditConfirm") {
      showBanner(null);
      await saveEdit();
      return;
    }

    const current = STEPS[state.stepIndex];
    showBanner(null);
    if (current === "company") {
      // Company chosen: ask what to do with it rather than assuming a new
      // invoice, now that issued ones can be browsed too.
      state.view = "mode";
      render();
      return;
    }
    if (current === "review") {
      await issueInvoice();
      return;
    }
    if (current === "items") {
      itemPicker.hide();
    }
    state.stepIndex += 1;
    render();
  });

  async function issueInvoice() {
    if (state.issuing) return;
    state.issuing = true;
    if (!state.idempotencyKey) state.idempotencyKey = uuidv4();
    nextBtn.disabled = true;
    nextBtn.innerHTML = '<span class="spinner"></span> Issuing...';

    const payload = {
      company: state.company.key,
      invoice_date: state.invoiceDate,
      customer_id: state.customer.id,
      delivery_address_id: state.address.id,
      lines: state.lines.map((l) => ({
        item_id: l.item_id,
        quantity: String(l.quantity),
        unit_price: String(l.unit_price),
        original_unit_price: String(l.original_unit_price),
      })),
      // Preview trial: request AutoCount e-Invoice submission for every
      // invoice. This is option 1 and intentionally remains preview-only.
      submit_einvoice: true,
      idempotency_key: state.idempotencyKey,
    };

    try {
      const data = await apiPost("/invoices", payload);
      state.issueResult = { ok: true, data };
      state.stepIndex = STEPS.indexOf("result");
      render();
    } catch (e) {
      // Same idempotency key is preserved so retrying reuses it — no
      // duplicate invoice on a second tap after a network hiccup.
      state.issueResult = { ok: false, message: e.message };
      state.stepIndex = STEPS.indexOf("result");
      render();
      // Let the user try again from the top; a fresh invoice needs a
      // fresh key only after a definite failure. Reset key on next visit
      // to items screen if they choose to start over.
    } finally {
      state.issuing = false;
      nextBtn.innerHTML = "Issue Invoice";
    }
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  // "Start another invoice" — long-press-free: tapping the header title
  // on the result screen resets state for a new entry.
  headerTitle.addEventListener("click", () => {
    if (state.view) return;
    if (STEPS[state.stepIndex] !== "result") return;
    const keepCompany = state.company;
    Object.assign(state, {
      stepIndex: 0, customer: null, address: null, invoiceDate: todayISO(),
      lines: [], idempotencyKey: null, issuing: false, issueResult: null,
      company: keepCompany,
    });
    invoiceDateInput.value = state.invoiceDate;
    customerSearchInput.value = "";
    customerListEl.innerHTML = "";
    addressListEl.innerHTML = "";
    addressSection.style.display = "none";
    render();
  });

  // ---------- Init ----------

  loadCompanies();
  render();
})();

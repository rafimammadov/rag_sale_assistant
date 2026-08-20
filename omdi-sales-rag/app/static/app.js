const query = new URLSearchParams(window.location.search);
const companySlug = query.get("company") || "yigit-aluminium";
const sessionKey = `omdi-session-${companySlug}`;
const cartKey = `omdi-cart-${companySlug}`;
const orderKey = `omdi-order-${companySlug}`;

const state = {
  company: null,
  customerSession: localStorage.getItem(sessionKey) || crypto.randomUUID(),
  conversationId: null,
  cart: null,
  order: null,
  adminKey: sessionStorage.getItem("omdi-admin-key") || "",
  reviewToken: query.get("review_token") || "",
};
localStorage.setItem(sessionKey, state.customerSession);

const $ = (selector) => document.querySelector(selector);
const messages = $("#messages");
const cartItems = $("#cart-items");
const toastRegion = $("#toast-region");

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function formatMoney(value, currency = "TRY") {
  if (value === null || value === undefined || value === "") return "Price not set";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function toast(message, kind = "info") {
  const node = document.createElement("div");
  node.className = `toast ${kind === "error" ? "error" : ""}`;
  node.textContent = message;
  toastRegion.append(node);
  setTimeout(() => node.remove(), 4500);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return body;
}

function adminHeaders() {
  return state.adminKey ? { "X-Admin-API-Key": state.adminKey } : {};
}

function setSystemStatus(text, online = false) {
  const node = $("#system-status");
  node.classList.toggle("online", online);
  node.querySelector("span").textContent = text;
}

async function loadCompany() {
  try {
    state.company = await api(`/api/companies/${companySlug}`);
    $("#company-name").textContent = state.company.name;
    $("#assistant-name").textContent = state.company.assistant_name;
    document.title = `${state.company.assistant_name} · ${state.company.name}`;
    if (state.company.website) $("#scrape-url").value = state.company.website;
    setSystemStatus("Ready", true);
  } catch (error) {
    setSystemStatus("Company not configured");
    addMessage(
      "assistant",
      `The company profile “${companySlug}” is not configured yet. Run the bootstrap command in the README or create the company through the API.`,
    );
    throw error;
  }
}

function addMessage(role, text, data = {}) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "You" : "AI";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  bubble.append(paragraph);

  if (data.sources?.length) {
    const sourceCards = document.createElement("div");
    sourceCards.className = "source-cards";
    for (const source of data.sources) {
      const details = document.createElement("details");
      details.className = "source-card";
      const location = [source.page ? `page ${source.page}` : "", source.section || ""]
        .filter(Boolean)
        .join(" · ");
      details.innerHTML = `
        <summary>[${escapeHtml(source.id)}] ${escapeHtml(source.source_name)}</summary>
        <p>${escapeHtml(source.excerpt)}</p>
        ${location ? `<p>${escapeHtml(location)}</p>` : ""}
      `;
      sourceCards.append(details);
    }
    bubble.append(sourceCards);
  }

  if (data.recommendations?.length) {
    const cards = document.createElement("div");
    cards.className = "recommendation-cards";
    for (const recommendation of data.recommendations) {
      const card = document.createElement("div");
      card.className = "recommendation-card";
      const price = recommendation.unit_price
        ? formatMoney(recommendation.unit_price, recommendation.currency || state.company.currency)
        : "Company to confirm price";
      card.innerHTML = `
        <strong>${escapeHtml(recommendation.sku ? `${recommendation.sku} · ` : "")}${escapeHtml(recommendation.name)}</strong>
        <p>${escapeHtml(recommendation.reason)}</p>
        <p>${escapeHtml(recommendation.price_note || price)}</p>
        <footer>
          <span>${escapeHtml(price)}</span>
          <button class="secondary-button" type="button">Add to request</button>
        </footer>
      `;
      card.querySelector("button").addEventListener("click", () => addRecommendation(recommendation));
      cards.append(card);
    }
    bubble.append(cards);
  }
  article.append(avatar, bubble);
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
}

function addLoadingMessage() {
  const article = document.createElement("article");
  article.className = "message assistant";
  article.id = "loading-message";
  article.innerHTML = `
    <div class="avatar">AI</div>
    <div class="bubble"><span class="loading-dots"><i></i><i></i><i></i></span></div>
  `;
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
}

async function sendChat(message) {
  addMessage("user", message);
  addLoadingMessage();
  try {
    const response = await api(`/api/companies/${companySlug}/chat`, {
      method: "POST",
      body: JSON.stringify({
        message,
        customer_session: state.customerSession,
        conversation_id: state.conversationId,
        customer_context: {
          cart_items: state.cart?.items?.map((item) => ({
            sku: item.sku,
            name: item.name,
            quantity: item.quantity,
            unit: item.unit,
          })) || [],
        },
      }),
    });
    state.conversationId = response.conversation_id;
    addMessage("assistant", response.answer, response);
  } catch (error) {
    addMessage("assistant", `I could not complete that request: ${error.message}`);
  } finally {
    $("#loading-message")?.remove();
  }
}

async function ensureCart() {
  const savedId = localStorage.getItem(cartKey);
  if (savedId) {
    try {
      state.cart = await api(
        `/api/companies/${companySlug}/carts/${savedId}?customer_session=${encodeURIComponent(state.customerSession)}`,
      );
      if (state.cart.status !== "draft") {
        localStorage.removeItem(cartKey);
        state.cart = null;
      }
    } catch {
      localStorage.removeItem(cartKey);
    }
  }
  if (!state.cart) {
    state.cart = await api(`/api/companies/${companySlug}/carts`, {
      method: "POST",
      body: JSON.stringify({ customer_session: state.customerSession }),
    });
    localStorage.setItem(cartKey, state.cart.id);
  }
  renderCart();
}

async function addRecommendation(recommendation) {
  await ensureCart();
  const payload = {
    sku: recommendation.sku,
    name: recommendation.name,
    quantity: recommendation.suggested_quantity || 1,
    unit: recommendation.unit || "piece",
    unit_price: recommendation.unit_price,
    currency: recommendation.currency || state.company.currency,
    notes: recommendation.price_note,
    evidence: recommendation.evidence_ids || [],
  };
  try {
    state.cart = await api(
      `/api/companies/${companySlug}/carts/${state.cart.id}/items?customer_session=${encodeURIComponent(state.customerSession)}`,
      { method: "POST", body: JSON.stringify(payload) },
    );
    renderCart();
    toast("Added to the approval request.");
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderCart() {
  const items = state.cart?.items || [];
  $("#cart-count").textContent = items.length;
  $("#open-checkout").disabled = items.length === 0;
  if (!items.length) {
    cartItems.innerHTML = `
      <div class="empty-state"><span>+</span><p>Add a recommendation or enter an item manually.</p></div>
    `;
    $("#cart-total").textContent = "Not fully priced";
    return;
  }
  cartItems.innerHTML = "";
  let completePricing = true;
  let total = 0;
  for (const item of items) {
    if (item.unit_price === null || item.unit_price === undefined) {
      completePricing = false;
    } else {
      total += Number(item.quantity) * Number(item.unit_price);
    }
    const row = document.createElement("div");
    row.className = "cart-item";
    row.innerHTML = `
      <div>
        <strong>${escapeHtml(item.sku ? `${item.sku} · ` : "")}${escapeHtml(item.name)}</strong>
        <small>${escapeHtml(String(item.quantity))} ${escapeHtml(item.unit)} · ${
          item.unit_price === null ? "price pending" : formatMoney(item.unit_price, item.currency)
        }</small>
      </div>
      <button type="button" aria-label="Remove item">Remove</button>
    `;
    row.querySelector("button").addEventListener("click", () => removeCartItem(item.id));
    cartItems.append(row);
  }
  $("#cart-total").textContent = completePricing
    ? formatMoney(total, items[0].currency)
    : "Not fully priced";
}

async function removeCartItem(itemId) {
  try {
    state.cart = await api(
      `/api/companies/${companySlug}/carts/${state.cart.id}/items/${itemId}?customer_session=${encodeURIComponent(state.customerSession)}`,
      { method: "DELETE" },
    );
    renderCart();
  } catch (error) {
    toast(error.message, "error");
  }
}

function statusCopy(order) {
  const mapping = {
    pending_company_approval: [
      "Pending company approval",
      "The request is waiting in the company review queue. No payment, production, reservation, or shipping can start yet.",
    ],
    company_approved: [
      "Approved by the company",
      "The company approved the request. Review the company note and total, then confirm if you accept.",
    ],
    changes_requested: [
      "Company requested changes",
      order.company_note || "The company asked for the request to be revised.",
    ],
    rejected: ["Not approved", order.company_note || "The company did not approve this request."],
    customer_confirmed: [
      "Customer confirmed",
      "The approved terms were accepted. The company can now continue its normal sales process.",
    ],
    cancelled: ["Cancelled", "This request will not proceed."],
  };
  return mapping[order.status] || [order.status, "Status updated."];
}

function renderOrder(order) {
  state.order = order;
  const [title, description] = statusCopy(order);
  $("#order-status-title").textContent = title;
  $("#order-status-body").innerHTML = `
    <p>${escapeHtml(description)}</p>
    <div class="approval-callout">
      Request ID: <strong>${escapeHtml(order.id)}</strong><br />
      Estimated total: ${escapeHtml(formatMoney(order.estimated_total, order.currency))}<br />
      Approved total: ${escapeHtml(formatMoney(order.approved_total, order.currency))}
    </div>
    ${order.company_note ? `<p><strong>Company note:</strong> ${escapeHtml(order.company_note)}</p>` : ""}
  `;
  $("#customer-confirm").classList.toggle("hidden", order.status !== "company_approved");
}

async function pollOrder() {
  const orderId = localStorage.getItem(orderKey);
  if (!orderId) return;
  try {
    const order = await api(
      `/api/companies/${companySlug}/orders/${orderId}?customer_session=${encodeURIComponent(state.customerSession)}`,
    );
    const changed = state.order && state.order.status !== order.status;
    renderOrder(order);
    if (changed) {
      toast(`Order status: ${statusCopy(order)[0]}`);
      $("#order-dialog").showModal();
    }
  } catch {
    localStorage.removeItem(orderKey);
  }
}

async function loadSources() {
  try {
    const sources = await api(`/api/companies/${companySlug}/sources`, {
      headers: adminHeaders(),
    });
    $("#source-list").innerHTML = sources.length
      ? sources
          .map(
            (source) => `
              <div class="source-row">
                <strong>${escapeHtml(source.name)}</strong>
                <small>${escapeHtml(source.kind)} · ${escapeHtml(source.status)} · authority ${source.authority_score}/100</small>
                <small>${escapeHtml(source.origin)}</small>
              </div>
            `,
          )
          .join("")
      : '<p class="empty-state">No indexed sources.</p>';
  } catch (error) {
    toast(error.message, "error");
  }
}

function orderReviewParams(orderId) {
  if (state.reviewToken && query.get("order_id") === orderId) {
    return `?review_token=${encodeURIComponent(state.reviewToken)}`;
  }
  return "";
}

async function loadApprovals() {
  const orderId = query.get("order_id");
  try {
    let orders;
    if (orderId && state.reviewToken && !state.adminKey) {
      orders = [
        await api(
          `/api/admin/companies/${companySlug}/orders/${orderId}?review_token=${encodeURIComponent(state.reviewToken)}`,
        ),
      ];
    } else {
      orders = await api(
        `/api/admin/companies/${companySlug}/orders?status=pending_company_approval`,
        { headers: adminHeaders() },
      );
    }
    $("#approval-list").innerHTML = "";
    if (!orders.length) {
      $("#approval-list").innerHTML = "<p>No pending approvals.</p>";
      return;
    }
    for (const order of orders) {
      const row = document.createElement("div");
      row.className = "approval-row";
      row.innerHTML = `
        <strong>${escapeHtml(order.customer_name)} · ${escapeHtml(order.id)}</strong>
        <small>${escapeHtml(order.customer_company || "Individual customer")} · ${escapeHtml(order.customer_email || order.customer_phone || "")}</small>
        <small>${escapeHtml(formatMoney(order.estimated_total, order.currency))}</small>
        <div class="approval-actions">
          <button class="approve" type="button">Approve</button>
          <button class="changes" type="button">Request changes</button>
          <button class="reject" type="button">Reject</button>
        </div>
      `;
      row.querySelector(".approve").addEventListener("click", () => decideOrder(order, "approve"));
      row
        .querySelector(".changes")
        .addEventListener("click", () => decideOrder(order, "request-changes"));
      row.querySelector(".reject").addEventListener("click", () => decideOrder(order, "reject"));
      $("#approval-list").append(row);
    }
  } catch (error) {
    $("#approval-list").innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

async function decideOrder(order, action) {
  const note = window.prompt("Company note (optional):", "") ?? "";
  let approvedTotal = null;
  if (action === "approve") {
    const value = window.prompt(
      "Approved total (leave blank if the company will contact the customer):",
      order.estimated_total ?? "",
    );
    approvedTotal = value ? Number(value) : null;
  }
  try {
    await api(
      `/api/admin/companies/${companySlug}/orders/${order.id}/${action}${orderReviewParams(order.id)}`,
      {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({
          actor: "Company reviewer",
          note,
          approved_total: approvedTotal,
          currency: order.currency,
        }),
      },
    );
    toast("Order decision saved.");
    loadApprovals();
  } catch (error) {
    toast(error.message, "error");
  }
}

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  await sendChat(message);
});

$("#suggestions").addEventListener("click", (event) => {
  if (event.target.matches("button")) sendChat(event.target.textContent);
});

$("#manual-item-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const price = $("#manual-price").value;
  await addRecommendation({
    sku: $("#manual-sku").value.trim() || null,
    name: $("#manual-name").value.trim(),
    suggested_quantity: Number($("#manual-quantity").value),
    unit: $("#manual-unit").value.trim(),
    unit_price: price ? Number(price) : null,
    currency: state.company.currency,
    price_note: "Manually entered; company must confirm.",
    evidence_ids: [],
  });
  event.target.reset();
  $("#manual-quantity").value = "1";
  $("#manual-unit").value = "metre";
});

$("#open-checkout").addEventListener("click", () => $("#checkout-dialog").showModal());
$("#close-checkout").addEventListener("click", () => $("#checkout-dialog").close());
$("#close-order").addEventListener("click", () => $("#order-dialog").close());

$("#checkout-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!$("#customer-email").value.trim() && !$("#customer-phone").value.trim()) {
    toast("Provide an email address or phone number.", "error");
    return;
  }
  try {
    const order = await api(`/api/companies/${companySlug}/orders`, {
      method: "POST",
      body: JSON.stringify({
        cart_id: state.cart.id,
        submission_key: crypto.randomUUID(),
        customer_name: $("#customer-name").value.trim(),
        customer_email: $("#customer-email").value.trim() || null,
        customer_phone: $("#customer-phone").value.trim() || null,
        customer_company: $("#customer-company").value.trim() || null,
        delivery_address: $("#delivery-address").value.trim() || null,
        customer_note: $("#customer-note").value.trim() || null,
        consent_to_share_with_company: $("#customer-consent").checked,
      }),
    });
    localStorage.setItem(orderKey, order.id);
    localStorage.removeItem(cartKey);
    renderOrder(order);
    $("#checkout-dialog").close();
    $("#order-dialog").showModal();
    state.cart = null;
    await ensureCart();
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#customer-confirm").addEventListener("click", async () => {
  try {
    const order = await api(
      `/api/companies/${companySlug}/orders/${state.order.id}/confirm`,
      {
        method: "POST",
        body: JSON.stringify({
          customer_session: state.customerSession,
          accept_company_terms: true,
        }),
      },
    );
    renderOrder(order);
    toast("Approved company terms accepted.");
  } catch (error) {
    toast(error.message, "error");
  }
});

function openAdmin() {
  $("#admin-panel").classList.remove("hidden");
  $("#admin-panel").scrollIntoView({ behavior: "smooth" });
  loadSources();
  loadApprovals();
}

$("#admin-toggle").addEventListener("click", openAdmin);
$("#close-admin").addEventListener("click", () => $("#admin-panel").classList.add("hidden"));
$("#save-admin-key").addEventListener("click", () => {
  state.adminKey = $("#admin-key").value;
  sessionStorage.setItem("omdi-admin-key", state.adminKey);
  loadApprovals();
  toast("Admin key set for this browser tab.");
});
$("#refresh-sources").addEventListener("click", loadSources);
$("#refresh-orders").addEventListener("click", loadApprovals);

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData();
  for (const file of $("#upload-files").files) form.append("files", file);
  form.append("authority_score", $("#upload-authority").value);
  try {
    await api(`/api/companies/${companySlug}/sources/upload`, {
      method: "POST",
      headers: adminHeaders(),
      body: form,
    });
    toast("Documents indexed.");
    event.target.reset();
    loadSources();
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#scrape-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api(`/api/companies/${companySlug}/sources/scrape`, {
      method: "POST",
      headers: adminHeaders(),
      body: JSON.stringify({
        url: $("#scrape-url").value,
        max_pages: Number($("#scrape-pages").value),
        max_depth: Number($("#scrape-depth").value),
        authority_score: 70,
      }),
    });
    toast(`Indexed ${result.indexed} pages; skipped ${result.skipped}.`);
    loadSources();
  } catch (error) {
    toast(error.message, "error");
  }
});

async function start() {
  $("#admin-key").value = state.adminKey;
  try {
    await loadCompany();
    await ensureCart();
    await pollOrder();
    setInterval(pollOrder, 12000);
    if (query.get("admin") === "1") openAdmin();
  } catch (error) {
    toast(error.message, "error");
  }
}

start();

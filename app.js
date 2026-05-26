const STORAGE_KEY = "autoShopManager.v1";
const API_STATE_URL = "/api/state";
const API_PARTS_SEARCH_URL = "/api/parts/search";
const API_PARTS_PROVIDERS_URL = "/api/parts/providers";
const API_VEHICLE_DECODE_URL = "/api/vehicles/decode-vin";
const API_VEHICLE_MODELS_URL = "/api/vehicles/models";
const API_AUTH_ME_URL = "/api/auth/me";
const API_AUTH_SETTINGS_URL = "/api/auth/settings";
const ROCKAUTO_CATALOG_BASE_URL = "https://www.rockauto.com/en/catalog";
const MANUAL_MODEL_VALUE = "__manual_model__";
const VIN_BARCODE_FORMATS = ["code_39", "code_128", "data_matrix", "qr_code", "pdf417"];
const TAX_RATE = 0.08125;
const ORDER_STATUSES = [
  "estimate created",
  "estimate sent",
  "estimate approved, order parts",
  "waiting on parts",
  "ready to be completed",
  "work done",
  "paid/close"
];
const CLOSED_ORDER_STATUS = "paid/close";
const LEGACY_STATUS_MAP = {
  Estimate: "estimate created",
  Approved: "estimate approved, order parts",
  "In Progress": "ready to be completed",
  "Waiting Parts": "waiting on parts",
  Ready: "ready to be completed",
  Paid: "paid/close"
};
const STATUS_STYLE_MAP = {
  "estimate created": "status-estimate-created",
  "estimate sent": "status-estimate-sent",
  "estimate approved, order parts": "status-approved",
  "waiting on parts": "status-waiting",
  "ready to be completed": "status-ready",
  "work done": "status-done",
  "paid/close": "status-closed"
};

const starterData = {
  customers: [
    {
      id: crypto.randomUUID(),
      name: "Walk-in Customer",
      phone: "",
      email: "",
      notes: "Use this until you add real customers.",
      vehicles: [
        {
          id: crypto.randomUUID(),
          year: "2017",
          make: "Ford",
          model: "F-150",
          engine: "5.0L",
          vin: "",
          plate: "",
          plateState: ""
        }
      ]
    }
  ],
  orders: [],
  partsOrders: []
};

let state = structuredClone(starterData);
let currentView = "dashboard";
let apiAvailable = false;
let partsProviders = [];
let authSettings = null;
let vinScannerStream = null;
let vinScannerFrame = null;
let vinScannerDetector = null;
let vinScannerBusy = false;

const views = {
  dashboard: {
    title: "Dashboard",
    subtitle: "Today at a glance.",
    el: document.querySelector("#dashboardView")
  },
  customers: {
    title: "Customers",
    subtitle: "Customer and vehicle records.",
    el: document.querySelector("#customersView")
  },
  orders: {
    title: "Estimates & Repair Orders",
    subtitle: "Build estimates, approve work, and track status.",
    el: document.querySelector("#ordersView")
  },
  parts: {
    title: "Parts",
    subtitle: "Compare supplier quotes and track orders.",
    el: document.querySelector("#partsView")
  },
  settings: {
    title: "Settings",
    subtitle: "Authentication, backups, and app configuration.",
    el: document.querySelector("#settingsView")
  }
};

async function loadState() {
  try {
    const response = await fetch(API_STATE_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`State API returned ${response.status}`);
    const payload = await response.json();
    apiAvailable = true;
    return normalizeState(payload);
  } catch {
    apiAvailable = false;
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return structuredClone(starterData);
    try {
      return normalizeState(JSON.parse(saved));
    } catch {
      return structuredClone(starterData);
    }
  }
}

async function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state, null, 2));
  if (!apiAvailable) return;
  try {
    const response = await fetch(API_STATE_URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state)
    });
    if (!response.ok) throw new Error(`State API returned ${response.status}`);
  } catch {
    apiAvailable = false;
  }
}

function money(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD"
  }).format(Number(value) || 0);
}

function normalizeOrderStatus(status) {
  return LEGACY_STATUS_MAP[status] || (ORDER_STATUSES.includes(status) ? status : ORDER_STATUSES[0]);
}

function normalizeState(data) {
  const normalized = data || structuredClone(starterData);
  normalized.customers = normalized.customers || [];
  normalized.orders = (normalized.orders || []).map((order) => ({
    ...order,
    status: normalizeOrderStatus(order.status)
  }));
  normalized.partsOrders = normalized.partsOrders || [];
  return normalized;
}

function isClosedOrder(order) {
  return normalizeOrderStatus(order.status) === CLOSED_ORDER_STATUS;
}

function statusClass(status) {
  return STATUS_STYLE_MAP[normalizeOrderStatus(status)] || "status-estimate-created";
}

function roundCurrency(value) {
  return Math.round((Number(value) || 0) * 100) / 100;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[char]));
}

function allVehicles() {
  return state.customers.flatMap((customer) =>
    (customer.vehicles || []).map((vehicle) => ({
      ...vehicle,
      customerId: customer.id,
      customerName: customer.name
    }))
  );
}

function vehicleLabel(vehicle) {
  if (!vehicle) return "No vehicle";
  return [vehicle.year, vehicle.make, vehicle.model, vehicle.trim, vehicle.engine].filter(Boolean).join(" ");
}

function normalizeRockAutoMake(make) {
  const value = String(make || "").trim();
  const key = value.toLowerCase().replace(/\./g, "").replace(/\s+/g, " ");
  const aliases = {
    chevy: "Chevrolet",
    mercedes: "Mercedes-Benz",
    vw: "Volkswagen"
  };
  return aliases[key] || value;
}

function normalizeRockAutoModel(make, model) {
  const normalizedMake = normalizeRockAutoMake(make).toLowerCase();
  const value = String(model || "").trim();
  const key = value.toLowerCase().replace(/\./g, "").replace(/\s+/g, " ");

  if (normalizedMake === "honda" && key === "del sol") return "Civic Del Sol";

  if (normalizedMake === "ford") {
    const truckMatch = key.match(/^(f|e)\s*-?\s*(\d{3})$/);
    if (truckMatch) return `${truckMatch[1].toUpperCase()}-${truckMatch[2]}`;
  }

  return value;
}

function rockAutoCatalogVehicle(vehicle) {
  if (!vehicle) return null;
  return {
    year: String(vehicle.year || "").trim(),
    make: normalizeRockAutoMake(vehicle.make),
    model: normalizeRockAutoModel(vehicle.make, vehicle.model)
  };
}

function rockAutoPathToken(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, "+");
}

function rockAutoCatalogUrl(vehicle) {
  const catalogVehicle = rockAutoCatalogVehicle(vehicle);
  if (!catalogVehicle?.year || !catalogVehicle.make || !catalogVehicle.model) return "";
  const path = [
    catalogVehicle.make,
    catalogVehicle.year,
    catalogVehicle.model
  ].map(rockAutoPathToken).join(",");
  return `${ROCKAUTO_CATALOG_BASE_URL}/${encodeURIComponent(path)}`;
}

function rockAutoCatalogLabel(vehicle) {
  const catalogVehicle = rockAutoCatalogVehicle(vehicle);
  if (!catalogVehicle?.year || !catalogVehicle.make || !catalogVehicle.model) return "";
  return `${catalogVehicle.year} ${catalogVehicle.make} ${catalogVehicle.model}`;
}

function setView(viewName) {
  currentView = viewName;
  Object.entries(views).forEach(([name, view]) => {
    view.el.classList.toggle("active", name === viewName);
  });
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === viewName);
  });
  document.querySelector("#viewTitle").textContent = views[viewName].title;
  document.querySelector("#viewSubtitle").textContent = views[viewName].subtitle;
  render();
}

function render() {
  renderDashboard();
  renderCustomerOptions();
  renderVehicleOptions();
  renderPartsTargetOptions();
  renderRockAutoLookup();
  renderPartsProviders();
  renderAuthSettings();
  renderCustomers();
  renderOrders();
  renderPartsOrders();
  updateTotals();
}

function renderAuthSettings() {
  if (!authSettings) return;
  document.querySelector("#authLocalEnabled").checked = Boolean(authSettings.localEnabled);
  document.querySelector("#authLocalUsername").value = authSettings.localUsername || "admin";
  document.querySelector("#authOidcEnabled").checked = Boolean(authSettings.oidcEnabled);
  document.querySelector("#authIssuerUrl").value = authSettings.issuerUrl || "";
  document.querySelector("#authClientId").value = authSettings.clientId || "";
  document.querySelector("#authClientSecret").placeholder = authSettings.clientSecretConfigured
    ? "Configured - leave blank to keep current"
    : "Required for Keycloak login";
  document.querySelector("#authPublicUrl").value = authSettings.publicUrl || window.location.origin;
  document.querySelector("#authEmailDomains").value = authSettings.emailDomains || "*";
  const publicUrl = document.querySelector("#authPublicUrl").value.replace(/\/$/, "");
  document.querySelector("#authRedirectUri").textContent = `${publicUrl || window.location.origin}/oauth2/callback`;
}

function renderDashboard() {
  const openOrders = state.orders.filter((order) => !isClosedOrder(order));
  const waitingParts = state.partsOrders.filter((order) => order.status !== "Received");
  const unpaidTotal = openOrders.reduce((sum, order) => sum + orderTotal(order).total, 0);

  views.dashboard.el.innerHTML = `
    <div class="stat-grid">
      <div class="stat"><span>Customers</span><strong>${state.customers.length}</strong></div>
      <div class="stat"><span>Vehicles</span><strong>${allVehicles().length}</strong></div>
      <div class="stat"><span>Open ROs</span><strong>${openOrders.length}</strong></div>
      <div class="stat"><span>Open Value</span><strong>${money(unpaidTotal)}</strong></div>
    </div>
    <div class="panel">
      <div class="panel-header">
        <h2>Needs Attention</h2>
        <span class="muted">${waitingParts.length} parts orders open</span>
      </div>
      <div class="list">
        ${
          [...openOrders.slice(0, 5), ...waitingParts.slice(0, 5)].map((item) => {
            if (item.supplier) {
              return `<div class="list-item"><strong>${escapeHtml(item.partName)}</strong><span class="muted">${escapeHtml(item.supplier)} - ${escapeHtml(item.status)}</span></div>`;
            }
            const customer = state.customers.find((entry) => entry.id === item.customerId);
            return `<a href="#estimate-${escapeHtml(item.id)}" class="list-item card-link" data-action="open-order" data-id="${item.id}">
              <span class="dashboard-card-main">
                <strong>${escapeHtml(item.status)} - ${escapeHtml(customer?.name || "Unknown")}</strong>
                <span class="muted">${money(orderTotal(item).total)}</span>
                <span class="link-hint">Open estimate</span>
              </span>
              <span class="progress-indicator ${statusClass(item.status)}">
                <span class="progress-dot" aria-hidden="true"></span>
                ${escapeHtml(normalizeOrderStatus(item.status))}
              </span>
            </a>`;
          }).join("") || `<p class="muted">Nothing open yet. A quiet board is not the worst thing.</p>`
        }
      </div>
    </div>
  `;
}

function renderCustomerOptions() {
  const select = document.querySelector("#orderCustomer");
  const previous = select.value;
  select.innerHTML = `<option value="">Choose customer</option>` + state.customers
    .map((customer) => `<option value="${customer.id}">${escapeHtml(customer.name)}</option>`)
    .join("");
  select.value = previous;
}

function renderVehicleOptions() {
  const orderCustomerId = document.querySelector("#orderCustomer").value;
  const orderVehicle = document.querySelector("#orderVehicle");
  const vehicleSelects = [orderVehicle, document.querySelector("#partsVehicle")];
  const vehicles = allVehicles();

  vehicleSelects.forEach((select) => {
    const previous = select.value;
    const filtered = select === orderVehicle && orderCustomerId
      ? vehicles.filter((vehicle) => vehicle.customerId === orderCustomerId)
      : vehicles;
    select.innerHTML = `<option value="">Choose vehicle</option>` + filtered
      .map((vehicle) => `<option value="${vehicle.id}">${escapeHtml(vehicle.customerName)} - ${escapeHtml(vehicleLabel(vehicle))}</option>`)
      .join("");
    select.value = previous;
  });
}

function renderPartsTargetOptions() {
  const select = document.querySelector("#partsTargetOrder");
  if (!select) return;
  const previous = select.value;
  const openOrders = state.orders.filter((order) => !isClosedOrder(order));
  select.innerHTML = `<option value="">Do not add to estimate</option>` + openOrders.map((order) => {
    const customer = state.customers.find((entry) => entry.id === order.customerId);
    const vehicle = allVehicles().find((entry) => entry.id === order.vehicleId);
    const label = `${customer?.name || "Unknown"} - ${vehicleLabel(vehicle)} - ${order.status}`;
    return `<option value="${order.id}">${escapeHtml(label)}</option>`;
  }).join("");
  select.value = previous;
}

function selectedPartsVehicle() {
  return allVehicles().find((entry) => entry.id === document.querySelector("#partsVehicle")?.value);
}

function renderRockAutoLookup() {
  const container = document.querySelector("#rockAutoLookup");
  if (!container) return;
  const vehicle = selectedPartsVehicle();
  const catalogUrl = rockAutoCatalogUrl(vehicle);
  const catalogLabel = rockAutoCatalogLabel(vehicle);
  container.innerHTML = catalogUrl ? `
    <a class="secondary external-button" href="${escapeHtml(catalogUrl)}" target="_blank" rel="noopener noreferrer">Open RockAuto</a>
    <span class="muted">${escapeHtml(catalogLabel)}</span>
  ` : "";
}

function providerSummary(provider) {
  if (provider.key === "rockauto") return "Select a vehicle below to open the matching RockAuto catalog.";
  if (provider.key === "manual") return "Use for phone quotes, walk-ins, and one-off vendors.";
  return provider.enabled ? "Configured" : "Needs credentials";
}

function renderProviderAction(provider) {
  if (provider.key !== "rockauto") return "";
  const vehicle = selectedPartsVehicle();
  const catalogUrl = rockAutoCatalogUrl(vehicle);
  return `<div class="item-actions">
    ${catalogUrl
      ? `<a class="secondary small external-button" href="${escapeHtml(catalogUrl)}" target="_blank" rel="noopener noreferrer">Open RockAuto</a>`
      : `<button type="button" class="secondary small" disabled>Select vehicle below</button>`}
  </div>`;
}

function renderPartsProviders() {
  const container = document.querySelector("#partsProviders");
  if (!container) return;
  container.innerHTML = partsProviders.map((provider) => `
    <article class="provider ${provider.enabled ? "" : "disabled"}">
      <div class="list-item-title">
        <strong>${escapeHtml(provider.displayName)}</strong>
        <span class="pill">${escapeHtml(provider.status)}</span>
      </div>
      <span class="muted">${escapeHtml(provider.description || "Provider adapter staged")}</span>
      <span>${escapeHtml(providerSummary(provider))}</span>
      ${renderProviderAction(provider)}
    </article>
  `).join("") || `<p class="muted">Provider status will appear here.</p>`;
}

function refreshPartsLookupLinks() {
  renderRockAutoLookup();
  renderPartsProviders();
}

function renderCustomers() {
  const query = document.querySelector("#customerSearch").value.trim().toLowerCase();
  const list = document.querySelector("#customerList");
  const customers = state.customers.filter((customer) => {
    const haystack = [customer.name, customer.phone, customer.email, customer.notes].join(" ").toLowerCase();
    return haystack.includes(query);
  });

  list.innerHTML = customers.map((customer) => `
    <article class="list-item">
      <div class="list-item-title">
        <strong>${escapeHtml(customer.name)}</strong>
        <span class="pill">${customer.vehicles?.length || 0} vehicles</span>
      </div>
      <span class="muted">${escapeHtml(customer.phone || "No phone")} ${customer.email ? `- ${escapeHtml(customer.email)}` : ""}</span>
      <div class="vehicle-list">
        ${(customer.vehicles || []).map((vehicle) => `
          <span>
            ${escapeHtml(vehicleLabel(vehicle))}
            ${vehicle.body ? `- ${escapeHtml(vehicle.body)}` : ""}
            ${vehicle.plate ? `- ${escapeHtml([vehicle.plateState, vehicle.plate].filter(Boolean).join(" "))}` : ""}
            ${vehicle.vin ? `- VIN ${escapeHtml(vehicle.vin)}` : ""}
            ${vehicle.source ? `<small class="source-tag">${escapeHtml(vehicle.source)}</small>` : ""}
          </span>
        `).join("") || "<span>No vehicles yet</span>"}
      </div>
      <div class="item-actions">
        <button class="secondary small" data-action="edit-customer" data-id="${customer.id}">Edit</button>
        <button class="secondary small" data-action="add-vehicle" data-id="${customer.id}">Add Vehicle</button>
        <button class="primary small" data-action="start-order" data-id="${customer.id}">New Estimate</button>
      </div>
    </article>
  `).join("") || `<p class="muted">No customers found.</p>`;
}

function renderOrders() {
  const filter = document.querySelector("#orderFilter").value;
  const list = document.querySelector("#orderList");
  const orders = state.orders.filter((order) => {
    if (filter === "all") return true;
    if (filter === "open") return !isClosedOrder(order);
    return normalizeOrderStatus(order.status) === filter;
  });

  list.innerHTML = orders.map((order) => {
    const customer = state.customers.find((entry) => entry.id === order.customerId);
    const vehicle = allVehicles().find((entry) => entry.id === order.vehicleId);
    return `
      <article class="list-item">
        <div class="list-item-title">
          <strong>${escapeHtml(customer?.name || "Unknown customer")}</strong>
          <span class="pill ${statusClass(order.status)}">${escapeHtml(normalizeOrderStatus(order.status))}</span>
        </div>
        <span class="muted">${escapeHtml(vehicleLabel(vehicle))}</span>
        <span>${escapeHtml(order.concern || "No concern entered")}</span>
        <span class="muted">Subtotal ${money(orderTotal(order).subtotal)} - Tax ${money(orderTotal(order).tax)}</span>
        <strong>Total ${money(orderTotal(order).total)}</strong>
        <div class="item-actions">
          <button class="secondary small" data-action="edit-order" data-id="${order.id}">Edit</button>
          <button class="secondary small" data-action="lookup-parts" data-id="${order.id}">Find Parts</button>
          <button class="secondary small" data-action="duplicate-order" data-id="${order.id}">Duplicate</button>
          <button class="secondary small" data-action="email-order" data-id="${order.id}">Email</button>
          <button class="danger small" data-action="delete-order" data-id="${order.id}">Delete</button>
        </div>
      </article>
    `;
  }).join("") || `<p class="muted">No work orders in this view.</p>`;
}

function renderPartsOrders() {
  const list = document.querySelector("#partsOrders");
  list.innerHTML = state.partsOrders.map((order) => `
    <article class="list-item">
      <div class="list-item-title">
        <strong>${escapeHtml(order.partName)}</strong>
        <span class="pill">${escapeHtml(order.status)}</span>
      </div>
      <span class="muted">${escapeHtml(order.supplier)} - ${money(order.cost)} cost - ${escapeHtml(order.eta)}</span>
      <div class="item-actions">
        <button class="secondary small" data-action="part-status" data-id="${order.id}" data-status="Ordered">Ordered</button>
        <button class="secondary small" data-action="part-status" data-id="${order.id}" data-status="Received">Received</button>
      </div>
    </article>
  `).join("") || `<p class="muted">No parts orders yet.</p>`;
}

function customerFromForm() {
  return {
    id: document.querySelector("#customerId").value || crypto.randomUUID(),
    name: document.querySelector("#customerName").value.trim(),
    phone: document.querySelector("#customerPhone").value.trim(),
    email: document.querySelector("#customerEmail").value.trim(),
    notes: document.querySelector("#customerNotes").value.trim(),
    vehicles: []
  };
}

function clearCustomerForm() {
  document.querySelector("#customerForm").reset();
  document.querySelector("#customerId").value = "";
  document.querySelector("#customerForm h2").textContent = "Add Customer";
}

function fillCustomerForm(customer) {
  document.querySelector("#customerId").value = customer.id;
  document.querySelector("#customerName").value = customer.name;
  document.querySelector("#customerPhone").value = customer.phone || "";
  document.querySelector("#customerEmail").value = customer.email || "";
  document.querySelector("#customerNotes").value = customer.notes || "";
  document.querySelector("#customerForm h2").textContent = "Edit Customer";
  setView("customers");
}

function setVehicleLookupStatus(message, isError = false) {
  const status = document.querySelector("#vehicleLookupStatus");
  status.textContent = message || "";
  status.classList.toggle("error-text", isError);
}

function setFormStatus(selector, message, isError = false) {
  const status = document.querySelector(selector);
  status.textContent = message || "";
  status.classList.toggle("error-text", isError);
}

function setVinScannerStatus(message, isError = false) {
  const status = document.querySelector("#vinScannerStatus");
  status.textContent = message || "";
  status.classList.toggle("error-text", isError);
}

function openVehicleDialog(customerId) {
  const form = document.querySelector("#vehicleForm");
  form.reset();
  document.querySelector("#vehicleCustomerId").value = customerId;
  resetVehicleModelOptions();
  stopVinScanner();
  document.querySelector("#vehicleLookupStatus").dataset.source = "";
  setVehicleLookupStatus("Enter a VIN to decode, or fill year/make/model manually.");
  const dialog = document.querySelector("#vehicleDialog");
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

function closeVehicleDialog() {
  stopVinScanner();
  const dialog = document.querySelector("#vehicleDialog");
  if (typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

function extractVinFromScan(rawValue) {
  const normalized = String(rawValue || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  const match = normalized.match(/[A-HJ-NPR-Z0-9]{17}/);
  return match ? match[0] : "";
}

async function getVinBarcodeDetector() {
  if (!("BarcodeDetector" in window)) {
    throw new Error("Barcode scanning is not supported by this browser.");
  }

  if (vinScannerDetector) return vinScannerDetector;

  let formats = VIN_BARCODE_FORMATS;
  if (typeof BarcodeDetector.getSupportedFormats === "function") {
    const supportedFormats = await BarcodeDetector.getSupportedFormats();
    formats = VIN_BARCODE_FORMATS.filter((format) => supportedFormats.includes(format));
  }
  if (!formats.length) {
    throw new Error("This browser camera scanner does not support common VIN barcode formats.");
  }

  vinScannerDetector = new BarcodeDetector({ formats });
  return vinScannerDetector;
}

async function startVinScanner() {
  if (!window.isSecureContext) {
    setVehicleLookupStatus("Camera scanning requires HTTPS.", true);
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    setVehicleLookupStatus("Camera access is not available in this browser.", true);
    return;
  }

  try {
    const detector = await getVinBarcodeDetector();
    const panel = document.querySelector("#vinScannerPanel");
    const video = document.querySelector("#vinScannerVideo");
    panel.hidden = false;
    setVinScannerStatus("Starting camera...");
    vinScannerStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 }
      },
      audio: false
    });
    video.srcObject = vinScannerStream;
    await video.play();
    setVinScannerStatus("Point the camera at the VIN barcode.");
    scanVinFrame(detector, video);
  } catch (error) {
    stopVinScanner();
    setVehicleLookupStatus(error.message || "Unable to start VIN scanner.", true);
  }
}

function stopVinScanner() {
  if (vinScannerFrame) {
    cancelAnimationFrame(vinScannerFrame);
    vinScannerFrame = null;
  }
  if (vinScannerStream) {
    vinScannerStream.getTracks().forEach((track) => track.stop());
    vinScannerStream = null;
  }
  vinScannerBusy = false;
  const video = document.querySelector("#vinScannerVideo");
  if (video) video.srcObject = null;
  const panel = document.querySelector("#vinScannerPanel");
  if (panel) panel.hidden = true;
  setVinScannerStatus("");
}

async function scanVinFrame(detector, video) {
  if (!vinScannerStream) return;
  if (!vinScannerBusy && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    vinScannerBusy = true;
    try {
      const barcodes = await detector.detect(video);
      const vin = barcodes.map((barcode) => extractVinFromScan(barcode.rawValue)).find(Boolean);
      if (vin) {
        await acceptScannedVin(vin);
        return;
      }
    } catch {
      setVinScannerStatus("Still looking for a VIN barcode...");
    } finally {
      vinScannerBusy = false;
    }
  }
  vinScannerFrame = requestAnimationFrame(() => scanVinFrame(detector, video));
}

async function acceptScannedVin(vin) {
  stopVinScanner();
  document.querySelector("#vehicleVin").value = vin;
  setVehicleLookupStatus("VIN scanned. Decoding...");
  await decodeVehicleVin();
}

function vehicleFromForm() {
  return {
    id: crypto.randomUUID(),
    year: document.querySelector("#vehicleYear").value.trim(),
    make: document.querySelector("#vehicleMake").value.trim(),
    model: selectedVehicleModel(),
    engine: document.querySelector("#vehicleEngine").value.trim(),
    trim: document.querySelector("#vehicleTrim").value.trim(),
    body: document.querySelector("#vehicleBody").value.trim(),
    vin: document.querySelector("#vehicleVin").value.trim().toUpperCase(),
    plate: document.querySelector("#vehiclePlate").value.trim().toUpperCase(),
    plateState: document.querySelector("#vehiclePlateState").value.trim().toUpperCase(),
    source: document.querySelector("#vehicleLookupStatus").dataset.source || ""
  };
}

function selectedVehicleModel() {
  const modelSelect = document.querySelector("#vehicleModel");
  if (modelSelect.value === MANUAL_MODEL_VALUE) {
    return document.querySelector("#vehicleModelCustom").value.trim();
  }
  return modelSelect.value.trim();
}

async function saveVehicleFromForm(event) {
  event.preventDefault();
  const customerId = document.querySelector("#vehicleCustomerId").value;
  const customer = state.customers.find((entry) => entry.id === customerId);
  if (!customer) return;
  customer.vehicles = customer.vehicles || [];
  customer.vehicles.push(vehicleFromForm());
  await saveState();
  closeVehicleDialog();
  render();
}

function applyDecodedVehicle(vehicle) {
  document.querySelector("#vehicleYear").value = vehicle.year || document.querySelector("#vehicleYear").value;
  document.querySelector("#vehicleMake").value = vehicle.make || document.querySelector("#vehicleMake").value;
  setVehicleModelOptions([], vehicle.model || selectedVehicleModel());
  document.querySelector("#vehicleEngine").value = vehicle.engine || document.querySelector("#vehicleEngine").value;
  document.querySelector("#vehicleTrim").value = vehicle.trim || "";
  document.querySelector("#vehicleBody").value = vehicle.body || "";
  document.querySelector("#vehicleVin").value = vehicle.vin || document.querySelector("#vehicleVin").value;
  document.querySelector("#vehicleLookupStatus").dataset.source = vehicle.source || "";
}

async function decodeVehicleVin() {
  const vin = document.querySelector("#vehicleVin").value.trim();
  const year = document.querySelector("#vehicleYear").value.trim();
  if (!vin) {
    setVehicleLookupStatus("Enter a VIN first.", true);
    return;
  }
  setVehicleLookupStatus("Decoding VIN with NHTSA vPIC...");
  try {
    const url = `${API_VEHICLE_DECODE_URL}?vin=${encodeURIComponent(vin)}${year ? `&year=${encodeURIComponent(year)}` : ""}`;
    const response = await fetch(url, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "VIN decode failed");
    applyDecodedVehicle(payload.vehicle || {});
    await loadVehicleModelSuggestions(payload.vehicle?.model || "");
    const warning = payload.warnings?.[0] ? ` ${payload.warnings[0]}` : "";
    setVehicleLookupStatus(`Decoded by NHTSA vPIC.${warning}`);
  } catch (error) {
    setVehicleLookupStatus(error.message, true);
  }
}

function resetVehicleModelOptions() {
  const modelSelect = document.querySelector("#vehicleModel");
  modelSelect.innerHTML = `
    <option value="">Enter year and make first</option>
    <option value="${MANUAL_MODEL_VALUE}">Other / manual entry</option>
  `;
  document.querySelector("#vehicleModelCustom").value = "";
  toggleManualVehicleModel();
}

function setVehicleModelOptions(models, selectedModel = "") {
  const modelSelect = document.querySelector("#vehicleModel");
  const normalizedModels = [...new Set((models || []).filter(Boolean))];
  const hasSelectedModel = selectedModel && normalizedModels.includes(selectedModel);
  const options = [
    `<option value="">${normalizedModels.length ? "Choose model" : "No model list loaded"}</option>`,
    ...normalizedModels.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`),
    `<option value="${MANUAL_MODEL_VALUE}">Other / manual entry</option>`
  ];
  modelSelect.innerHTML = options.join("");

  if (selectedModel && hasSelectedModel) {
    modelSelect.value = selectedModel;
    document.querySelector("#vehicleModelCustom").value = "";
  } else if (selectedModel) {
    modelSelect.value = MANUAL_MODEL_VALUE;
    document.querySelector("#vehicleModelCustom").value = selectedModel;
  }
  toggleManualVehicleModel();
}

function toggleManualVehicleModel() {
  const useManualModel = document.querySelector("#vehicleModel").value === MANUAL_MODEL_VALUE;
  const manualLabel = document.querySelector("#vehicleModelCustomLabel");
  const manualInput = document.querySelector("#vehicleModelCustom");
  manualLabel.hidden = !useManualModel;
  manualInput.required = useManualModel;
}

async function loadVehicleModelSuggestions(selectedModel = "") {
  const year = document.querySelector("#vehicleYear").value.trim();
  const make = document.querySelector("#vehicleMake").value.trim();
  if (!year || !make) {
    resetVehicleModelOptions();
    return;
  }
  setVehicleModelOptions([], selectedModel || selectedVehicleModel());
  try {
    const url = `${API_VEHICLE_MODELS_URL}?year=${encodeURIComponent(year)}&make=${encodeURIComponent(make)}`;
    const response = await fetch(url, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) return;
    setVehicleModelOptions(payload.models || [], selectedModel || selectedVehicleModel());
  } catch {
    // Model suggestions are helpful, but manual entry should never be blocked.
  }
}

async function loadCurrentUser() {
  try {
    const response = await fetch(API_AUTH_ME_URL, { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    const user = payload.user || {};
    document.querySelector("#signedInUser").textContent = user.displayName || user.email || user.username || "";
  } catch {
    document.querySelector("#signedInUser").textContent = "";
  }
}

async function loadAuthSettings() {
  try {
    const response = await fetch(API_AUTH_SETTINGS_URL, { cache: "no-store" });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload.settings || null;
  } catch {
    return null;
  }
}

async function saveAuthSettings(event) {
  event.preventDefault();
  setFormStatus("#authSettingsStatus", "Saving authentication settings...");
  try {
    const response = await fetch(API_AUTH_SETTINGS_URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        localEnabled: document.querySelector("#authLocalEnabled").checked,
        localUsername: document.querySelector("#authLocalUsername").value.trim(),
        localPassword: document.querySelector("#authLocalPassword").value,
        oidcEnabled: document.querySelector("#authOidcEnabled").checked,
        issuerUrl: document.querySelector("#authIssuerUrl").value.trim(),
        clientId: document.querySelector("#authClientId").value.trim(),
        clientSecret: document.querySelector("#authClientSecret").value,
        publicUrl: document.querySelector("#authPublicUrl").value.trim(),
        emailDomains: document.querySelector("#authEmailDomains").value.trim()
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Settings save failed");
    authSettings = payload.settings;
    document.querySelector("#authLocalPassword").value = "";
    document.querySelector("#authClientSecret").value = "";
    renderAuthSettings();
    setFormStatus("#authSettingsStatus", "Authentication settings saved.");
  } catch (error) {
    setFormStatus("#authSettingsStatus", error.message, true);
  }
}

function blankLine(type) {
  return type === "labor"
    ? { description: "", qty: 1, rate: 125 }
    : { description: "", qty: 1, rate: 0, cost: 0, supplier: "" };
}

function addLine(type, line = blankLine(type)) {
  const template = document.querySelector("#lineTemplate");
  const node = template.content.firstElementChild.cloneNode(true);
  node.dataset.type = type;
  node.querySelector(".line-description").value = line.description || "";
  node.querySelector(".line-qty").value = line.qty ?? 1;
  node.querySelector(".line-rate").value = line.rate ?? 0;
  node.querySelector(".remove-line").addEventListener("click", () => {
    node.remove();
    updateTotals();
  });
  node.querySelectorAll("input").forEach((input) => input.addEventListener("input", updateTotals));
  document.querySelector(type === "labor" ? "#laborLines" : "#partLines").append(node);
  updateTotals();
}

function readLines(selector) {
  return [...document.querySelectorAll(`${selector} .line-row`)].map((row) => ({
    description: row.querySelector(".line-description").value.trim(),
    qty: Number(row.querySelector(".line-qty").value) || 0,
    rate: Number(row.querySelector(".line-rate").value) || 0
  })).filter((line) => line.description || line.qty || line.rate);
}

function clearOrderForm() {
  document.querySelector("#orderForm").reset();
  document.querySelector("#orderId").value = "";
  document.querySelector("#laborLines").innerHTML = "";
  document.querySelector("#partLines").innerHTML = "";
  document.querySelector("#orderStatus").value = ORDER_STATUSES[0];
  addLine("labor");
  addLine("part");
  updateCurrentOrderActions();
  renderVehicleOptions();
}

function orderFromForm() {
  return {
    id: document.querySelector("#orderId").value || crypto.randomUUID(),
    customerId: document.querySelector("#orderCustomer").value,
    vehicleId: document.querySelector("#orderVehicle").value,
    status: normalizeOrderStatus(document.querySelector("#orderStatus").value),
    odometer: document.querySelector("#orderOdometer").value.trim(),
    concern: document.querySelector("#orderConcern").value.trim(),
    labor: readLines("#laborLines"),
    parts: readLines("#partLines"),
    updatedAt: new Date().toISOString()
  };
}

function fillOrderForm(order) {
  document.querySelector("#orderId").value = order.id;
  document.querySelector("#orderCustomer").value = order.customerId;
  renderVehicleOptions();
  document.querySelector("#orderVehicle").value = order.vehicleId || "";
  document.querySelector("#orderStatus").value = normalizeOrderStatus(order.status);
  document.querySelector("#orderOdometer").value = order.odometer || "";
  document.querySelector("#orderConcern").value = order.concern || "";
  document.querySelector("#laborLines").innerHTML = "";
  document.querySelector("#partLines").innerHTML = "";
  (order.labor?.length ? order.labor : [blankLine("labor")]).forEach((line) => addLine("labor", line));
  (order.parts?.length ? order.parts : [blankLine("part")]).forEach((line) => addLine("part", line));
  updateCurrentOrderActions();
  setView("orders");
}

function updateCurrentOrderActions() {
  const hasSavedOrder = Boolean(document.querySelector("#orderId").value);
  document.querySelector("#deleteCurrentOrder").disabled = !hasSavedOrder;
}

function orderTotal(order) {
  const subtotal = [...(order.labor || []), ...(order.parts || [])]
    .reduce((sum, line) => sum + (Number(line.qty) || 0) * (Number(line.rate) || 0), 0);
  const partsSubtotal = (order.parts || [])
    .reduce((sum, line) => sum + (Number(line.qty) || 0) * (Number(line.rate) || 0), 0);
  const tax = roundCurrency(partsSubtotal * TAX_RATE);
  return {
    subtotal: roundCurrency(subtotal),
    tax,
    total: roundCurrency(subtotal + tax)
  };
}

function updateTotals() {
  document.querySelectorAll(".line-row").forEach((row) => {
    const qty = Number(row.querySelector(".line-qty").value) || 0;
    const rate = Number(row.querySelector(".line-rate").value) || 0;
    row.querySelector(".line-total").textContent = money(qty * rate);
  });
  const total = orderTotal({
    labor: readLines("#laborLines"),
    parts: readLines("#partLines")
  });
  document.querySelector("#subtotalOut").textContent = money(total.subtotal);
  document.querySelector("#taxOut").textContent = money(total.tax);
  document.querySelector("#totalOut").textContent = money(total.total);
}

function retailPrice(cost) {
  if (cost < 25) return Number((cost * 1.8).toFixed(2));
  if (cost < 100) return Number((cost * 1.6).toFixed(2));
  if (cost < 300) return Number((cost * 1.4).toFixed(2));
  return Number((cost * 1.25).toFixed(2));
}

async function searchParts(event) {
  event.preventDefault();
  const targetOrderId = document.querySelector("#partsTargetOrder").value;
  const quotes = await fetchPartsQuotes();
  document.querySelector("#partsResults").innerHTML = quotes.map((quote) => `
    <article class="quote">
      <strong>${escapeHtml(quote.partName)}</strong>
      <span class="muted">${escapeHtml(quote.supplier)} - ${escapeHtml(quote.partNumber)}</span>
      <span>${escapeHtml(quote.description)}</span>
      <span class="quote-price">${money(quote.retail)}</span>
      <span class="muted">${money(quote.cost)} cost - ${quote.stock} in stock - ${escapeHtml(quote.eta)}</span>
      <button class="primary small" data-action="order-part"
        data-supplier="${escapeHtml(quote.supplier)}"
        data-part="${escapeHtml(quote.partName)}"
        data-cost="${quote.cost}"
        data-retail="${quote.retail}"
        data-eta="${escapeHtml(quote.eta)}">Track Order</button>
      ${targetOrderId ? `<button class="secondary small" data-action="add-quote-to-order"
        data-order-id="${escapeHtml(targetOrderId)}"
        data-part="${escapeHtml(quote.partName)}"
        data-retail="${quote.retail}"
        data-supplier="${escapeHtml(quote.supplier)}"
        data-number="${escapeHtml(quote.partNumber)}">Add to Estimate</button>` : ""}
    </article>
  `).join("");
}

async function fetchPartsQuotes() {
  const payload = {
    vehicleId: document.querySelector("#partsVehicle").value,
    category: document.querySelector("#partsCategory").value,
    keyword: document.querySelector("#partsKeyword").value.trim()
  };
  if (apiAvailable) {
    const response = await fetch(API_PARTS_SEARCH_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (response.ok) {
      const data = await response.json();
      return data.quotes || [];
    }
  }
  return localPartsSearch(payload);
}

function localPartsSearch({ vehicleId, category, keyword }) {
  const vehicle = allVehicles().find((entry) => entry.id === vehicleId);
  const base = {
    "Brake Pads": 46,
    "Rotor": 58,
    "Oil Filter": 8,
    "Air Filter": 17,
    "Battery": 142,
    "Alternator": 215,
    "Starter": 188
  }[category] || 40;
  const suppliers = [
    { name: "Mock NAPA Pro", eta: "Today 2:30 PM", stock: 4, factor: 1.08 },
    { name: "Mock O'Reilly Pro", eta: "Today 4:00 PM", stock: 2, factor: 0.98 },
    { name: "Mock Local Warehouse", eta: "Tomorrow AM", stock: 8, factor: 0.9 }
  ];

  return suppliers.map((supplier, index) => {
    const cost = Number((base * supplier.factor + index * 3).toFixed(2));
    return {
      id: crypto.randomUUID(),
      supplier: supplier.name,
      partName: `${vehicle ? `${vehicle.make} ${vehicle.model} ` : ""}${category}`,
      partNumber: `${category.slice(0, 3).toUpperCase()}-${String(base * 10 + index * 7).padStart(4, "0")}`,
      description: keyword ? `${keyword} match` : "Standard replacement part",
      cost,
      retail: retailPrice(cost),
      eta: supplier.eta,
      stock: supplier.stock
    };
  });
}

function startOrderForCustomer(customerId) {
  clearOrderForm();
  document.querySelector("#orderCustomer").value = customerId;
  renderVehicleOptions();
  const customer = state.customers.find((entry) => entry.id === customerId);
  if (customer?.vehicles?.[0]) {
    document.querySelector("#orderVehicle").value = customer.vehicles[0].id;
  }
  setView("orders");
}

function openPartsLookupForOrder(orderId) {
  const order = state.orders.find((entry) => entry.id === orderId);
  if (!order) return;
  setView("parts");
  document.querySelector("#partsTargetOrder").value = order.id;
  document.querySelector("#partsVehicle").value = order.vehicleId || "";
  refreshPartsLookupLinks();
}

function openPartsLookupForCurrentForm() {
  const orderId = document.querySelector("#orderId").value;
  if (orderId) {
    openPartsLookupForOrder(orderId);
    return;
  }
  setView("parts");
  document.querySelector("#partsTargetOrder").value = "";
  document.querySelector("#partsVehicle").value = document.querySelector("#orderVehicle").value || "";
  refreshPartsLookupLinks();
}

function handleListClick(event) {
  const control = event.target.closest("[data-action]");
  if (!control) return;
  event.preventDefault();
  const { action, id } = control.dataset;

  if (action === "edit-customer") {
    fillCustomerForm(state.customers.find((customer) => customer.id === id));
  }
  if (action === "add-vehicle") {
    openVehicleDialog(id);
  }
  if (action === "start-order") {
    startOrderForCustomer(id);
  }
  if (action === "edit-order") {
    fillOrderForm(state.orders.find((order) => order.id === id));
  }
  if (action === "open-order") {
    openOrder(id);
  }
  if (action === "lookup-parts") {
    openPartsLookupForOrder(id);
  }
  if (action === "duplicate-order") {
    const source = state.orders.find((order) => order.id === id);
    const copy = structuredClone(source);
    copy.id = crypto.randomUUID();
    copy.status = ORDER_STATUSES[0];
    copy.updatedAt = new Date().toISOString();
    state.orders.unshift(copy);
    saveState();
    render();
  }
  if (action === "delete-order") {
    deleteOrder(id);
  }
  if (action === "email-order") {
    emailOrder(id, control);
  }
  if (action === "part-status") {
    const partOrder = state.partsOrders.find((entry) => entry.id === id);
    partOrder.status = control.dataset.status;
    saveState();
    render();
  }
  if (action === "order-part") {
    state.partsOrders.unshift({
      id: crypto.randomUUID(),
      supplier: control.dataset.supplier,
      partName: control.dataset.part,
      cost: Number(control.dataset.cost),
      retail: Number(control.dataset.retail),
      eta: control.dataset.eta,
      status: "Quoted"
    });
    saveState();
    render();
  }
  if (action === "add-quote-to-order") {
    const order = state.orders.find((entry) => entry.id === control.dataset.orderId);
    if (!order) return;
    order.parts = order.parts || [];
    order.parts.push({
      description: `${control.dataset.part} (${control.dataset.supplier} ${control.dataset.number})`,
      qty: 1,
      rate: Number(control.dataset.retail) || 0
    });
    order.updatedAt = new Date().toISOString();
    saveState();
    render();
  }
}

function openOrder(orderId) {
  const order = state.orders.find((entry) => entry.id === orderId);
  if (order) fillOrderForm(order);
}

function openOrderFromHash() {
  const match = window.location.hash.match(/^#estimate-(.+)$/);
  if (!match) return;
  openOrder(decodeURIComponent(match[1]));
}

async function deleteOrder(orderId) {
  const order = state.orders.find((entry) => entry.id === orderId);
  if (!order) return;
  const customer = state.customers.find((entry) => entry.id === order.customerId);
  const label = customer?.name ? `${customer.name}'s ${order.status}` : order.status;
  if (!confirm(`Delete ${label}? This cannot be undone.`)) return;

  try {
    if (apiAvailable) {
      const response = await fetch(`/api/orders/${encodeURIComponent(orderId)}`, {
        method: "DELETE"
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "Delete failed");
      }
    }

    state.orders = state.orders.filter((entry) => entry.id !== orderId);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state, null, 2));
    if (!apiAvailable) {
      await saveState();
    }
    if (document.querySelector("#orderId").value === orderId) {
      clearOrderForm();
    }
    render();
  } catch (error) {
    alert(error.message);
  }
}

async function emailOrder(orderId, button) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Sending";
  try {
    const response = await fetch("/api/email/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ orderId })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "Email failed");
    }
    button.textContent = "Sent";
    setTimeout(() => {
      button.textContent = originalText;
      button.disabled = false;
    }, 1800);
  } catch (error) {
    alert(error.message);
    button.textContent = originalText;
    button.disabled = false;
  }
}

document.querySelectorAll(".nav-button").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

document.querySelector("#newCustomerQuick").addEventListener("click", () => {
  clearCustomerForm();
  setView("customers");
});

document.querySelector("#newOrderQuick").addEventListener("click", () => {
  clearOrderForm();
  setView("orders");
});

document.querySelector("#logoutButton").addEventListener("click", () => {
  window.location.href = "/logout";
});

document.querySelector("#customerForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const incoming = customerFromForm();
  const existing = state.customers.find((customer) => customer.id === incoming.id);
  if (existing) {
    Object.assign(existing, incoming, { vehicles: existing.vehicles || [] });
  } else {
    state.customers.unshift(incoming);
  }
  saveState();
  clearCustomerForm();
  render();
});

document.querySelector("#clearCustomerForm").addEventListener("click", clearCustomerForm);
document.querySelector("#customerSearch").addEventListener("input", renderCustomers);
document.querySelector("#authSettingsForm").addEventListener("submit", saveAuthSettings);
document.querySelector("#authPublicUrl").addEventListener("input", renderAuthSettings);
document.querySelector("#vehicleForm").addEventListener("submit", saveVehicleFromForm);
document.querySelector("#decodeVin").addEventListener("click", decodeVehicleVin);
document.querySelector("#scanVin").addEventListener("click", startVinScanner);
document.querySelector("#stopVinScanner").addEventListener("click", stopVinScanner);
document.querySelector("#cancelVehicle").addEventListener("click", closeVehicleDialog);
document.querySelector("#closeVehicleDialog").addEventListener("click", closeVehicleDialog);
document.querySelector("#vehicleModel").addEventListener("change", toggleManualVehicleModel);
document.querySelector("#vehicleYear").addEventListener("change", () => loadVehicleModelSuggestions());
document.querySelector("#vehicleMake").addEventListener("change", () => loadVehicleModelSuggestions());
document.querySelector("#orderCustomer").addEventListener("change", renderVehicleOptions);
document.querySelector("#orderFilter").addEventListener("change", renderOrders);
document.querySelector("#addLaborLine").addEventListener("click", () => addLine("labor"));
document.querySelector("#addPartLine").addEventListener("click", () => addLine("part"));
document.querySelector("#lookupPartsForCurrentOrder").addEventListener("click", openPartsLookupForCurrentForm);

document.querySelector("#orderForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const order = orderFromForm();
  const index = state.orders.findIndex((entry) => entry.id === order.id);
  if (index >= 0) state.orders[index] = order;
  else state.orders.unshift(order);
  saveState();
  render();
});

document.querySelector("#clearOrderForm").addEventListener("click", clearOrderForm);
document.querySelector("#printOrder").addEventListener("click", () => window.print());
document.querySelector("#deleteCurrentOrder").addEventListener("click", () => {
  const orderId = document.querySelector("#orderId").value;
  if (orderId) deleteOrder(orderId);
});
document.querySelector("#partsSearchForm").addEventListener("submit", searchParts);
document.querySelector("#partsVehicle").addEventListener("change", refreshPartsLookupLinks);
document.querySelector("#partsTargetOrder").addEventListener("change", (event) => {
  const order = state.orders.find((entry) => entry.id === event.target.value);
  if (order?.vehicleId) {
    document.querySelector("#partsVehicle").value = order.vehicleId;
  }
  refreshPartsLookupLinks();
});

document.querySelector("#clearCompletedParts").addEventListener("click", () => {
  state.partsOrders = state.partsOrders.filter((order) => order.status !== "Received");
  saveState();
  render();
});

document.querySelector("#exportData").addEventListener("click", () => {
  document.querySelector("#backupOutput").value = JSON.stringify(state, null, 2);
});

document.querySelector("#importData").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const text = await file.text();
  const imported = JSON.parse(text);
  state = imported;
  await saveState();
  render();
});

document.querySelector("#resetDemo").addEventListener("click", () => {
  if (!confirm("Replace local data with the starter demo?")) return;
  state = structuredClone(starterData);
  saveState();
  render();
});

document.body.addEventListener("click", handleListClick);

async function boot() {
  state = await loadState();
  partsProviders = await loadPartsProviders();
  authSettings = await loadAuthSettings();
  await loadCurrentUser();
  clearOrderForm();
  render();
  openOrderFromHash();
}

async function loadPartsProviders() {
  if (!apiAvailable) return [];
  try {
    const response = await fetch(API_PARTS_PROVIDERS_URL, { cache: "no-store" });
    if (!response.ok) return [];
    const data = await response.json();
    return data.providers || [];
  } catch {
    return [];
  }
}

boot();
window.addEventListener("hashchange", openOrderFromHash);

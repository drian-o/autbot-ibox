/* StockPilot: no external scripts; all server-provided text uses textContent. */
"use strict";

const $ = (id) => document.getElementById(id);
const moneyFormatter = new Intl.NumberFormat("id-ID", {style:"currency",currency:"IDR",maximumFractionDigits:0});
const money = (value) => value === null || value === undefined ? "Belum diperiksa" : moneyFormatter.format(Number(value));
let currentState = null;
let selectedProfile = null;
let refreshing = false;
let refreshAgain = false;
let disconnected = false;
const pendingActions = new Set();
let discoveryRequest = null;
let discoveryApplied = null;
let productFormProfile = null;
let discoveredOptions = null;
try { selectedProfile = Number(localStorage.getItem("stockpilot-profile")) || null; } catch (_) {}

const productStatuses = {
  paused:["Dijeda","neutral"], watching:["Dalam pantauan","info"], out_of_stock:["Stok habis","neutral"], available:["Stok tersedia","good"], ready:["Stok tersedia","good"], manual_checkout:["Checkout di browser","warning"],
  price_above_limit:["Di atas batas harga","warning"], budget_blocked:["Anggaran tidak cukup","warning"],
  needs_action:["Perlu tindakan","warning"], error:["Perlu diperiksa","danger"],
  ordered:["Pesanan dibuat","good"], completed:["Target selesai","good"]
};
const orderStatuses = {
  preparing:["Menyiapkan checkout","info"], submitting:["Mengirim pesanan","info"], manual_checkout:["Checkout di browser","warning"], handoff:["Checkout di browser","warning"],
  awaiting_payment:["Menunggu pembayaran","warning"], needs_action:["Perlu tindakan","warning"],
  unknown:["Status belum pasti","danger"], paid_manual:["Dibayar · catatan pengguna","good"],
  cancelled_manual:["Dibatalkan · catatan pengguna","neutral"]
};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}
function icon(name, extraClass) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon" + (extraClass ? " " + extraClass : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}
function actionButton(label, symbol, className, action) {
  const button = node("button", className || "button button-secondary", label);
  button.type = "button";
  if (symbol) button.prepend(icon(symbol));
  button.addEventListener("click", action);
  return button;
}
function badge(status, dictionary) {
  const item = dictionary[status] || [status || "Belum diperiksa", "neutral"];
  const badgeNode = node("span", "status-badge " + item[1]);
  badgeNode.append(node("span", "status-dot"), document.createTextNode(item[0]));
  return badgeNode;
}
function safeURL(value) {
  try {
    if (typeof value !== "string" || !value.trim()) return null;
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    return url.href;
  } catch (_) { return null; }
}
function productURL(value) {
  const safe = safeURL(value);
  if (!safe) return null;
  const url = new URL(safe);
  return ["ibox.co.id", "www.ibox.co.id"].includes(url.hostname) && (!url.port || url.port === "443") && url.pathname.startsWith("/product/") && url.pathname.length > 9 ? url.href : null;
}
function parseDate(value) {
  if (!value) return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function dateTime(value) {
  const date = parseDate(value);
  return date ? date.toLocaleString("id-ID", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "—";
}
function shortTime(value) {
  const date = parseDate(value);
  return date ? date.toLocaleTimeString("id-ID", {hour:"2-digit",minute:"2-digit"}) : "—";
}
function showToast(message, isError = false) {
  const toast = node("div", "toast" + (isError ? " error" : ""));
  toast.append(icon(isError ? "info" : "check"), node("span", "", message));
  $("toast-region").append(toast);
  setTimeout(() => toast.remove(), isError ? 7000 : 4200);
}
function setError(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}
function currentProfile() {
  return (currentState?.profiles || []).find((p) => Number(p.id) === Number(selectedProfile));
}
function scopedProducts() {
  return (currentState?.products || []).filter((p) => Number(p.profile_id) === Number(selectedProfile));
}
function scopedOrders() {
  return (currentState?.orders || []).filter((p) => Number(p.profile_id) === Number(selectedProfile));
}
function openModal(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}

async function post(path, payload = {}) {
  const response = await fetch(path, {
    method:"POST", credentials:"same-origin",
    headers:{"Content-Type":"application/json","X-CSRF-Token":currentState?.csrf || ""},
    body:JSON.stringify(payload)
  });
  let data;
  try { data = await response.json(); } catch (_) { throw new Error("Respons server tidak dapat dibaca. Periksa terminal aplikasi."); }
  if (!response.ok || data.error) throw new Error(data.error || "Tindakan belum berhasil. Coba periksa status aplikasi.");
  return data;
}

async function withAction(key, action, successMessage = "") {
  if (pendingActions.has(key)) return;
  pendingActions.add(key);
  renderMonitor();
  try {
    await action();
    if (successMessage) showToast(successMessage);
    await refreshState();
  } catch (error) {
    showToast(error.message || "Tidak dapat menyelesaikan tindakan.", true);
  } finally {
    pendingActions.delete(key);
    renderMonitor();
  }
}

async function refreshState() {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    const response = await fetch("/api/state", {credentials:"same-origin",cache:"no-store"});
    if (!response.ok) throw new Error("Status server tidak tersedia.");
    const data = await response.json();
    if (!Array.isArray(data.profiles) || !Array.isArray(data.products)) throw new Error("Format status server tidak sesuai.");
    currentState = data;
    disconnected = false;
    $("connection-status").className = "connection connected";
    $("connection-status").replaceChildren(node("span", "status-dot"), document.createTextNode("Dashboard terhubung"));
    render();
  } catch (_) {
    if (!disconnected) showToast("Koneksi ke aplikasi terputus. Pastikan Python masih berjalan di terminal.", true);
    disconnected = true;
    $("connection-status").className = "connection offline";
    $("connection-status").replaceChildren(node("span", "status-dot"), document.createTextNode("Dashboard terputus"));
    $("footer-status").textContent = "Koneksi terputus — mencoba kembali…";
  } finally {
    refreshing = false;
    if (refreshAgain) { refreshAgain = false; setTimeout(refreshState, 0); }
  }
}

function renderProfiles() {
  const profiles = currentState.profiles;
  if (!profiles.some((p) => Number(p.id) === Number(selectedProfile))) selectedProfile = profiles[0]?.id ?? null;
  const select = $("profile-select");
  const serialized = profiles.map((p) => [p.id, p.name]);
  if (select.dataset.profiles !== JSON.stringify(serialized)) {
    select.replaceChildren();
    for (const profile of profiles) {
      const option = node("option", "", profile.name);
      option.value = profile.id;
      select.append(option);
    }
    select.dataset.profiles = JSON.stringify(serialized);
  }
  select.value = selectedProfile ?? "";
  const profile = currentProfile();
  const committed = Number(profile?.committed || 0);
  const budget = Number(profile?.budget || 0);
  $("budget-used").textContent = money(committed);
  $("budget-limit").textContent = money(budget);
  const percentage = budget > 0 ? Math.min(100, committed / budget * 100) : 0;
  $("budget-progress-fill").style.width = percentage + "%";
  $("budget-progress-fill").classList.toggle("full", percentage >= 90);
  $("budget-progress").setAttribute("aria-valuenow", String(Math.round(percentage)));
  $("edit-budget").disabled = !profile;
  $("add-product").disabled = !profile;
  $("profile-product-label").textContent = profile ? "Profil: " + profile.name : "";
}

function renderMonitor() {
  if (!currentState) return;
  const monitor = currentState.monitor || {};
  const running = Boolean(monitor.running);
  const busy = Boolean(monitor.busy);
  const capabilities = currentState.capabilities || {};
  const enabled = currentState.adapter_ready !== false && capabilities.inspect !== false;
  const loginEnabled = currentState.login_ready !== false && capabilities.login !== false;
  $("mode-chip").textContent = "IBOX LIVE";
  $("mode-chip").classList.add("live");
  $("adapter-alert").hidden = enabled;
  $("adapter-alert-text").textContent = currentState.adapter_message || "Browser iBox belum siap. Periksa petunjuk instalasi dan terminal aplikasi.";
  $("monitor-status").className = "monitor-pill" + (running ? " running" : "") + (busy ? " busy" : "");
  $("monitor-status").replaceChildren(node("span", "status-dot"), document.createTextNode(busy ? "Sedang memeriksa" : running ? "Pemantauan aktif" : "Pemantauan dijeda"));
  $("monitor-headline").textContent = busy ? "Membaca halaman iBox…" : running ? "Incaran aktif sedang dipantau." : "Pilihanmu sudah siap dipantau?";
  $("monitor-description").textContent = monitor.last_error || (running ? "Harga, stok, dan varian dibaca langsung dari iBox. Aksi mengikuti pengaturan tiap incaran." : "Tambahkan link produk dan pilih aksinya. Mulai pantau berlaku untuk seluruh profil.");
  $("monitor-interval").textContent = "Setiap " + (monitor.interval || "—") + " detik";
  $("start-monitor").hidden = running;
  $("stop-monitor").hidden = !running;
  $("start-monitor").disabled = !enabled || pendingActions.has("monitor");
  $("stop-monitor").disabled = pendingActions.has("monitor");
  $("check-once").disabled = !enabled || busy || pendingActions.has("check");
  $("login-browser").disabled = !loginEnabled || !selectedProfile || busy || pendingActions.has("login");
  $("footer-status").textContent = busy ? "Pemeriksaan sedang berlangsung" : running ? "Pemantauan aktif · seluruh profil" : "Mesin pemantau dijeda";
  const connection = currentState.connection || {};
  const states = {
    not_checked: ["Koneksi iBox belum diperiksa", "Belum diperiksa", "neutral"],
    checking: ["Sedang membaca halaman iBox", "Memeriksa", "info"],
    connected: ["Halaman iBox berhasil dibaca", "Pembacaan berhasil", "good"],
    needs_action: ["Browser iBox perlu tindakan", "Perlu tindakan", "warning"],
    error: ["Koneksi iBox perlu diperiksa", "Belum berhasil", "danger"]
  };
  const state = states[connection.status] || states.not_checked;
  $("store-connection-title").textContent = state[0];
  $("store-connection-badge").textContent = state[1];
  $("store-connection-badge").className = "status-badge " + state[2];
  $("store-connection-message").textContent = (connection.message || "Buka iBox untuk login, lalu baca link produk atau jalankan pemeriksaan stok.") + (connection.last_checked ? " · " + dateTime(connection.last_checked) : "");
  $("checkout-mode-message").textContent = currentState.checkout_message || capabilities.checkout_message || (capabilities.checkout ? "Checkout otomatis tersedia jika dipilih pada incaran. Pembayaran tetap diselesaikan di halaman iBox." : "Aksi “Siapkan di browser” membuka produk dan varian untuk dilanjutkan di iBox. Checkout otomatis belum diaktifkan.");
  const automatic = $("product-action").querySelector('option[value="auto"]');
  automatic.disabled = !capabilities.checkout;
  automatic.textContent = capabilities.checkout ? "Checkout otomatis — sampai instruksi pembayaran" : "Checkout otomatis — pemetaan belum siap";
  $("discover-product").disabled = !enabled || Boolean(discoveryRequest && currentState.discovery?.busy) || pendingActions.has("discover");
}

function renderStats() {
  const products = scopedProducts();
  const orders = scopedOrders();
  $("active-count").textContent = products.filter((p) => Boolean(p.active)).length;
  $("available-count").textContent = products.filter((p) => p.last_checked && (p.last_stock === true || p.last_stock === 1)).length;
  $("payment-count").textContent = orders.filter((o) => o.status === "awaiting_payment").length;
  const checked = products.map((p) => parseDate(p.last_checked)).filter(Boolean).sort((a,b) => b - a);
  $("last-check").textContent = checked.length ? shortTime(checked[0].toISOString()) : "—";
  $("last-check-caption").textContent = checked.length ? checked[0].toLocaleDateString("id-ID", {day:"numeric",month:"long"}) : "Belum ada pemeriksaan";
  $("nav-product-count").textContent = products.length;
  $("nav-order-count").textContent = orders.length;
}

function renderProducts() {
  const all = scopedProducts();
  const query = $("product-search").value.toLocaleLowerCase("id-ID").trim();
  const filter = $("product-filter").value;
  const products = all.filter((product) => {
    const matchesText = !query || (product.name + " " + product.variant).toLocaleLowerCase("id-ID").includes(query);
    const matchesStatus = filter === "all" || filter === "active" && Boolean(product.active) || filter === "attention" && ["needs_action","error","budget_blocked","price_above_limit"].includes(product.status);
    return matchesText && matchesStatus;
  }).sort((a,b) => Number(a.priority) - Number(b.priority) || Number(a.id) - Number(b.id));
  $("wishlist-count").textContent = all.length;
  $("products-empty").hidden = all.length > 0;
  const body = $("products-body");
  body.replaceChildren();
  if (all.length && !products.length) {
    const row = node("tr");
    const cell = node("td", "no-results", "Tidak ada produk yang cocok dengan pencarian atau filter ini.");
    cell.colSpan = 6;
    row.append(cell);body.append(row);
  }
  for (const product of products) {
    const row = node("tr");
    const productCell = node("td");
    const main = node("div", "product-main");
    const visual = node("span", "product-visual");visual.append(icon("box"));
    const info = node("div");
    const title = node("div", "product-title");
    const url = safeURL(product.url);
    if (url) {
      const link = node("a", "", product.name);link.href=url;link.target="_blank";link.rel="noopener noreferrer";title.append(link);
    } else title.textContent=product.name;
    const actionLabels = {notify:"Pantau saja", assist:"Siapkan di browser", auto:"Checkout otomatis"};
    info.append(title, node("div", "product-meta", [product.color, product.capacity].filter(Boolean).join(" · ") || product.variant || "Varian sesuai link"), node("div", "product-priority", "Prioritas " + product.priority + " · " + (actionLabels[product.action] || "Pantau saja")));
    main.append(visual, info);productCell.append(main);
    const priceCell = node("td");
    priceCell.append(node("div", "money-value", money(product.last_checked ? product.last_price : null)), node("div", "money-limit", "Batas " + money(product.max_unit_price)), node("div", "money-limit", "Total ≤ " + money(product.max_total_price)));
    const quantityCell = node("td");quantityCell.append(node("div", "quantity-value", product.quantity + " unit"));
    const statusCell = node("td");statusCell.append(badge(product.status, productStatuses));
    if (product.note) statusCell.append(node("div", "product-note", product.note));
    statusCell.append(node("div", "product-note", product.last_checked ? dateTime(product.last_checked) : "Belum diperiksa di iBox"));
    const toggleCell = node("td");
    const toggle = node("button", "toggle");toggle.type="button";toggle.setAttribute("role","switch");toggle.setAttribute("aria-checked", String(Boolean(product.active)));toggle.setAttribute("aria-label", (product.active ? "Jeda pantauan " : "Aktifkan pantauan ") + product.name);
    toggle.disabled=pendingActions.has("product-"+product.id);
    toggle.addEventListener("click", () => withAction("product-"+product.id, () => post("/api/products/"+product.id+"/toggle", {active:!Boolean(product.active)}), product.active ? "Pantauan produk dijeda." : "Pantauan produk diaktifkan."));
    toggleCell.append(toggle);
    const actionsCell=node("td");const actions=node("div","row-actions");
    const edit=actionButton("", "edit", "icon-button", () => openProduct(product));edit.setAttribute("aria-label","Edit "+product.name);edit.title="Edit incaran";
    const remove=actionButton("", "trash", "icon-button danger", () => {
      if (window.confirm('Hapus "'+product.name+'" dari daftar incaran? Riwayat pesanan tetap disimpan.')) withAction("product-"+product.id, () => post("/api/products/"+product.id+"/delete"), "Incaran dihapus.");
    });remove.setAttribute("aria-label","Hapus "+product.name);remove.title="Hapus incaran";
    const open=actionButton("", "external", "icon-button", () => withAction("open-"+product.id, () => post("/api/products/"+product.id+"/open"), "Permintaan membuka produk dikirim. Lanjutkan di browser iBox."));open.setAttribute("aria-label","Buka "+product.name+" di browser iBox");open.title="Buka di browser iBox";
    open.disabled = pendingActions.has("open-"+product.id) || currentState.login_ready === false;
    actions.append(open,edit,remove);actionsCell.append(actions);
    row.append(productCell,priceCell,quantityCell,statusCell,toggleCell,actionsCell);body.append(row);
  }
}

function renderOrders() {
  const orders=scopedOrders().slice().sort((a,b)=>(parseDate(b.created_at)?.getTime()||0)-(parseDate(a.created_at)?.getTime()||0));
  $("orders-count").textContent=orders.length;
  $("orders-empty").hidden=orders.length>0;
  const list=$("orders-list");list.replaceChildren();
  for (const order of orders) {
    const card=node("article","order-card");
    const heading=node("div","order-heading");
    const details=node("div");
    details.append(node("div","order-title",order.product_name || "Pesanan #"+order.id),node("div","order-meta",(order.order_ref ? "Pesanan iBox: "+order.order_ref : "Persiapan lokal #"+String(order.id).slice(0,8))+" · "+order.quantity+" unit · "+dateTime(order.created_at)));
    heading.append(details,badge(order.status,orderStatuses));card.append(heading);
    if (order.note) card.append(node("p","order-note",order.note));
    const bottom=node("div","order-bottom");
    const amount = node("div", "order-amount-wrap");amount.append(node("span", "order-amount", money(order.total ?? order.reserved_total)), node("small", "order-amount-caption", order.total == null ? "Alokasi maksimum" : "Total tercatat"));bottom.append(amount);
    const actions=node("div","order-actions");
    const url=safeURL(order.payment_url);
    if (url && !["paid_manual","cancelled_manual"].includes(order.status)) {
      const payment=node("a","button button-small button-primary","Buka pembayaran");
      payment.prepend(icon("external"));payment.href=url;payment.target="_blank";payment.rel="noopener noreferrer";actions.append(payment);
    }
    if (["manual_checkout", "handoff", "needs_action", "unknown"].includes(order.status)) {
      const reopen = actionButton("Lanjutkan di browser iBox", "external", "button button-small button-secondary", () => withAction("order-open-"+order.id, () => post("/api/orders/"+order.id+"/open"), "Browser iBox diminta dibuka. Periksa checkout di jendela tersebut."));
      reopen.disabled = pendingActions.has("order-open-"+order.id);actions.append(reopen);
    }
    if (["awaiting_payment","needs_action","unknown","manual_checkout","handoff"].includes(order.status)) actions.append(actionButton("Perbarui status",null,"button button-small button-secondary",()=>openResolve(order)));
    bottom.append(actions);card.append(bottom);list.append(card);
  }
}

function renderEvents() {
  const events=(currentState.events || []).slice(0,30);
  $("events-empty").hidden=events.length>0;
  const list=$("events-list");list.replaceChildren();
  for (const event of events) {
    const item=node("li","activity-item");
    const level=["warning","error","info"].includes(event.level) ? event.level : "";
    const content=node("div");content.append(node("div","event-message",event.message),node("div","event-time",dateTime(event.created_at)));
    item.append(node("span","event-dot "+level),content);list.append(item);
  }
}

function render() { renderProfiles();renderMonitor();renderStats();renderProducts();renderOrders();renderEvents();renderDiscovery(); }

function openProduct(product=null) {
  if (!currentProfile()) { showToast("Buat profil belanja terlebih dahulu.",true);return; }
  $("product-form").reset();setError("product-error","");
  productFormProfile = Number(selectedProfile);
  discoveryRequest = null;discoveryApplied = null;discoveredOptions = null;
  $("discovery-feedback").hidden = true;
  $("product-colors").replaceChildren();$("product-capacities").replaceChildren();
  $("product-id").value=product?.id ?? "";
  $("product-dialog-title").textContent=product ? "Edit incaran" : "Tambah incaran";
  $("product-name").value=product?.name || "";
  $("product-url").value=product?.url || "";
  $("product-color").value=product?.color || "";
  $("product-capacity").value=product?.capacity || "";
  $("product-action").value=product?.action || "notify";
  $("product-quantity").value=product?.quantity ?? 1;
  $("product-max-unit").value=product?.max_unit_price || "";
  $("product-max-total").value=product?.max_total_price || "";
  const priority=String(product?.priority ?? 2);
  if (![...$("product-priority").options].some((option)=>option.value===priority)) {
    const option=node("option","",priority+" — Prioritas tersimpan");option.value=priority;$("product-priority").append(option);
  }
  $("product-priority").value=priority;
  $("product-active").checked=Boolean(product?.active);
  updateCurrencyPreview();updateActionHelp();updateVariantSummary();
  openModal("product-dialog");
}
function openResolve(order) {
  $("resolve-form").reset();setError("resolve-error","");
  $("resolve-order-id").value=order.id;
  $("resolve-product-name").textContent=(order.product_name || "Checkout")+" · "+(order.order_ref ? "Pesanan iBox: "+order.order_ref : "Persiapan lokal #"+String(order.id).slice(0,8));
  openModal("resolve-dialog");
}
async function submitForm(form,event,errorId,action,successMessage,dialogId) {
  event.preventDefault();setError(errorId,"");
  const submit=form.querySelector('button[type="submit"]');
  if (submit.disabled) return;
  submit.disabled=true;
  try {await action();$(dialogId).close();showToast(successMessage);await refreshState();}
  catch(error){setError(errorId,error.message || "Data belum dapat disimpan.");}
  finally{submit.disabled=false;}
}

$("add-product").addEventListener("click",()=>openProduct());
$("empty-add-product").addEventListener("click",()=>openProduct());
$("product-search").addEventListener("input",()=>currentState&&renderProducts());
$("product-filter").addEventListener("change",()=>currentState&&renderProducts());
$("profile-select").addEventListener("change",(event)=>{
  selectedProfile=Number(event.target.value);
  try{localStorage.setItem("stockpilot-profile",String(selectedProfile));}catch(_){}
  render();
});
$("start-monitor").addEventListener("click",()=>withAction("monitor",()=>post("/api/monitor/start"),"Pemantauan dimulai untuk seluruh incaran aktif."));
$("stop-monitor").addEventListener("click",()=>withAction("monitor",()=>post("/api/monitor/stop"),"Pemantauan dihentikan. Proses yang sudah berjalan akan diselesaikan."));
$("check-once").addEventListener("click",()=>withAction("check",()=>post("/api/monitor/check"),"Pemeriksaan sekali diminta."));
$("login-browser").addEventListener("click",()=>withAction("login",()=>post("/api/login",{profile_id:Number(selectedProfile)}),"Permintaan membuka iBox dikirim. Login langsung di jendela browser iBox."));
$("add-profile").addEventListener("click",()=>{$("profile-form").reset();setError("profile-error","");updateOtherCurrencyPreviews();openModal("profile-dialog");});
$("edit-budget").addEventListener("click",()=>{$("budget-input").value=currentProfile()?.budget || "";setError("budget-error","");updateOtherCurrencyPreviews();openModal("budget-dialog");});
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click",()=>$(button.dataset.close).close());
for (const link of document.querySelectorAll(".nav-link")) link.addEventListener("click",()=>{document.querySelectorAll(".nav-link").forEach((item)=>item.classList.toggle("active",item===link));});

$("product-form").addEventListener("submit",(event)=>submitForm($("product-form"),event,"product-error",async()=>{
  const url=$("product-url").value.trim();
  if(!productURL(url)) throw new Error("Gunakan link produk HTTPS dari ibox.co.id, misalnya https://ibox.co.id/product/…");
  const color=$("product-color").value.trim(), capacity=$("product-capacity").value.trim();
  const payload={profile_id:productFormProfile,name:$("product-name").value.trim(),url,color,capacity,variant:[color,capacity].filter(Boolean).join(" · "),action:$("product-action").value,quantity:Number($("product-quantity").value),max_unit_price:Number($("product-max-unit").value),max_total_price:Number($("product-max-total").value),priority:Number($("product-priority").value),active:$("product-active").checked};
  const id=$("product-id").value;
  await post(id ? "/api/products/"+id : "/api/products",payload);
},"Incaran berhasil disimpan.","product-dialog"));

$("profile-form").addEventListener("submit",(event)=>submitForm($("profile-form"),event,"profile-error",async()=>{
  const result=await post("/api/profiles",{name:$("profile-name").value.trim(),budget:Number($("profile-budget").value)});
  const id=result.id || result.profile?.id || result.profile_id;
  if(id) selectedProfile=Number(id);
},"Profil belanja dibuat.","profile-dialog"));

$("budget-form").addEventListener("submit",(event)=>submitForm($("budget-form"),event,"budget-error",()=>post("/api/budget",{profile_id:Number(selectedProfile),budget:Number($("budget-input").value)}),"Anggaran profil diperbarui.","budget-dialog"));

$("resolve-form").addEventListener("submit",(event)=>submitForm($("resolve-form"),event,"resolve-error",()=>post("/api/orders/"+$("resolve-order-id").value+"/resolve",{status:$("resolve-status").value,note:$("resolve-note").value.trim()}),"Hasil pemeriksaan dicatat. Aktifkan kembali incaran hanya jika memang ingin memesan ulang.","resolve-dialog"));

function updateCurrencyPreview() {
  const unit = Number($("product-max-unit").value);
  const total = Number($("product-max-total").value);
  const quantity = Number($("product-quantity").value) || 1;
  $("unit-preview").textContent = $("product-max-unit").value ? "Batas per unit: " + money(unit) : "Contoh: 74000000 = Rp74.000.000. Angka 74 berarti Rp74.";
  $("total-preview").textContent = $("product-max-total").value ? "Batas total: " + money(total) : "Batas total untuk seluruh jumlah target.";
  const profile = currentState?.profiles?.find(p => Number(p.id) === productFormProfile);
  let summary = unit > 0 ? quantity + " × " + money(unit) + " = " + money(quantity * unit) + ". Tambahkan ruang untuk ongkir dan biaya pada batas total." : "Batas belanja tetap mengikuti angka yang kamu isi.";
  if (total > 0 && unit > 0 && total < unit * quantity) summary += " Batas total masih kurang.";
  if (profile && Number(profile.budget) === 0) summary += " Anggaran profil belum diatur.";
  else if (profile && total > Math.max(0, Number(profile.budget) - Number(profile.committed || 0))) summary += " Total ini melebihi sisa anggaran profil.";
  $("limit-summary").textContent = summary;
}
function updateOtherCurrencyPreviews() {
  for (const [input, preview] of [["profile-budget", "profile-budget-preview"], ["budget-input", "budget-preview"]]) {
    $(preview).textContent = $(input).value ? "Anggaran: " + money(Number($(input).value)) : "Nominal Rupiah penuh, bukan dalam juta.";
  }
}
function updateActionHelp() {
  const help = {
    notify: "Hasil pemeriksaan tampil di dashboard. Tidak membuka checkout otomatis.",
    assist: "Saat syarat terpenuhi, produk dan varian disiapkan di browser iBox. Kamu melanjutkan keranjang, pengiriman, dan pembayaran di sana.",
    auto: "Aplikasi memeriksa batas belanja sebelum mengirim pesanan. Instruksi pembayaran yang terbaca belum berarti lunas."
  };
  $("product-action-help").textContent = help[$("product-action").value] || help.notify;
}
function updateVariantSummary() {
  if (!discoveredOptions) {
    $("variant-summary").textContent = "Warna dan kapasitas diperiksa sebagai satu pilihan. Pilihan yang tidak tersedia tidak akan diganti otomatis.";
    return;
  }
  const notes = [];
  for (const [field, key] of [["product-color", "colors"], ["product-capacity", "capacities"]]) {
    const value = $(field).value.trim();
    const option = (discoveredOptions[key] || []).find(item => item.value === value);
    if (value && option?.available === false) notes.push(value + ": tidak tersedia saat halaman dibaca");
    else if (value && !option && (discoveredOptions[key] || []).length) notes.push(value + ": belum ditemukan pada pilihan halaman");
  }
  $("variant-summary").textContent = notes.length ? notes.join(". ") + ". Tetap bisa dipantau; pilihan tidak diganti otomatis." : "Ketersediaan kombinasi warna dan kapasitas diperiksa ulang saat memantau.";
}
function setDiscoveryFeedback(message, kind = "") {
  $("discovery-feedback").hidden = !message;
  $("discovery-feedback").className = "discovery-feedback" + (kind ? " " + kind : "");
  $("discovery-feedback").textContent = message;
}
function renderDiscovery() {
  if (!$("product-dialog").open || !discoveryRequest) return;
  const state = currentState?.discovery;
  if (!state || String(state.request_id) !== String(discoveryRequest.id)) return;
  if (Number(state.profile_id) !== productFormProfile || $("product-url").value.trim() !== discoveryRequest.url) return;
  if (state.busy) { setDiscoveryFeedback("Browser sedang membaca halaman produk iBox…");return; }
  if (discoveryApplied === discoveryRequest.id) return;
  discoveryApplied = discoveryRequest.id;
  if (state.error) { setDiscoveryFeedback(state.error, "error");return; }
  const result = state.result;
  if (!result) return;
  discoveredOptions = result;
  if (result.name) $("product-name").value = result.name;
  for (const [key, field, listId, selectedKey] of [["colors", "product-color", "product-colors", "selected_color"], ["capacities", "product-capacity", "product-capacities", "selected_capacity"]]) {
    const list = $(listId);list.replaceChildren();
    for (const item of result[key] || []) {
      if (!item || typeof item.value !== "string") continue;
      const option = node("option");option.value = item.value;
      option.label = item.available === false ? "Tidak tersedia saat dibaca" : item.selected ? "Pilihan pada halaman" : item.value;
      list.append(option);
    }
    if (!$(field).value.trim()) {
      const selected = result[selectedKey] || (result[key] || []).find(item => item.selected)?.value || "";
      $(field).value = selected;
    }
  }
  const price = result.price ?? result.unit_price;
  const selection = [result.selected_color, result.selected_capacity].filter(Boolean).join(" · ");
  const availability = result.available === false ? "Stok pada pilihan halaman: habis." : result.available === true ? "Stok pada pilihan halaman: tersedia." : "Status stok belum pasti.";
  setDiscoveryFeedback("Halaman berhasil dibaca" + (selection ? " · " + selection : "") + ". " + (price != null ? "Harga yang terbaca " + money(price) + ". " : "") + availability + " Batas belanja tidak diubah.", "success");
  updateVariantSummary();updateCurrencyPreview();
}
$("discover-product").addEventListener("click", async () => {
  const url = $("product-url").value.trim();
  if (!productURL(url)) {setError("product-error", "Tempel link produk HTTPS dari ibox.co.id terlebih dahulu.");return;}
  if (pendingActions.has("discover")) return;
  setError("product-error", "");pendingActions.add("discover");renderMonitor();
  setDiscoveryFeedback("Mengantrekan pembacaan halaman produk iBox…");
  try {
    const response = await post("/api/products/discover", {url, profile_id:productFormProfile});
    discoveryRequest = {id:response.request_id, url, profile_id:productFormProfile};
    discoveryApplied = null;
    await refreshState();
  } catch (error) {setDiscoveryFeedback(error.message || "Halaman belum dapat dibaca.", "error");}
  finally {pendingActions.delete("discover");renderMonitor();}
});
$("product-url").addEventListener("input", () => {
  discoveryRequest = null;discoveryApplied = null;discoveredOptions = null;
  $("product-colors").replaceChildren();$("product-capacities").replaceChildren();
  setDiscoveryFeedback("");updateVariantSummary();renderMonitor();
});
for (const field of ["product-max-unit", "product-max-total", "product-quantity"]) $(field).addEventListener("input", updateCurrencyPreview);
for (const field of ["profile-budget", "budget-input"]) $(field).addEventListener("input", updateOtherCurrencyPreviews);
for (const field of ["product-color", "product-capacity"]) $(field).addEventListener("input", updateVariantSummary);
$("product-action").addEventListener("change", updateActionHelp);

refreshState();
setInterval(refreshState,3000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshState();});

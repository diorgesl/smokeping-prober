const state = { targets: [], categories: [], routers: [], query: "", category: "", status: "", deleteId: null, deleteKind: "target" };
const $ = (selector) => document.querySelector(selector);
const cards = $("#cards");
const dialog = $("#targetDialog");
const form = $("#targetForm");
const routerCards = $("#routerCards");
const routerDialog = $("#routerDialog");
const routerForm = $("#routerForm");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}
function toast(message, error=false) {
  const node = document.createElement("div"); node.className = `toast${error ? " error" : ""}`; node.textContent = message;
  $("#toasts").append(node); setTimeout(() => node.remove(), 4500);
}
async function api(path, options={}) {
  const response = await fetch(path, {headers:{"Content-Type":"application/json", ...(options.headers||{})}, ...options});
  let body = {}; try { body = await response.json(); } catch {}
  if (!response.ok) {
    // FastAPI validation errors come as a list of {loc, msg}.
    const detail = Array.isArray(body.detail) ? body.detail.map(d => String(d.msg || d).replace(/^Value error, /, "")).join("; ") : body.detail;
    throw new Error(detail || `Erro HTTP ${response.status}`);
  }
  return body;
}
function metric(value, unit) { return value == null ? "—" : `${Number(value).toFixed(value >= 100 ? 0 : 1)}<small>${unit}</small>`; }
function statusLabel(status) { return ({healthy:"Normal",warning:"Atenção",critical:"Crítico",down:"Indisponível",unknown:"Sem dados"})[status] || status; }
function matchesStatus(t) {
  if (!state.status) return true;
  if (state.status === "healthy") return t.status === "healthy";
  if (state.status === "alert") return ["warning","critical","down"].includes(t.status);
  return true;
}
function lossClass(loss) { return loss >= 20 ? "bad" : loss >= 5 ? "warn" : ""; }
function linkMetrics(m) {
  if (!m?.links?.length) return "";
  return `<div class="link-metrics"><div><span>Link</span><span>Latência</span><span>Perda</span><span>Jitter</span></div>${m.links.map(l =>
    `<div><b title="${escapeHtml(l.name)}">${escapeHtml(l.name)}</b><span>${metric(l.latency," ms")}</span><span class="${lossClass(l.loss)}">${metric(l.loss,"%")}</span><span>${metric(l.jitter," ms")}</span></div>`).join("")}</div>`;
}
function render() {
  const q = state.query.toLocaleLowerCase("pt-BR");
  const visible = state.targets.filter(t => matchesStatus(t) && (!state.category || t.category === state.category) && (!q || `${t.title} ${t.host} ${t.category}`.toLocaleLowerCase("pt-BR").includes(q)));
  cards.innerHTML = visible.map(t => {
    const m = t.metrics;
    return `<article class="card ${escapeHtml(t.status)} ${t.alerts_enabled ? "alerts-on" : "alerts-off"}" data-id="${t.id}">
      <div class="card-head"><div><h2>${escapeHtml(t.title)}</h2><div class="host">${escapeHtml(t.host)}</div><span class="badge">${escapeHtml(t.category)}</span>${t.router ? `<span class="badge router">via ${escapeHtml(t.router)}</span>` : ""}</div>
      <div class="menu"><button class="menu-button" data-action="menu" aria-label="Opções">⋮</button><div class="menu-list hidden"><button data-action="edit">Editar</button><button data-action="duplicate">Copiar</button><button class="delete" data-action="delete">Excluir</button></div></div></div>
      <div class="metrics"><div class="metric"><span>Latência</span><b>${metric(m?.latency," ms")}</b></div><div class="metric"><span>Perda</span><b>${metric(m?.loss,"%")}</b></div><div class="metric"><span>Jitter</span><b>${metric(m?.jitter," ms")}</b></div></div>
      ${linkMetrics(m)}
      <div class="card-foot"><label class="alert-label"><input type="checkbox" role="switch" data-action="toggle" ${t.alerts_enabled ? "checked" : ""}><span>Alertas ${t.alerts_enabled ? "ativos" : "inativos"}</span></label><span class="status ${escapeHtml(t.status)}">● ${statusLabel(t.status)}</span></div>
    </article>`;
  }).join("");
  $("#emptyState").classList.toggle("hidden", visible.length > 0);
  $("#totalCount").textContent = state.targets.length;
  $("#healthyCount").textContent = state.targets.filter(t => t.status === "healthy").length;
  $("#alertCount").textContent = state.targets.filter(t => ["warning","critical","down"].includes(t.status)).length;
}
function updateCategories() {
  $("#categoryFilter").innerHTML = '<option value="">Todas</option>' + state.categories.map(c => `<option ${c===state.category?"selected":""}>${escapeHtml(c)}</option>`).join("");
  $("#categoryOptions").innerHTML = state.categories.map(c => `<option value="${escapeHtml(c)}"></option>`).join("");
}
async function loadTargets(silent=false) {
  if (!silent) cards.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  $("#refreshButton").classList.add("loading");
  try {
    const data = await api("/api/targets"); state.targets = data.targets; state.categories = data.categories; updateCategories(); render();
    $("#metricNotice").classList.toggle("hidden", !data.metric_error);
    $("#metricNotice").textContent = data.metric_error ? "Prometheus indisponível. Os destinos continuam editáveis, mas as métricas não puderam ser carregadas." : "";
  } catch (error) { cards.innerHTML=""; toast(error.message,true); }
  finally { $("#refreshButton").classList.remove("loading"); }
}
function renderRouterOptions(selected="") {
  $("#routerSelect").innerHTML = '<option value="">Local (prober)</option>' + state.routers.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)} (${escapeHtml(r.address)})</option>`).join("");
  $("#routerSelect").value = state.routers.some(r => r.name === selected) ? selected : "";
}
function renderRemoteFields() {
  const router = state.routers.find(r => r.name === $("#routerSelect").value);
  $("#remoteFields").classList.toggle("hidden", !router);
  $("#routerLinks").textContent = router ? router.links.map(l => l.name).join(", ") : "";
}
$("#routerSelect").addEventListener("change", () => {
  // Remote runs send a burst of pings per run, so 1s makes no sense there.
  const interval = form.elements.interval; const remote = Boolean($("#routerSelect").value);
  if (remote && interval.value === "1s") interval.value = "1m";
  if (!remote && interval.value === "1m") interval.value = "1s";
  renderRemoteFields();
});
function openForm(target=null, {duplicate=false}={}) {
  form.reset(); $("#targetId").value = duplicate ? "" : (target?.id || "");
  $("#dialogTitle").textContent = duplicate ? "Copiar destino" : (target ? "Editar destino" : "Novo destino");
  $("#duplicateButton").classList.toggle("hidden", duplicate || !target?.id);
  const values = target ? {...target, ...(duplicate ? {title:`${target.title} (cópia)`, smokeping_name:""} : {})} : {network:"auto",interval:"1s",size:56,tos:"0x00",alerts_enabled:true};
  renderRouterOptions(values.router || "");
  for (const [key,value] of Object.entries(values)) { if (key === "router") continue; const input=form.elements.namedItem(key); if (!input) continue; input.type === "checkbox" ? input.checked=Boolean(value) : input.value=value ?? ""; }
  renderRemoteFields();
  dialog.showModal(); setTimeout(() => form.elements.title.focus(), 50);
}
cards.addEventListener("click", async event => {
  const card=event.target.closest(".card"); if(!card) return; const target=state.targets.find(t=>t.id===card.dataset.id); const action=event.target.dataset.action;
  if(action==="menu") { card.querySelector(".menu-list").classList.toggle("hidden"); return; }
  if(action==="edit") { openForm(target); return; }
  if(action==="duplicate") { openForm(target, {duplicate:true}); return; }
  if(action==="delete") { state.deleteKind="target"; state.deleteId=target.id; $("#deleteTitle").textContent="Excluir destino?"; $("#deleteText").textContent=`${target.title} (${target.host}) será removido do config.yaml.`; $("#deleteDialog").showModal(); }
});
cards.addEventListener("change", async event => {
  if(event.target.dataset.action!=="toggle") return; const card=event.target.closest(".card"); const enabled=event.target.checked;
  try { const result=await api(`/api/targets/${card.dataset.id}/alerts`,{method:"PATCH",body:JSON.stringify({enabled})}); toast(enabled?"Alertas ativados":"Alertas desativados"); if(result.warning) toast(`Configuração salva, mas o reload falhou: ${result.warning}`,true); await loadTargets(true); }
  catch(error){event.target.checked=!enabled;toast(error.message,true);}
});
form.addEventListener("submit", async event => {
  event.preventDefault(); $("#saveSpinner").classList.remove("hidden");
  const data=Object.fromEntries(new FormData(form)); data.size=Number(data.size); data.alerts_enabled=form.elements.alerts_enabled.checked; data.protocol="icmp"; data.menu=data.title;
  data.count = data.router && data.count ? Number(data.count) : null;
  if (!data.router) { data.packet_interval = ""; data.timeout = ""; }
  const id=$("#targetId").value;
  try { const result=await api(id?`/api/targets/${id}`:"/api/targets",{method:id?"PUT":"POST",body:JSON.stringify(data)}); dialog.close(); toast(id?"Destino atualizado":"Destino adicionado"); if(result.warning) toast(`Salvo; reload pendente: ${result.warning}`,true); await loadTargets(true); }
  catch(error){toast(error.message,true);} finally{$("#saveSpinner").classList.add("hidden");}
});
$("#confirmDelete").addEventListener("click",async()=>{
  const isRouter = state.deleteKind === "router";
  const path = isRouter ? `/api/routers/${encodeURIComponent(state.deleteId)}` : `/api/targets/${state.deleteId}`;
  try{const result=await api(path,{method:"DELETE"});$("#deleteDialog").close();toast(isRouter?"Roteador excluído":"Destino excluído");if(result.warning)toast(`Excluído; reload pendente: ${result.warning}`,true);await (isRouter?loadRouters():loadTargets(true));}catch(error){toast(error.message,true);}
});
$("#addButton").addEventListener("click",()=>openForm()); $("#closeDialog").addEventListener("click",()=>dialog.close()); $("#cancelDialog").addEventListener("click",()=>dialog.close());
$("#duplicateButton").addEventListener("click",()=>{ const data=Object.fromEntries(new FormData(form)); data.alerts_enabled=form.elements.alerts_enabled.checked; openForm(data, {duplicate:true}); });
$("#cancelDelete").addEventListener("click",()=>$("#deleteDialog").close()); $("#refreshButton").addEventListener("click",()=>loadTargets(true));
$("#reloadButton").addEventListener("click",()=>$("#reloadDialog").showModal());
$("#cancelReload").addEventListener("click",()=>$("#reloadDialog").close());
$("#confirmReload").addEventListener("click",async()=>{try{await api("/api/reload",{method:"POST"});$("#reloadDialog").close();toast("SmokePing Prober recarregado");}catch(error){toast(error.message,true);}});
$("#searchInput").addEventListener("input",e=>{state.query=e.target.value;render();}); $("#categoryFilter").addEventListener("change",e=>{state.category=e.target.value;render();});
document.querySelectorAll(".summary-item").forEach(btn=>btn.addEventListener("click",()=>{
  const status=btn.dataset.status; state.status = state.status===status ? "" : status;
  document.querySelectorAll(".summary-item").forEach(b=>{const active=b.dataset.status===state.status;b.classList.toggle("active",active);b.setAttribute("aria-pressed",active);});
  render();
}));
document.addEventListener("click",e=>{if(!e.target.closest(".menu"))document.querySelectorAll(".menu-list").forEach(m=>m.classList.add("hidden"));});

// --- Roteadores ---------------------------------------------------------------

function renderRouters() {
  routerCards.innerHTML = state.routers.map(r => {
    const h = r.health; const degraded = h && (h.sessions_up < r.sessions || h.errors > 0);
    const health = h ? `<span><b>${h.sessions_up}/${r.sessions}</b> sessões SSH</span><span><b>${h.errors}</b> erros (${escapeHtml(document.body.dataset.window)})</span>` : `<span>Sem métricas do prober ainda</span>`;
    return `<article class="card router-card ${degraded ? "degraded" : ""}" data-name="${escapeHtml(r.name)}">
      <div class="card-head"><div><h2>${escapeHtml(r.name)}</h2><div class="host">${escapeHtml(r.username)}@${escapeHtml(r.address)}</div></div>
      <div class="menu"><button class="menu-button" data-action="menu" aria-label="Opções">⋮</button><div class="menu-list hidden"><button data-action="edit">Editar</button><button class="delete" data-action="delete">Excluir</button></div></div></div>
      <div class="router-meta">${health}<span><b>${r.targets}</b> destino(s)</span>${r.insecure_skip_host_key ? '<span class="warn">host key não verificada</span>' : ""}</div>
      <div class="router-links">${r.links.map(l => `<div class="router-link"><strong>${escapeHtml(l.name)}</strong>${l.vpn_instance ? `<code>vpn ${escapeHtml(l.vpn_instance)}</code>` : ""}${l.source ? `<code>${escapeHtml(l.source)}</code>` : ""}${l.source6 ? `<code>${escapeHtml(l.source6)}</code>` : ""}</div>`).join("")}</div>
    </article>`;
  }).join("");
  $("#routerEmpty").classList.toggle("hidden", state.routers.length > 0);
  $("#routerCount").textContent = state.routers.length || "";
}
async function loadRouters() {
  try { const data = await api("/api/routers"); state.routers = data.routers; renderRouters(); }
  catch (error) { toast(error.message, true); }
}
function linkRow(link={}) {
  const row = document.createElement("div"); row.className = "link-row";
  row.innerHTML = `<label>Nome<input data-field="name" required maxlength="63" pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]*" placeholder="isp-a"></label>
    <label>VPN instance<input data-field="vpn_instance" maxlength="31" placeholder="opcional"></label>
    <label>source (IPv4)<input data-field="source" placeholder="192.0.2.1"></label>
    <label>source6 (IPv6)<input data-field="source6" placeholder="opcional"></label>
    <button class="remove-link" type="button" aria-label="Remover link" title="Remover link">×</button>`;
  row.querySelectorAll("input").forEach(input => { input.value = link[input.dataset.field] || ""; });
  $("#linkRows").append(row);
}
function openRouterForm(router=null) {
  routerForm.reset(); $("#linkRows").innerHTML = "";
  $("#routerOriginal").value = router?.name || "";
  $("#routerDialogTitle").textContent = router ? "Editar roteador" : "Novo roteador";
  const r = router || {sessions:5, known_hosts:"/etc/smokeping_prober/known_hosts"};
  const els = routerForm.elements;
  for (const key of ["name","address","username","sessions","known_hosts"]) els[key].value = r[key] ?? "";
  els.auth.value = r.private_key_file ? "private_key_file" : "password_file";
  els.auth_path.value = r.private_key_file || r.password_file || "";
  els.insecure_skip_host_key.checked = Boolean(r.insecure_skip_host_key);
  (r.links?.length ? r.links : [{}]).forEach(linkRow);
  routerDialog.showModal(); setTimeout(() => els.name.focus(), 50);
}
routerCards.addEventListener("click", event => {
  const card = event.target.closest(".card"); if (!card) return;
  const router = state.routers.find(r => r.name === card.dataset.name); const action = event.target.dataset.action;
  if (action === "menu") { card.querySelector(".menu-list").classList.toggle("hidden"); return; }
  if (action === "edit") { openRouterForm(router); return; }
  if (action === "delete") {
    state.deleteKind = "router"; state.deleteId = router.name; $("#deleteTitle").textContent = "Excluir roteador?";
    $("#deleteText").textContent = router.targets ? `${router.name} ainda é usado por ${router.targets} destino(s). Mova ou exclua esses destinos antes.` : `${router.name} será removido do config.yaml.`;
    $("#deleteDialog").showModal();
  }
});
$("#linkRows").addEventListener("click", event => {
  if (!event.target.classList.contains("remove-link")) return;
  if ($("#linkRows").children.length === 1) { toast("O roteador precisa de pelo menos um link", true); return; }
  event.target.closest(".link-row").remove();
});
$("#addLinkButton").addEventListener("click", () => linkRow());
routerForm.addEventListener("submit", async event => {
  event.preventDefault(); $("#routerSpinner").classList.remove("hidden");
  const els = routerForm.elements; const original = $("#routerOriginal").value;
  const data = {
    name: els.name.value, address: els.address.value, username: els.username.value, sessions: Number(els.sessions.value),
    password_file: els.auth.value === "password_file" ? els.auth_path.value : "",
    private_key_file: els.auth.value === "private_key_file" ? els.auth_path.value : "",
    known_hosts: els.insecure_skip_host_key.checked ? "" : els.known_hosts.value,
    insecure_skip_host_key: els.insecure_skip_host_key.checked,
    links: [...$("#linkRows").children].map(row => Object.fromEntries([...row.querySelectorAll("input")].map(i => [i.dataset.field, i.value]))),
  };
  try {
    const result = await api(original ? `/api/routers/${encodeURIComponent(original)}` : "/api/routers", {method: original ? "PUT" : "POST", body: JSON.stringify(data)});
    routerDialog.close(); toast(original ? "Roteador atualizado" : "Roteador adicionado");
    if (result.warning) toast(`Salvo; reload pendente: ${result.warning}`, true);
    await Promise.all([loadRouters(), loadTargets(true)]);
  } catch (error) { toast(error.message, true); } finally { $("#routerSpinner").classList.add("hidden"); }
});
routerForm.elements.auth.addEventListener("change", e => { routerForm.elements.auth_path.placeholder = e.target.value === "password_file" ? "/etc/smokeping_prober/ne8k.pass" : "/etc/smokeping_prober/ne8k.key"; });
$("#addRouterButton").addEventListener("click", () => openRouterForm());
$("#closeRouterDialog").addEventListener("click", () => routerDialog.close()); $("#cancelRouterDialog").addEventListener("click", () => routerDialog.close());
function showView(view) {
  document.querySelectorAll(".tab").forEach(t => { const active = t.dataset.view === view; t.classList.toggle("active", active); t.setAttribute("aria-pressed", active); });
  $("#targetsView").classList.toggle("hidden", view !== "targets");
  $("#routersView").classList.toggle("hidden", view !== "routers");
}
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  showView(tab.dataset.view); history.replaceState(null, "", tab.dataset.view === "routers" ? "#roteadores" : location.pathname);
}));
if (location.hash === "#roteadores") showView("routers");

loadRouters(); loadTargets(); setInterval(()=>{loadTargets(true);loadRouters();},30000);

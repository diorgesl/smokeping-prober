const state = { targets: [], categories: [], query: "", category: "", status: "", deleteId: null };
const $ = (selector) => document.querySelector(selector);
const cards = $("#cards");
const dialog = $("#targetDialog");
const form = $("#targetForm");

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
  if (!response.ok) throw new Error(body.detail || `Erro HTTP ${response.status}`);
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
function render() {
  const q = state.query.toLocaleLowerCase("pt-BR");
  const visible = state.targets.filter(t => matchesStatus(t) && (!state.category || t.category === state.category) && (!q || `${t.title} ${t.host} ${t.category}`.toLocaleLowerCase("pt-BR").includes(q)));
  cards.innerHTML = visible.map(t => {
    const m = t.metrics;
    return `<article class="card ${escapeHtml(t.status)} ${t.alerts_enabled ? "alerts-on" : "alerts-off"}" data-id="${t.id}">
      <div class="card-head"><div><h2>${escapeHtml(t.title)}</h2><div class="host">${escapeHtml(t.host)}</div><span class="badge">${escapeHtml(t.category)}</span></div>
      <div class="menu"><button class="menu-button" data-action="menu" aria-label="Opções">⋮</button><div class="menu-list hidden"><button data-action="edit">Editar</button><button data-action="duplicate">Copiar</button><button class="delete" data-action="delete">Excluir</button></div></div></div>
      <div class="metrics"><div class="metric"><span>Latência</span><b>${metric(m?.latency," ms")}</b></div><div class="metric"><span>Perda</span><b>${metric(m?.loss,"%")}</b></div><div class="metric"><span>Jitter</span><b>${metric(m?.jitter," ms")}</b></div></div>
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
function openForm(target=null, {duplicate=false}={}) {
  form.reset(); $("#targetId").value = duplicate ? "" : (target?.id || "");
  $("#dialogTitle").textContent = duplicate ? "Copiar destino" : (target ? "Editar destino" : "Novo destino");
  $("#duplicateButton").classList.toggle("hidden", duplicate || !target?.id);
  const values = target ? {...target, ...(duplicate ? {title:`${target.title} (cópia)`, smokeping_name:""} : {})} : {network:"auto",interval:"1s",size:56,tos:"0x00",alerts_enabled:true};
  for (const [key,value] of Object.entries(values)) { const input=form.elements.namedItem(key); if (!input) continue; input.type === "checkbox" ? input.checked=Boolean(value) : input.value=value ?? ""; }
  dialog.showModal(); setTimeout(() => form.elements.title.focus(), 50);
}
cards.addEventListener("click", async event => {
  const card=event.target.closest(".card"); if(!card) return; const target=state.targets.find(t=>t.id===card.dataset.id); const action=event.target.dataset.action;
  if(action==="menu") { card.querySelector(".menu-list").classList.toggle("hidden"); return; }
  if(action==="edit") { openForm(target); return; }
  if(action==="duplicate") { openForm(target, {duplicate:true}); return; }
  if(action==="delete") { state.deleteId=target.id; $("#deleteText").textContent=`${target.title} (${target.host}) será removido do config.yaml.`; $("#deleteDialog").showModal(); }
});
cards.addEventListener("change", async event => {
  if(event.target.dataset.action!=="toggle") return; const card=event.target.closest(".card"); const enabled=event.target.checked;
  try { const result=await api(`/api/targets/${card.dataset.id}/alerts`,{method:"PATCH",body:JSON.stringify({enabled})}); toast(enabled?"Alertas ativados":"Alertas desativados"); if(result.warning) toast(`Configuração salva, mas o reload falhou: ${result.warning}`,true); await loadTargets(true); }
  catch(error){event.target.checked=!enabled;toast(error.message,true);}
});
form.addEventListener("submit", async event => {
  event.preventDefault(); $("#saveSpinner").classList.remove("hidden");
  const data=Object.fromEntries(new FormData(form)); data.size=Number(data.size); data.alerts_enabled=form.elements.alerts_enabled.checked; data.protocol="icmp"; data.menu=data.title;
  const id=$("#targetId").value;
  try { const result=await api(id?`/api/targets/${id}`:"/api/targets",{method:id?"PUT":"POST",body:JSON.stringify(data)}); dialog.close(); toast(id?"Destino atualizado":"Destino adicionado"); if(result.warning) toast(`Salvo; reload pendente: ${result.warning}`,true); await loadTargets(true); }
  catch(error){toast(error.message,true);} finally{$("#saveSpinner").classList.add("hidden");}
});
$("#confirmDelete").addEventListener("click",async()=>{try{const result=await api(`/api/targets/${state.deleteId}`,{method:"DELETE"});$("#deleteDialog").close();toast("Destino excluído");if(result.warning)toast(`Excluído; reload pendente: ${result.warning}`,true);await loadTargets(true);}catch(error){toast(error.message,true);}});
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
loadTargets(); setInterval(()=>loadTargets(true),30000);

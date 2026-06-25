const { invoke } = window.__TAURI__.core;
const GLYPH = { running:"🔵", review:"🟡", mr:"🟣", merged:"✅", idle:"⚪", done:"✅", failed:"❌", needs_input:"❓" };
let STATE = null, SEL = null;

const $ = (id) => document.getElementById(id);
function el(tag, cls, txt){ const e=document.createElement(tag); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e; }
function setStatus(t, err){ const s=$("statusbar"); s.textContent=t; s.style.color=err?"#f87171":""; }

async function load(){
  try{
    STATE = JSON.parse(await invoke("get_state"));
    renderTree();
    if (SEL) renderCenter(findTask(SEL));
    setStatus("обновлено " + new Date().toLocaleTimeString());
  }catch(e){ setStatus("ошибка движка: " + e, true); }
}
function findTask(sel){
  const p = STATE.projects[sel.p]; if(!p) return null;
  const g = p.groups.find(x=>x.key===sel.g); if(!g) return null;
  return g.tasks.find(t=>t.tid===sel.tid) || null;
}
function renderTree(){
  const tree = $("tree"); tree.innerHTML="";
  for (const [pn,p] of Object.entries(STATE.projects)){
    tree.appendChild(el("div","proj", pn + "  ·  " + p.repos.length + " repo"));
    for (const g of p.groups){
      const gh = el("div","group", g.loose ? "(без группы)" : (g.summary||g.key));
      if(!g.loose){ gh.appendChild(el("span","gk", g.key)); }
      tree.appendChild(gh);
      for (const t of g.tasks){
        const row = el("div","task");
        if (SEL && SEL.tid===t.tid) row.classList.add("sel");
        row.appendChild(el("span",null, GLYPH[t.status]||"•"));
        row.appendChild(document.createTextNode(t.title));
        row.onclick = ()=>{ SEL={p:pn, g:g.key, tid:t.tid}; renderTree(); renderCenter(t); };
        tree.appendChild(row);
      }
      for (const o of g.ops){
        const row = el("div","ops");
        row.appendChild(el("span",null, GLYPH[o.status]||"⚙"));
        row.appendChild(document.createTextNode("⚙ ops: " + o.kind));
        tree.appendChild(row);
      }
    }
  }
}
function renderCenter(t){
  const c=$("center"), f=$("facts");
  if(!t){ c.innerHTML='<div class="empty">задача не найдена</div>'; return; }
  c.innerHTML="";
  c.appendChild(el("h2", null, (GLYPH[t.status]||"") + " " + t.title));
  c.appendChild(el("div","meta", "статус: " + t.status + "   ·   ветка: " + t.branch + (t.merged?"   ·   влита":"")));
  if (t.needs_input){ const w=el("div","row2"); w.innerHTML='<span style="color:#fbbf24">❓ агент ждёт ответа:</span> '+t.needs_input; c.appendChild(w); }
  c.appendChild(el("div","meta","репозитории → target:"));
  for (const r of t.repos){
    const row=el("div","row2");
    row.innerHTML='<span class="k">'+r.repo+'</span> → '+r.base+(r.mr?'   <a href="'+r.mr+'" target="_blank">MR ↗</a>':'   <span class="k">— нет MR</span>');
    c.appendChild(row);
  }
  // facts pane
  f.innerHTML=""; f.appendChild(el("div","proj","ФАКТЫ"));
  const mrs = t.repos.filter(r=>r.mr);
  const head=el("div","row2"); head.textContent = "MR: "+mrs.length+"/"+t.repos.length+(t.merged?"   ·   ✅ влито":""); f.appendChild(head);
  for (const r of mrs){ const x=el("div","row2"); x.innerHTML='<a href="'+r.mr+'" target="_blank">'+r.repo+' MR ↗</a>'; f.appendChild(x); }
  f.appendChild(el("div","meta","[Test / Stage / Release — действия следующим шагом]"));
}
window.addEventListener("DOMContentLoaded", ()=>{ $("refresh").onclick=load; load(); setInterval(load, 5000); });

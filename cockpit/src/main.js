const { invoke } = window.__TAURI__.core;
const GLYPH = { running:"🔵", review:"🟡", mr:"🟣", merged:"✅", idle:"⚪", done:"✅", failed:"❌", needs_input:"❓" };
const COLOR = { running:"#60a5fa", review:"#fbbf24", mr:"#c084fc", merged:"#a78bfa", idle:"#6b7280", done:"#34d399", failed:"#f87171", needs_input:"#fbbf24" };
let STATE = null, SEL = null;
const collapsed = new Set();   // "proj:<n>" / "group:<proj>/<key>" — survives refresh

const $ = (id) => document.getElementById(id);
function el(t, cls, txt){ const e=document.createElement(t); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e; }
function setStatus(t, err){ const s=$("statusbar"); s.textContent=t; s.style.color=err?"#f87171":""; }
function dot(st){ const d=el("span","dot"); d.style.background=COLOR[st]||"#6b7280"; d.title=st; return d; }
async function openExt(target){ try{ await invoke("open_external",{target}); }catch(e){ setStatus("не открыл: "+e,true); } }

async function load(){
  try{
    STATE = JSON.parse(await invoke("get_state"));
    renderTree();
    if (SEL) renderCenter(findTask(SEL));
    setStatus("обновлено " + new Date().toLocaleTimeString());
  }catch(e){ setStatus("ошибка движка: "+e, true); }
}
function findTask(sel){ const p=STATE.projects[sel.p]; if(!p)return null; const g=p.groups.find(x=>x.key===sel.g); if(!g)return null; return g.tasks.find(t=>t.tid===sel.tid)||null; }

function renderTree(){
  const tree=$("tree"); tree.innerHTML="";
  for (const [pn,p] of Object.entries(STATE.projects)){
    const pk="proj:"+pn, pc=collapsed.has(pk);
    const ph=el("div","proj");
    ph.append(el("span","caret", pc?"▸":"▾"), document.createTextNode(pn), el("span","cnt", String(p.groups.length)));
    ph.onclick=()=>{ pc?collapsed.delete(pk):collapsed.add(pk); renderTree(); };
    tree.appendChild(ph);
    if (pc) continue;
    for (const g of p.groups){
      const gk="group:"+pn+"/"+g.key, gc=collapsed.has(gk), tot=g.tasks.length+g.ops.length;
      const gh=el("div","group");
      gh.append(el("span","caret", gc?"▸":"▾"),
                document.createTextNode(g.loose?"(без группы)":(g.summary||g.key)),
                el("span","cnt", String(tot)));
      gh.onclick=()=>{ gc?collapsed.delete(gk):collapsed.add(gk); renderTree(); };
      tree.appendChild(gh);
      if (gc) continue;
      for (const t of g.tasks){
        const row=el("div","task"+(SEL&&SEL.tid===t.tid?" sel":""));
        row.append(dot(t.status), document.createTextNode(t.title));
        row.onclick=()=>{ SEL={p:pn,g:g.key,tid:t.tid}; renderTree(); renderCenter(t); };
        tree.appendChild(row);
      }
      for (const o of g.ops){
        const row=el("div","ops"); row.append(dot(o.status), document.createTextNode("ops: "+o.kind)); tree.appendChild(row);
      }
    }
  }
}
function btn(label, fn){ const b=el("button","btn",label); b.onclick=fn; return b; }
function renderCenter(t){
  const c=$("center"), f=$("facts");
  if(!t){ c.innerHTML='<div class="empty">задача не найдена</div>'; return; }
  c.innerHTML="";
  const head=el("div","dhead"); head.append(dot(t.status), el("h2",null,t.title)); c.appendChild(head);
  c.appendChild(el("div","meta","ветка "+t.branch+"   ·   "+t.status+(t.merged?"   ·   влита":"")));
  const acts=el("div","acts");
  if (t.dir) acts.append(btn("Открыть папку → Claude Code / Codex", ()=>openExt(t.dir)));
  c.appendChild(acts);
  if (t.needs_input){ c.appendChild(el("div","ni","❓ агент ждёт ответа: "+t.needs_input)); }
  c.appendChild(el("div","sec","репозитории → target"));
  for (const r of t.repos){
    const row=el("div","row2"); row.append(el("span","k", r.repo+" → "+r.base));
    if (r.mr){ const a=el("a","lnk","MR ↗"); a.onclick=()=>openExt(r.mr); row.append(a); }
    else { row.append(el("span","dim"," — нет MR")); }
    c.appendChild(row);
  }
  f.innerHTML=""; f.appendChild(el("div","sec","ФАКТЫ"));
  const mrs=t.repos.filter(r=>r.mr);
  f.appendChild(el("div","row2","MR: "+mrs.length+"/"+t.repos.length+(t.merged?"   ✅ влито":"")));
  for (const r of mrs){ const x=el("div","row2"); const a=el("a","lnk", r.repo+" MR ↗"); a.onclick=()=>openExt(r.mr); x.append(a); f.appendChild(x); }
  f.appendChild(el("div","meta","Test / Stage / Release — следующим шагом"));
}
window.addEventListener("DOMContentLoaded", ()=>{ $("refresh").onclick=load; load(); setInterval(load, 5000); });

const { invoke } = window.__TAURI__.core;
const listen = window.__TAURI__.event.listen;
const GLYPH = { running:"🔵", review:"🟡", mr:"🟣", merged:"✅", idle:"⚪", done:"✅", failed:"❌", needs_input:"❓" };
const COLOR = { running:"#60a5fa", review:"#fbbf24", mr:"#c084fc", merged:"#a78bfa", idle:"#6b7280", done:"#34d399", failed:"#f87171", needs_input:"#fbbf24" };
let STATE = null, SEL = null;
const collapsed = new Set();
let chat = null;

const $ = (id) => document.getElementById(id);
function el(t, cls, txt){ const e=document.createElement(t); if(cls)e.className=cls; if(txt!=null)e.textContent=txt; return e; }
function setStatus(t, err){ const s=$("statusbar"); s.textContent=t; s.style.color=err?"#f87171":""; }
function dot(st){ const d=el("span","dot"); d.style.background=COLOR[st]||"#6b7280"; d.title=st; return d; }
function btn(label, fn, cls){ const b=el("button","btn"+(cls?" "+cls:""),label); b.onclick=fn; return b; }
async function openExt(target){ try{ await invoke("open_external",{target}); }catch(e){ setStatus("не открыл: "+e,true); } }
function curGroup(){ const p=STATE&&SEL&&STATE.projects[SEL.p]; return p? p.groups.find(g=>g.key===SEL.g) : null; }
function findTask(){ const g=curGroup(); return g? g.tasks.find(t=>t.tid===SEL.tid) : null; }

async function load(){
  try{
    STATE = JSON.parse(await invoke("get_state"));
    renderTree();
    const t = findTask(); if (t) renderFacts(t);   // live facts; chat terminal untouched
    setStatus("обновлено " + new Date().toLocaleTimeString());
  }catch(e){ setStatus("ошибка движка: "+e, true); }
}
function renderTree(){
  const tree=$("tree"); tree.innerHTML="";
  for (const [pn,p] of Object.entries(STATE.projects)){
    const pk="proj:"+pn, pc=collapsed.has(pk);
    const ph=el("div","proj"); ph.append(el("span","caret", pc?"▸":"▾"), document.createTextNode(pn), el("span","cnt", String(p.groups.length)));
    ph.onclick=()=>{ pc?collapsed.delete(pk):collapsed.add(pk); renderTree(); };
    tree.appendChild(ph);
    if (pc) continue;
    for (const g of p.groups){
      const gk="group:"+pn+"/"+g.key, gc=collapsed.has(gk), tot=g.tasks.length+g.ops.length;
      const gh=el("div","group"); gh.append(el("span","caret", gc?"▸":"▾"), document.createTextNode(g.loose?"(без группы)":(g.summary||g.key)), el("span","cnt", String(tot)));
      gh.onclick=()=>{ gc?collapsed.delete(gk):collapsed.add(gk); renderTree(); };
      tree.appendChild(gh);
      if (gc) continue;
      for (const t of g.tasks){
        const row=el("div","task"+(SEL&&SEL.tid===t.tid?" sel":""));
        row.append(dot(t.status), document.createTextNode(t.title));
        row.onclick=()=>{ SEL={p:pn,g:g.key,tid:t.tid}; renderTree(); openTask(t); };
        tree.appendChild(row);
      }
      for (const o of g.ops){ const row=el("div","ops"); row.append(dot(o.status), document.createTextNode("ops: "+o.kind)); tree.appendChild(row); }
    }
  }
}

function openTask(t){
  renderFacts(t);
  const c=$("center"); c.innerHTML="";
  const head=el("div","dhead"); head.append(dot(t.status), el("h2",null,t.title)); c.appendChild(head);
  c.appendChild(el("div","meta","ветка "+t.branch+"   ·   "+t.status));
  const ctr=el("div","chatbar");
  const sel=el("select","picker"); ["claude","codex"].forEach(b=>{ const o=el("option",null,b); o.value=b; sel.appendChild(o); });
  ctr.append(el("span","k","движок:"), sel, btn("▶ Запустить чат", ()=>startChat(t, sel.value)), btn("■ Стоп", stopChat, "ghost"));
  c.appendChild(ctr);
  const term=el("div"); term.id="term"; c.appendChild(term);
  if (chat) stopChat();
}

// ---- results modal + actions ----
function modal(title){
  const ov=el("div","overlay"); const box=el("div","modal");
  const hd=el("div","mhead"); hd.append(el("span",null,title), btn("✕", ()=>ov.remove(), "ghost"));
  const body=el("pre","mbody"); box.append(hd, body); ov.append(box); document.body.append(ov);
  return { ov, body };
}
async function runAction(args, label, prod){
  if (!confirm((prod?"⚠ ПРОД-bound — пойдёт в master!\n\n":"")+"Выполнить:\n"+label+" ?")) return;
  const m = modal("⏳ "+label+" …");
  try{ const out = await invoke("run_cc",{args}); m.body.textContent = (out||"(готово)").trim(); }
  catch(e){ m.body.textContent = "✗ ОШИБКА:\n"+e; m.body.style.color="#f87171"; }
  load();
}

function renderFacts(t){
  const f=$("facts"); f.innerHTML="";
  const g=curGroup(), loose = g && g.loose;
  f.appendChild(el("div","sec","ЗАДАЧА"));
  const trow=el("div","row2 acts");
  trow.append(btn("Создать MR", ()=>runAction(["task","mr",t.tid], "task mr "+t.tid, !loose?false:true)),
              btn("Влить", ()=>runAction(["task","merge",t.tid], "task merge "+t.tid, loose), "ghost"));
  if (t.dir) trow.append(btn("Папка", ()=>openExt(t.dir), "ghost"));
  f.appendChild(trow);
  f.appendChild(el("div","sec","репозитории → target"));
  for (const r of t.repos){ const row=el("div","row2"); row.append(el("span","k", r.repo+" → "+r.base)); if(r.mr){ const a=el("a","lnk"," MR ↗"); a.onclick=()=>openExt(r.mr); row.append(a); } f.appendChild(row); }
  const mrs=t.repos.filter(r=>r.mr);
  f.appendChild(el("div","row2 dim","MR: "+mrs.length+"/"+t.repos.length+(t.merged?"   ✅ влито":"")));
  // group ops
  f.appendChild(el("div","sec","ГРУППА "+(SEL?SEL.g:"")));
  const grow=el("div","row2 acts");
  grow.append(btn("Test", ()=>runAction(["epic","ops",SEL.g,"--kind","test"], "ops test "+SEL.g, false), "ghost"),
              btn("Stage", ()=>runAction(["epic","ops",SEL.g,"--kind","stage"], "ops stage "+SEL.g, false), "ghost"));
  f.appendChild(grow);
  if (!loose){
    const grow2=el("div","row2 acts");
    grow2.append(btn("Влить задачи", ()=>runAction(["epic","merge",SEL.g], "epic merge "+SEL.g, false), "ghost"),
                 btn("Release: MR→master", ()=>runAction(["epic","mr",SEL.g], "epic mr "+SEL.g, true), "warn"));
    f.appendChild(grow2);
  }
}

async function stopChat(){
  if (!chat) return;
  try{ await invoke("pty_kill",{id:chat.id}); }catch(e){}
  try{ chat.unlisten && chat.unlisten(); }catch(e){}
  try{ chat.term.dispose(); }catch(e){}
  chat=null;
}
async function startChat(t, backend){
  if (!t.dir){ setStatus("у задачи нет worktree-папки", true); return; }
  await stopChat();
  const host=$("term"); host.innerHTML="";
  const term=new window.Terminal({ fontSize:12.5, fontFamily:"Menlo, monospace", cursorBlink:true, theme:{ background:"#0e0e10", foreground:"#e6e6ea", cursor:"#7c8cff" } });
  const fit=new window.FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(host); fit.fit();
  const id="chat-"+Date.now();
  const unlisten=await listen("pty-output",(e)=>{ if(e.payload.id===id) term.write(new Uint8Array(e.payload.data)); });
  const unexit=await listen("pty-exit",(e)=>{ if(e.payload===id) term.write("\r\n[сессия завершена]\r\n"); });
  chat={ id, term, fit, unlisten:()=>{unlisten();unexit();} };
  try{
    await invoke("pty_spawn",{ id, cwd:t.dir, program:backend });
    await invoke("pty_resize",{ id, rows:term.rows, cols:term.cols });
    term.onData(d=>invoke("pty_write",{ id, data:Array.from(new TextEncoder().encode(d)) }));
    term.onResize(({cols,rows})=>invoke("pty_resize",{ id, rows, cols }));
    term.focus(); setStatus(backend+" запущен в "+t.dir);
  }catch(e){ setStatus("не запустил "+backend+": "+e, true); }
}
window.addEventListener("resize", ()=>{ if(chat){ chat.fit.fit(); } });
window.addEventListener("DOMContentLoaded", ()=>{ $("refresh").onclick=load; load(); setInterval(load, 5000); });

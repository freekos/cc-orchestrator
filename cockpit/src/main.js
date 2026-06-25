const { invoke } = window.__TAURI__.core;
const listen = window.__TAURI__.event.listen;
const GLYPH = { running:"🔵", review:"🟡", mr:"🟣", merged:"✅", idle:"⚪", done:"✅", failed:"❌", needs_input:"❓" };
const COLOR = { running:"#60a5fa", review:"#fbbf24", mr:"#c084fc", merged:"#a78bfa", idle:"#6b7280", done:"#34d399", failed:"#f87171", needs_input:"#fbbf24" };
// status = colour + SHAPE (WCAG 1.4.1: never colour alone). Distinct glyphs stay legible in greyscale.
const MARK = { needs_input:"!", failed:"✕", running:"◐", mr:"◆", review:"◑", merged:"✓", done:"✓", idle:"○" };
const STATUS_LABEL = { needs_input:"нужен ответ", failed:"упало", running:"работает", mr:"MR открыт",
                       review:"в ревью / закоммичено", merged:"влито", done:"готово", idle:"простаивает" };
const LEGEND = ["needs_input","failed","running","mr","review","merged","idle"];
function statusMark(st){ const m=el("span","mark m-"+st, MARK[st]||"•"); m.title=STATUS_LABEL[st]||st; return m; }
// loudest signal first: does this group hold anything that needs the human?
function groupAlert(g){
  const any=(s)=> g.tasks.some(t=>t.status===s)||g.ops.some(o=>o.status===s);
  return any("needs_input")?"needs_input":(any("failed")?"failed":null);
}
let STATE = null, SEL = null;
const collapsed = new Set();
let tabs = [], active = -1, seq = 0;   // middle = multiple tabs (chats/diffs)

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
    const t=findTask(); if(t){ renderFacts(t); renderLauncher(t); }
    setStatus("обновлено " + new Date().toLocaleTimeString());
  }catch(e){ setStatus("ошибка движка: "+e, true); }
}
function allKeys(){
  const ks=[];
  for (const [pn,p] of Object.entries(STATE.projects)){
    ks.push("proj:"+pn);
    for (const g of p.groups) ks.push("group:"+pn+"/"+g.key);
  }
  return ks;
}
const MORE_LIMIT = 8;            // long lists collapse to this with a "ещё N…" expander (Conductor)
const shownAll = new Set();
function folderIcon(open){
  const s=el("span","ficon");
  s.innerHTML = open
    ? '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h3.1l1.8 1.8H19a1.5 1.5 0 0 1 1.5 1.5"/><path d="M3.6 9.2h17.2l-1.5 8a1.2 1.2 0 0 1-1.2 1H5.3a1.2 1.2 0 0 1-1.2-1.4z"/></svg>'
    : '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h3.1l1.8 1.8H19a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/></svg>';
  return s;
}
function taskRow(t, pn, gkey){
  const row=el("div","task"+(SEL&&SEL.tid===t.tid?" sel":""));
  const lbl=el("span","label", t.title); lbl.title=t.title;
  row.append(statusMark(t.status), lbl);
  row.onclick=()=>{ SEL={p:pn,g:gkey,tid:t.tid}; renderTree(); renderFacts(t); renderLauncher(t); };
  return row;
}
function opsRow(o){
  const row=el("div","ops"); const lbl=el("span","label","ops: "+o.kind); lbl.title="ops: "+o.kind;
  row.append(statusMark(o.status), lbl); return row;
}
function projectAlert(p){   // loudest signal across all the project's groups (bubbled to the folder row)
  if (p.groups.some(g=>groupAlert(g)==="needs_input")) return "needs_input";
  if (p.groups.some(g=>groupAlert(g)==="failed")) return "failed";
  return null;
}
function renderTree(){
  const tree=$("tree"); tree.innerHTML="";
  // quiet toolbar (collapse / expand all) — like Conductor's section-header icons
  const tools=el("div","tree-tools");
  tools.append(el("span","tt-label","проекты"), el("span","gap"),
               btn("свернуть", ()=>{ allKeys().forEach(k=>collapsed.add(k)); renderTree(); }, "mini"),
               btn("развернуть", ()=>{ collapsed.clear(); renderTree(); }, "mini"));
  tree.appendChild(tools);
  for (const [pn,p] of Object.entries(STATE.projects)){
    const pk="proj:"+pn, pc=collapsed.has(pk);
    const loose=p.groups.find(g=>g.loose);
    const realGroups=p.groups.filter(g=>!g.loose);
    // task-based: standalone tasks aren't grouped — flat under the project, most-recently-touched first
    const looseTasks=loose ? loose.tasks.slice().sort((a,b)=>(b.activity||0)-(a.activity||0)) : [];
    const taskCount=p.groups.reduce((n,g)=>n+g.tasks.length,0);
    const ph=el("div","proj");
    ph.append(folderIcon(!pc), el("span","p-name", pn));
    const pa=projectAlert(p);
    if (pa) { const a=el("span","g-alert mark m-"+pa, MARK[pa]); a.title=STATUS_LABEL[pa]; ph.append(a); }
    ph.append(el("span","cnt", String(taskCount)));
    ph.onclick=()=>{ pc?collapsed.delete(pk):collapsed.add(pk); renderTree(); };
    tree.appendChild(ph);
    if (pc) continue;
    // standalone tasks at the top of the project (no "(без группы)" header)
    if (looseTasks.length || (loose && loose.ops.length)){
      const lk="loose:"+pn, all=shownAll.has(lk);
      const items=el("div","items loose");
      (all?looseTasks:looseTasks.slice(0,MORE_LIMIT)).forEach(t=>items.appendChild(taskRow(t,pn,loose.key)));
      if (!all && looseTasks.length>MORE_LIMIT){
        const more=el("div","more","ещё "+(looseTasks.length-MORE_LIMIT)+"…");
        more.onclick=()=>{ shownAll.add(lk); renderTree(); };
        items.appendChild(more);
      }
      (loose.ops||[]).forEach(o=>items.appendChild(opsRow(o)));
      tree.appendChild(items);
    }
    // real groups below the standalone tasks
    for (const g of realGroups){
      const gk="group:"+pn+"/"+g.key, gc=collapsed.has(gk);
      const gh=el("div","group"+(gc?" collapsed":""));
      gh.append(el("span","caret"), el("span","g-name", g.summary||g.key));
      const alert=groupAlert(g);
      if (alert) { const a=el("span","g-alert mark m-"+alert, MARK[alert]); a.title=STATUS_LABEL[alert]; gh.append(a); }
      gh.append(el("span","cnt", String(g.tasks.length+g.ops.length)));
      gh.onclick=()=>{ gc?collapsed.delete(gk):collapsed.add(gk); renderTree(); };
      tree.appendChild(gh);
      if (gc) continue;
      const items=el("div","items");
      const all=shownAll.has(gk);
      (all?g.tasks:g.tasks.slice(0,MORE_LIMIT)).forEach(t=>items.appendChild(taskRow(t,pn,g.key)));
      if (!all && g.tasks.length>MORE_LIMIT){
        const more=el("div","more","ещё "+(g.tasks.length-MORE_LIMIT)+"…");
        more.onclick=()=>{ shownAll.add(gk); renderTree(); };
        items.appendChild(more);
      }
      (g.ops||[]).forEach(o=>items.appendChild(opsRow(o)));
      tree.appendChild(items);
    }
  }
  // legend — subtle key, out of the per-row density
  const lg=el("div","legend");
  for (const st of LEGEND){ const it=el("span","lg-item"); it.append(el("span","mark m-"+st, MARK[st]), el("span",null,STATUS_LABEL[st])); lg.appendChild(it); }
  tree.appendChild(lg);
}

// ---- middle: launcher + tabbar + tabbody (persistent so terminals survive) ----
function renderLauncher(t){
  const l=$("launcher"); l.innerHTML="";
  if(!t){ l.append(el("span","dim","Выбери задачу слева")); return; }
  l.append(dot(t.status), el("b",null,t.title), el("span","dim","  "+t.branch+"  ·  "+t.status));
  const sel=el("select","picker"); ["claude","codex"].forEach(b=>{ const o=el("option",null,b); o.value=b; sel.appendChild(o); });
  l.append(el("span","gap"), el("span","k","движок:"), sel,
           btn("+ Чат", ()=>openChatTab(t, sel.value)), btn("+ Diff", ()=>openDiffTab(t), "ghost"));
}
function renderTabbar(){
  const bar=$("tabbar"); bar.innerHTML="";
  tabs.forEach((tb,i)=>{
    const chip=el("div","tab"+(i===active?" active":"")); chip.append(el("span",null,(tb.type==="diff"?"⟚ ":"")+tb.title));
    const x=el("span","x","✕"); x.onclick=(e)=>{ e.stopPropagation(); closeTab(i); }; chip.append(x);
    chip.onclick=()=>showTab(i); bar.appendChild(chip);
  });
}
function showTab(i){
  active=i;
  tabs.forEach((tb,j)=>{ tb.el.style.display = j===i ? "block":"none"; });
  renderTabbar();
  const tb=tabs[i]; if(tb && tb.type==="chat" && tb.fit){ try{tb.fit.fit(); tb.term.focus();}catch(e){} }
}
async function closeTab(i){
  const tb=tabs[i]; if(!tb) return;
  if(tb.type==="chat"){ try{await invoke("pty_kill",{id:tb.ptyId});}catch(e){} try{tb.unlisten&&tb.unlisten();}catch(e){} try{tb.term.dispose();}catch(e){} }
  tb.el.remove(); tabs.splice(i,1);
  active = tabs.length? Math.min(i, tabs.length-1) : -1;
  if(active>=0) showTab(active); else renderTabbar();
}
async function openChatTab(t, backend){
  if(!t.dir){ setStatus("у задачи нет worktree-папки", true); return; }
  const pane=el("div","tab-pane"); $("tabbody").append(pane);
  const term=new window.Terminal({ fontSize:12.5, fontFamily:"Menlo, monospace", cursorBlink:true, theme:{ background:"#0e0e10", foreground:"#e6e6ea", cursor:"#7c8cff" } });
  const fit=new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open(pane); fit.fit();
  const ptyId="chat-"+(++seq);
  const un=await listen("pty-output",(e)=>{ if(e.payload.id===ptyId) term.write(new Uint8Array(e.payload.data)); });
  const ux=await listen("pty-exit",(e)=>{ if(e.payload===ptyId) term.write("\r\n[сессия завершена]\r\n"); });
  const tab={ type:"chat", title:backend+": "+t.title.slice(0,14), el:pane, term, fit, ptyId, unlisten:()=>{un();ux();} };
  tabs.push(tab); showTab(tabs.length-1);
  try{
    await invoke("pty_spawn",{ id:ptyId, cwd:t.dir, program:backend });
    await invoke("pty_resize",{ id:ptyId, rows:term.rows, cols:term.cols });
    term.onData(d=>invoke("pty_write",{ id:ptyId, data:Array.from(new TextEncoder().encode(d)) }));
    term.onResize(({cols,rows})=>invoke("pty_resize",{ id:ptyId, rows, cols }));
    term.focus(); setStatus(backend+" запущен в "+t.dir);
  }catch(e){ setStatus("не запустил "+backend+": "+e, true); }
}
async function openDiffTab(t){
  const pane=el("div","tab-pane"); const pre=el("pre","diffpre","загрузка diff…"); pane.append(pre); $("tabbody").append(pane);
  const tab={ type:"diff", title:"diff: "+t.title.slice(0,14), el:pane };
  tabs.push(tab); showTab(tabs.length-1);
  try{ pre.textContent = (await invoke("run_cc",{args:["task","diff",t.tid]}) || "(пусто)").trim() || "(нет изменений)"; }
  catch(e){ pre.textContent="✗ "+e; pre.style.color="#f87171"; }
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
  const m=modal("⏳ "+label+" …");
  try{ m.body.textContent=(await invoke("run_cc",{args})||"(готово)").trim(); }
  catch(e){ m.body.textContent="✗ ОШИБКА:\n"+e; m.body.style.color="#f87171"; }
  load();
}
function renderFacts(t){
  const f=$("facts"); f.innerHTML="";
  const g=curGroup(), loose=g&&g.loose;
  f.appendChild(el("div","sec","ЗАДАЧА"));
  const trow=el("div","row2 acts");
  trow.append(btn("Создать MR", ()=>runAction(["task","mr",t.tid],"task mr "+t.tid, loose)),
              btn("Влить", ()=>runAction(["task","merge",t.tid],"task merge "+t.tid, loose), "ghost"));
  if(t.dir) trow.append(btn("Папка", ()=>openExt(t.dir), "ghost"));
  f.appendChild(trow);
  // combine toggle: pull this task's changes INTO the group's combined branch (or take them back out)
  if(!loose){ const crow=el("div","row2 acts");
    if(t.combined) crow.append(btn("⊖ Вынуть из группы", ()=>runAction(["group","combine",SEL.g,"--remove",t.tid],"вынуть "+t.tid+" из combined "+SEL.g,false), "ghost"));
    else crow.append(btn("⊕ Влить в группу", ()=>runAction(["group","combine",SEL.g,"--add",t.tid],"влить "+t.tid+" в combined "+SEL.g,false)));
    f.appendChild(crow); }
  f.appendChild(el("div","sec","репозитории → target"));
  for(const r of t.repos){ const row=el("div","row2"); row.append(el("span","k", r.repo+" → "+r.base)); if(r.mr){ const a=el("a","lnk"," MR ↗"); a.onclick=()=>openExt(r.mr); row.append(a); } f.appendChild(row); }
  const mrs=t.repos.filter(r=>r.mr);
  f.appendChild(el("div","row2 dim","MR: "+mrs.length+"/"+t.repos.length+(t.merged?"   ✅ влито":"")+(t.combined?"   ⊕ в combined":"")));
  f.appendChild(el("div","sec","ГРУППА "+(SEL?SEL.g:"")));
  // combined-branch state: which tasks are merged into the group's integration branch
  if(g && !loose){ const cn=(g.combined||[]).length;
    f.appendChild(el("div","row2 dim", cn? ("Объединено: "+cn+" задач → "+g.combined_branch) : "Объединено: пусто (combined-ветки нет)"));
    f.appendChild(btn("↻ Пересобрать combined", ()=>runAction(["group","combine",SEL.g],"пересобрать combined "+SEL.g,false), "ghost")); }
  const grow=el("div","row2 acts");
  grow.append(btn("Test", ()=>runAction(["group","ops",SEL.g,"--kind","test"],"ops test "+SEL.g,false), "ghost"),
              btn("Stage", ()=>runAction(["group","ops",SEL.g,"--kind","stage"],"ops stage "+SEL.g,false), "ghost"));
  f.appendChild(grow);
  if(!loose){ const g2=el("div","row2 acts");
    g2.append(btn("Влить задачи", ()=>runAction(["group","merge",SEL.g],"group merge "+SEL.g,false), "ghost"),
              btn("Release: MR→master", ()=>runAction(["group","mr",SEL.g],"group mr "+SEL.g,true), "warn"));
    f.appendChild(g2); }
}
function setupCenter(){
  const c=$("center"); c.innerHTML="";
  c.append(el("div","launcher")); $("center").lastChild.id="launcher";
  const bar=el("div","tabbar"); bar.id="tabbar"; c.append(bar);
  const body=el("div","tabbody"); body.id="tabbody"; c.append(body);
  renderLauncher(null);
}
window.addEventListener("resize", ()=>{ const tb=tabs[active]; if(tb&&tb.fit){ try{tb.fit.fit();}catch(e){} } });
window.addEventListener("DOMContentLoaded", ()=>{ setupCenter(); $("refresh").onclick=load; load(); setInterval(load, 5000); });

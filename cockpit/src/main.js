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
const seenKeys = new Set();   // projects/groups start COLLAPSED by default — seed each new key once
let tabs = [], active = null, seq = 0;   // middle = per-task chat/diff tabs; `active` is the shown tab object
let centerView = "home";                 // 'home'|'group'|'work'|'scopechat'|'ops' — so the 5s poll doesn't clobber a transient view
let engine = localStorage.getItem("cc_engine") || "claude";   // which agent "+ Чат" launches
const ENGINES = ["claude", "codex"];
const ENGINE_GLYPH = { claude: "✦", codex: "❯" };

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
    computeUpdates();   // diff vs last snapshot → "what's new" feed (before renderTree so tree dots show)
    renderTree();
    if(centerView==="ops" || centerView==="scopechat"){ /* transient full-screen view — don't clobber it on the poll */ }
    else { const t=findTask();
      if(t) renderFacts(t);                 // a task is open → refresh its facts (never disturb open chats)
      else if(SEL && SEL.g && SEL.tid===null && groupOf(SEL.p, SEL.g)){   // a group is selected → its dashboard
        renderGroupView(SEL.p, SEL.g); renderGroupFacts(SEL.p, SEL.g); centerMode("group"); }
      else { renderHome(); centerMode(true); }   // nothing selected → live triage overview (refreshes each tick)
    }
    setStatus("обновлено " + new Date().toLocaleTimeString());
  }catch(e){ setStatus("ошибка движка: "+e, true); }
}
// ---- "What's New": detect status changes across ALL tasks since the last snapshot ----
let lastSig=null;                  // tid -> status signature (null until baseline established)
const unseen=new Map();            // tid -> {title, pn, gkey, label, status, isNew} not yet acknowledged
function taskSig(t){ return t.status+"|"+(t.merged?1:0)+"|"+((t.repos||[]).filter(r=>r.mr).length)+"|"+(t.combined?1:0); }
function taskLabel(t){ return t.merged ? "влито ✓" : (STATUS_LABEL[t.status]||t.status); }
function computeUpdates(){
  const flat=activeTasksFlat(); const cur={};   // never badge archived tasks
  flat.forEach(x=>cur[x.t.tid]=taskSig(x.t));
  if(lastSig){
    for(const x of flat){ const tid=x.t.tid;
      const changed = lastSig[tid]!==undefined && lastSig[tid]!==cur[tid];
      const isNew   = lastSig[tid]===undefined;
      if((changed||isNew) && !(SEL && SEL.tid===tid))   // skip the task you're already looking at
        unseen.set(tid, {title:x.t.title, pn:x.pn, gkey:x.gkey, label:taskLabel(x.t), status:x.t.status, isNew});
    }
  }
  lastSig=cur;
  renderWhatsNew();
}
function renderWhatsNew(){
  const b=$("whatsnew"); if(!b) return;
  const n=unseen.size;
  if(!n){ b.style.display="none"; const p=$("wnpanel"); if(p) p.remove(); return; }
  b.style.display=""; b.textContent="● "+n+(n===1?" обновление":" обновл.");
  b.classList.toggle("alert", [...unseen.values()].some(u=>u.status==="needs_input"||u.status==="failed"));
  b.onclick=(e)=>{ e.stopPropagation(); toggleWhatsNew(b); };
}
// ---- network MR-state refresh: GitLab merge isn't local, so `task mrs` (network) must run to detect it ----
const mrInflight=new Set();
async function refreshTaskMr(tid, manual){
  if(mrInflight.has(tid)) return; mrInflight.add(tid);
  if(manual) setStatus("обновляю MR " + tid + " …");
  try{ await invoke("run_cc",{args:["task","mrs",tid]}); if(manual) await load(); }   // writes state.json → watcher/poll refresh
  catch(e){ if(manual) setStatus("✗ "+e, true); }
  finally{ mrInflight.delete(tid); }
}
// bounded background poll: refresh MR state for tasks with OPEN MRs, a couple per cycle (round-robin),
// so GitLab merges on ANY task surface — without hammering glab (≈2 calls / 90s).
let mrPollIdx=0;
function pollMrs(){
  if(!STATE) return;
  const cands=activeTasksFlat().filter(x=>!x.t.merged && (x.t.repos||[]).some(r=>r.mr)).map(x=>x.t.tid);
  if(!cands.length) return;
  for(let k=0;k<2 && k<cands.length;k++) refreshTaskMr(cands[(mrPollIdx+k)%cands.length]);
  mrPollIdx=(mrPollIdx+2)%cands.length;
}
function toggleWhatsNew(anchor){
  const old=$("wnpanel"); if(old){ old.remove(); return; }
  const p=el("div"); p.id="wnpanel";
  p.append(el("div","wn-head","Обновления статусов"));
  for(const [tid,u] of [...unseen.entries()].reverse()){
    const row=el("div","wn-row");
    row.append(statusMark(u.status), el("span","wn-proj", u.pn), el("span","wn-title", u.title), el("span","wn-label", u.isNew?"новая":u.label));
    row.onclick=()=>{ const x=allTasksFlat().find(z=>z.t.tid===tid); unseen.delete(tid); p.remove(); renderWhatsNew();
                      if(x) selectTask(x.t, x.pn, x.gkey); };
    p.appendChild(row);
  }
  const foot=el("div","wn-foot"); foot.append(btn("Отметить всё прочитанным", ()=>{ unseen.clear(); p.remove(); renderWhatsNew(); renderTree(); }, "ghost"));
  p.appendChild(foot);
  document.body.appendChild(p);
  const r=anchor.getBoundingClientRect();
  p.style.left=Math.max(8, Math.min(r.left, window.innerWidth-p.offsetWidth-8))+"px"; p.style.top=(r.bottom+6)+"px";
  const close=(ev)=>{ if(!p.contains(ev.target)&&ev.target!==anchor){ p.remove(); document.removeEventListener("mousedown",close); } };
  setTimeout(()=>document.addEventListener("mousedown",close),0);
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
  const row=el("div","task"+(SEL&&SEL.tid===t.tid?" sel":"")+(unseen.has(t.tid)?" has-update":""));
  const lbl=el("span","label", t.title);     // full title + context live in the hover-card now
  row.append(statusMark(t.status), lbl);
  const w=shortTime(t.activity); if(w){ const sp=el("span","when", w); sp.title="трогали "+relTime(t.activity); row.append(sp); }
  row.onclick=()=>selectTask(t, pn, gkey);
  row.onmouseenter=()=>showTaskCard(t, row); row.onmouseleave=hideCard;
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
// ---- hover-preview card (replaces the always-on legend): peek at an item without selecting it ----
let _card=null, _cardTimer=null;
function card(){ if(!_card){ _card=el("div"); _card.id="hovercard"; document.body.appendChild(_card); } return _card; }
function relTime(sec){
  if(!sec) return "—";
  const d=Math.max(0, Math.floor(Date.now()/1000)-sec);
  if(d<60) return "только что";
  if(d<3600) return Math.floor(d/60)+" мин назад";
  if(d<86400) return Math.floor(d/3600)+" ч назад";
  if(d<2592000) return Math.floor(d/86400)+" дн назад";
  return Math.floor(d/2592000)+" мес назад";
}
function shortTime(sec){    // compact relative time for the right-edge column ("5м"/"3ч"/"2д"/"4мес")
  if(!sec) return "";
  const d=Math.max(0, Math.floor(Date.now()/1000)-sec);
  if(d<3600) return Math.max(1,Math.floor(d/60))+"м";
  if(d<86400) return Math.floor(d/3600)+"ч";
  if(d<2592000) return Math.floor(d/86400)+"д";
  return Math.floor(d/2592000)+"мес";
}
function cardRow(k,v){ const r=el("div","hc-row"); r.append(el("span","hc-k",k), el("span","hc-v",v)); return r; }
function positionCard(anchor){
  const c=card(); c.style.display="block";
  const r=anchor.getBoundingClientRect(), w=c.offsetWidth, h=c.offsetHeight, gap=8;
  let left=r.right+gap; if(left+w>window.innerWidth-8) left=Math.max(8, r.left-w-gap);
  let top=Math.max(8, Math.min(r.top, window.innerHeight-8-h));
  c.style.left=left+"px"; c.style.top=top+"px";
}
function showTaskCard(t, anchor){
  clearTimeout(_cardTimer);
  _cardTimer=setTimeout(()=>{
    const c=card(); c.innerHTML="";
    c.append(el("div","hc-title", t.title));
    const st=el("div","hc-status"); st.append(el("span","mark m-"+t.status, MARK[t.status]), el("span",null,STATUS_LABEL[t.status]||t.status)); c.append(st);
    if(t.branch) c.append(cardRow("ветка", t.branch));
    const tot=(t.repos||[]).length, mrs=(t.repos||[]).filter(r=>r.mr).length;
    if(tot){ c.append(cardRow("репо → target", (t.repos||[]).map(r=>r.repo+" → "+r.base).join(", ")));
             c.append(cardRow("MR", mrs+"/"+tot+(t.merged?"  ✅":"")+(t.combined?"  ⊕ combined":""))); }
    c.append(cardRow("трогали", relTime(t.activity)));
    positionCard(anchor);
  }, 220);
}
function showGroupCard(g, anchor){
  clearTimeout(_cardTimer);
  _cardTimer=setTimeout(()=>{
    const c=card(); c.innerHTML="";
    c.append(el("div","hc-title", g.loose?"(без группы)":(g.summary||g.key)));
    c.append(cardRow("задач", String((g.tasks||[]).length)));
    const counts={}; (g.tasks||[]).forEach(t=>counts[t.status]=(counts[t.status]||0)+1);
    const br=el("div","hc-status");
    LEGEND.forEach(s=>{ if(counts[s]){ const sp=el("span","hc-cnt"); sp.append(el("span","mark m-"+s, MARK[s]), el("span",null,String(counts[s]))); br.append(sp); } });
    if(br.childNodes.length) c.append(br);
    const comb=(g.combined||[]).length;
    if(comb) c.append(cardRow("combined", comb+" → "+g.combined_branch));
    if((g.ops||[]).length) c.append(cardRow("ops", g.ops.map(o=>o.kind+" "+(MARK[o.status]||o.status)).join(", ")));
    positionCard(anchor);
  }, 220);
}
function hideCard(){ clearTimeout(_cardTimer); if(_card) _card.style.display="none"; }
function toggleKey(anchor){
  const old=document.getElementById("statuskey");
  if(old){ old.remove(); return; }
  const k=el("div"); k.id="statuskey";
  LEGEND.forEach(s=>{ const it=el("div","lg-item"); it.append(el("span","mark m-"+s, MARK[s]), el("span",null,STATUS_LABEL[s])); k.appendChild(it); });
  document.body.appendChild(k);
  const r=anchor.getBoundingClientRect();
  k.style.left=Math.max(8, Math.min(r.left, window.innerWidth-k.offsetWidth-8))+"px"; k.style.top=(r.bottom+6)+"px";
}
function renderTree(){
  const tree=$("tree"); tree.innerHTML="";
  for(const k of allKeys()){ if(!seenKeys.has(k)){ seenKeys.add(k); collapsed.add(k); } }   // default: collapsed
  // quiet toolbar (collapse / expand all) — like Conductor's section-header icons
  const tools=el("div","tree-tools");
  tools.append(el("span","tt-label","проекты"), el("span","gap"),
               btn("поиск", ()=>openSearch(), "mini"),
               btn("свернуть", ()=>{ allKeys().forEach(k=>collapsed.add(k)); renderTree(); }, "mini"),
               btn("развернуть", ()=>{ collapsed.clear(); renderTree(); }, "mini"),
               btn("?", (e)=>toggleKey(e.currentTarget), "mini key"));
  tree.appendChild(tools);
  for (const [pn,p] of Object.entries(STATE.projects)){
    const pk="proj:"+pn, pc=collapsed.has(pk);
    const loose=p.groups.find(g=>g.loose);
    const groupAct=g=>Math.max(0, ...(g.tasks||[]).map(t=>t.activity||0));   // group recency = its newest task
    const realGroups=p.groups.filter(g=>!g.loose && !g.archived).sort((a,b)=>groupAct(b)-groupAct(a));   // archived groups → search only
    // task-based: standalone tasks aren't grouped — flat under the project, most-recently-touched first
    const looseTasks=loose ? loose.tasks.filter(t=>!t.archived).slice().sort((a,b)=>(b.activity||0)-(a.activity||0)) : [];
    const taskCount=p.groups.reduce((n,g)=> n + (g.archived?0:(g.tasks||[]).filter(t=>!t.archived).length), 0);
    const ph=el("div","proj");
    ph.append(folderIcon(!pc), el("span","p-name", pn));
    const pa=projectAlert(p);
    if (pa) { const a=el("span","g-alert mark m-"+pa, MARK[pa]); a.title=STATUS_LABEL[pa]; ph.append(a); }
    const pchat=el("span","p-chat","💬"); pchat.title="Чат про проект (общая картина)";
    pchat.onclick=(e)=>{ e.stopPropagation(); openProjectChat(pn); };
    ph.append(pchat, el("span","cnt", String(taskCount)));
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
      const gtasks=(g.tasks||[]).filter(t=>!t.archived);   // archived tasks → search only
      const gk="group:"+pn+"/"+g.key, gc=collapsed.has(gk);
      const gsel = SEL && SEL.p===pn && SEL.g===g.key && !SEL.tid;
      const gh=el("div","group"+(gc?" collapsed":"")+(gsel?" gsel":""));
      const car=el("span","caret"); car.onclick=(e)=>{ e.stopPropagation(); gc?collapsed.delete(gk):collapsed.add(gk); renderTree(); };
      gh.append(car, el("span","g-name", g.summary||g.key));
      const alert=groupAlert(g);
      if (alert) { const a=el("span","g-alert mark m-"+alert, MARK[alert]); a.title=STATUS_LABEL[alert]; gh.append(a); }
      gh.append(el("span","cnt", String(gtasks.length+g.ops.length)));
      gh.onclick=()=>selectGroup(pn, g.key);   // open the feature dashboard (caret toggles collapse)
      gh.onmouseenter=()=>showGroupCard(g, gh); gh.onmouseleave=hideCard;
      tree.appendChild(gh);
      if (gc) continue;
      const items=el("div","items");
      const all=shownAll.has(gk);
      (all?gtasks:gtasks.slice(0,MORE_LIMIT)).forEach(t=>items.appendChild(taskRow(t,pn,g.key)));
      if (!all && gtasks.length>MORE_LIMIT){
        const more=el("div","more","ещё "+(gtasks.length-MORE_LIMIT)+"…");
        more.onclick=()=>{ shownAll.add(gk); renderTree(); };
        items.appendChild(more);
      }
      (g.ops||[]).forEach(o=>items.appendChild(opsRow(o)));
      tree.appendChild(items);
    }
  }
}

// ---- middle: browser-style chat tabs, per task. No launcher — a default chat opens on select. ----
const lastTab = {};                 // tid -> last shown tab (restore the right chat when you return to a task)
const opening = new Set();          // guard so the periodic refresh can't double-open the default chat
function taskTabs(tid){ return tabs.filter(t=>t.taskId===tid); }
function selectTask(t, pn, gkey){
  SEL={p:pn,g:gkey,tid:t.tid};
  if(unseen.delete(t.tid)) renderWhatsNew();   // opening a task acknowledges its update
  collapsed.delete("proj:"+pn); collapsed.delete("group:"+pn+"/"+gkey);   // reveal the selected task's path
  centerMode(false);
  renderTree(); renderFacts(t); showTaskChats(t);
  if(!t.merged && (t.repos||[]).some(r=>r.mr)) refreshTaskMr(t.tid);   // freshen the focused task's MR state
}
function showTaskChats(t){          // bring this task's tabs to the front; open a default chat if it has none
  if(!t){ renderTabbar(); return; }
  const mine=taskTabs(t.tid);
  if(!mine.length){ ensureChat(t); return; }
  showTab((lastTab[t.tid] && mine.includes(lastTab[t.tid])) ? lastTab[t.tid] : mine[0]);
}
const CHAT_RESTORE_LIMIT=8;   // restore the most recent N chat sessions as tabs; older stay behind "⟲"
async function ensureChat(t){
  if(taskTabs(t.tid).length || opening.has(t.tid)) return;
  opening.add(t.tid);
  try{
    let chats=[];
    try{ chats=JSON.parse((await invoke("run_cc",{args:["task","sessions",t.tid,"--json"]}))||"[]").filter(x=>x.kind==="chat"); }catch(_){}
    if(taskTabs(t.tid).length) return;            // a "+" opened one while we were fetching
    if(!chats.length){ await openChatTab(t, engine); return; }   // no past chats → one fresh chat
    // restore ALL the task's real chat sessions as tabs (newest ends up active). oldest-first so the
    // last one opened — the newest — becomes the active tab.
    const restore=chats.slice(0, CHAT_RESTORE_LIMIT).reverse();
    for(const s of restore){
      if(taskTabs(t.tid).some(x=>x.session===s.sid)) continue;
      let hist=[]; try{ hist=JSON.parse((await invoke("run_cc",{args:["task","history",s.sid,"--json"]}))||"[]"); }catch(_){}
      await openChatTab(t, "claude", {sid:s.sid, dir:s.dir, history:hist, preview:s.preview});
    }
  } finally { opening.delete(t.tid); }
}
function renderTabbar(){
  const bar=$("tabbar"); bar.innerHTML="";
  const t=findTask();
  if(!t){ bar.style.display="none"; return; }
  bar.style.display="flex";
  for(const tb of taskTabs(t.tid)){
    const chip=el("div","tab"+(tb===active?" active":""));
    const ic=el("span","tab-ic", tb.type==="diff" ? "⟚" : tb.type==="term" ? "▌" : (ENGINE_GLYPH[tb.engine]||"✦"));
    if(tb.type==="chat") ic.classList.add("e-"+tb.engine);
    chip.append(ic, el("span","tab-title", tb.title));
    const x=el("span","x","✕"); x.title="Закрыть"; x.onclick=(e)=>{ e.stopPropagation(); closeTab(tb); }; chip.append(x);
    chip.onclick=()=>showTab(tb); chip.title=tb.title; bar.appendChild(chip);
  }
  const add=el("div","tab tab-add","+"); add.title="Новый чат";   // browser-style "+" on the right
  add.onclick=()=>{ const tt=findTask(); if(tt) openChatTab(tt, engine); };
  bar.appendChild(add);
  const hist=el("div","tab tab-hist","⟲"); hist.title="Прошлые чаты задачи (cc TUI)";
  hist.onclick=()=>{ const tt=findTask(); if(tt) openSessionPicker(tt, hist); };
  bar.appendChild(hist);
}
function showTab(tb){
  if(!tb) return;
  active=tb; lastTab[tb.taskId]=tb;
  tabs.forEach(x=>{ x.el.style.display = x===tb ? ((x.type==="chat"||x.type==="term")?"flex":"block") : "none"; });
  renderTabbar(); updateChatContext();   // refresh "в этом чате" for the now-active chat
  if(tb.refit) setTimeout(()=>tb.refit(),30);   // terminal needs a re-fit after being shown
  if(tb.focus) try{ tb.focus(); }catch(e){}
}
function closeTab(tb){
  const i=tabs.indexOf(tb); if(i<0) return;
  try{ tb.unlisten&&tb.unlisten(); }catch(e){}
  try{ tb.onClose&&tb.onClose(); }catch(e){}   // term: kill the PTY + dispose xterm
  tb.el.remove(); tabs.splice(i,1);
  if(lastTab[tb.taskId]===tb) delete lastTab[tb.taskId];
  if(active!==tb){ renderTabbar(); return; }
  const mine=taskTabs(tb.taskId);
  if(mine.length) showTab(mine[Math.min(i, mine.length-1)] || mine[0]);
  else { active=null; renderTabbar(); }     // last chat closed: leave just the "+" (don't force-reopen)
}
// ---- minimal, CSP-safe markdown -> HTML (headings, code, lists, bold/italic, inline code) ----
function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function mdRender(src){
  const blocks=[];
  src=(src||"").replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{ blocks.push('<pre class="md-pre"><code>'+esc(code.replace(/\n$/,""))+"</code></pre>"); return " "+(blocks.length-1)+" "; });
  const inline=(s)=> esc(s).replace(/`([^`]+)`/g,'<code class="md-ic">$1</code>').replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g,"$1<i>$2</i>");
  let html="", list=null; const flush=()=>{ if(list){ html+="</"+list+">"; list=null; } };
  for(const ln of (src.split("\n"))){
    const ph=ln.match(/^ (\d+) $/); if(ph){ flush(); html+=blocks[+ph[1]]; continue; }
    let m;
    if(m=ln.match(/^(#{1,4})\s+(.*)/)){ flush(); const h=Math.min(4,m[1].length); html+="<h"+h+' class="md-h">'+inline(m[2])+"</h"+h+">"; }
    else if(m=ln.match(/^\s*[-*+]\s+(.*)/)){ if(list!=="ul"){ flush(); list="ul"; html+="<ul>"; } html+="<li>"+inline(m[1])+"</li>"; }
    else if(m=ln.match(/^\s*\d+\.\s+(.*)/)){ if(list!=="ol"){ flush(); list="ol"; html+="<ol>"; } html+="<li>"+inline(m[1])+"</li>"; }
    else if(ln.trim()===""){ flush(); }
    else { flush(); html+="<p>"+inline(ln)+"</p>"; }
  }
  flush(); return html;
}
// parse one stream line (Claude stream-json OR Codex jsonl) -> mutate the tab's last assistant msg
function handleChatEvent(tab, line){
  let ev; try{ ev=JSON.parse(line); }catch(_){ return; }
  const a=tab.msgs[tab.msgs.length-1];
  if(ev.type==="system" && ev.subtype==="init" && ev.session_id){ tab.session=ev.session_id; tab.sessionEngine=tab.engine; return; }
  if(ev.type==="thread.started" && ev.thread_id){ tab.session=ev.thread_id; tab.sessionEngine=tab.engine; return; }   // codex
  if(ev.type==="stream_event" && ev.event){
    const e2=ev.event;
    if(e2.type==="content_block_delta" && e2.delta && e2.delta.type==="text_delta"){ if(a&&a.role==="assistant"){ a.text+=e2.delta.text; tab.render(); } return; }
    if(e2.type==="content_block_start" && e2.content_block && e2.content_block.type==="tool_use"){ tab.msgs.splice(tab.msgs.length-1,0,{role:"tool",text:e2.content_block.name||"tool"}); tab.render(); return; }
    return;
  }
  if(ev.type==="assistant" && ev.message){   // whole assistant message (stream-json always emits this)
    const txt=(ev.message.content||[]).filter(b=>b&&b.type==="text").map(b=>b.text).join("");
    if(txt && a && a.role==="assistant" && !a.text){ a.text=txt; tab.render(); }   // fallback: only if deltas didn't stream (no double)
    return;
  }
  if(ev.type==="result" && ev.is_error && a && a.role==="assistant" && !a.text){      // surface API/turn errors
    a.text="✗ движок вернул ошибку"+(ev.subtype?" ("+ev.subtype+")":""); tab.render(); return;
  }
  if(ev.type==="item.completed" && ev.item){                                            // codex
    if(ev.item.type==="agent_message" && a && a.role==="assistant"){ a.text=ev.item.text||a.text; tab.render(); }
    else if(ev.item.type==="command_execution"||ev.item.type==="file_change"){ tab.msgs.splice(tab.msgs.length-1,0,{role:"tool",text:ev.item.type.replace("_"," ")}); tab.render(); }
    return;
  }
}
async function chatTurn(tab, prompt, display){
  if(tab.busy) return;
  tab.busy=true;
  if(display!==false) tab.msgs.push({role:"user", text: typeof display==="string"?display:prompt});
  tab.msgs.push({role:"assistant", text:"", busy:true});
  tab.render();
  const a=tab.msgs[tab.msgs.length-1];
  try{
    const cmd=tab.session ? "chat_followup" : "chat_spawn";
    tab.sessionEngine=tab.engine;                 // remember which engine owns the running session
    await invoke(cmd, { id:tab.id, cwd:tab.dir, engine:tab.engine, prompt, session: tab.session||"", dirs: tab.dirs||[] });
  }catch(e){ a.text="✗ не запустил движок: "+e; a.busy=false; tab.busy=false; tab.render(); }
}
// a user message: on the FIRST turn inject the task memory; after an engine switch, hand over the transcript
async function sendChat(tab, text){
  let ctx="";
  if(!tab.injectedMem && !tab.session){
    if(tab.firstContext){                         // scoped chat (feature/project): inject its context, no task-memory
      ctx += tab.firstContext + "\n\n";
    } else {
      let mem=""; try{ mem=((await invoke("run_cc",{args:["task","memory",tab.taskId]}))||"").trim(); }catch(_){}
      const hasMem = mem && /\S/.test(mem.replace(/^#.*$/gm,"").replace(/^##.*$/gm,"").trim());
      if(hasMem) ctx+="Память задачи (общий контекст всех чатов):\n"+mem+"\n\n";
    }
    tab.injectedMem=true;
  }
  if(tab.carry){                                  // engine just changed → the new session has no native context
    const dig=tab.msgs.filter(m=>m.role!=="tool"&&m.text).slice(-8).map(m=>(m.role==="user"?"Я: ":"Агент: ")+m.text).join("\n\n");
    if(dig) ctx+="Контекст прошлого разговора (был другой движок), продолжаем здесь:\n"+dig+"\n\n";
    tab.carry=false;
  }
  if(!tab.toldMemory && !tab.firstContext){   // task chats only: teach the agent to self-record into task memory
    ctx+="[cc] У задачи есть общая память для всех её чатов и репозиториев. Когда примешь важное решение, найдёшь ключевой факт или сменишь направление — добавь В КОНЦЕ ответа ОТДЕЛЬНОЙ строкой одно из: `cc-memory log: <кратко>` (событие/находка), `cc-memory current: <кратко>` (текущее направление), `cc-memory pivot: <кратко>` (разворот). Помечай так ТОЛЬКО эти строки, не обычный текст.\n\n";
    tab.toldMemory=true;
  }
  await chatTurn(tab, ctx+text, text);
}
// the cockpit harvests `cc-memory <kind>: ...` lines the agent emits and writes them via cc (the agent's
// shell can't reach the `cc` alias, so the WRITE happens here through the cockpit's working run_cc)
async function harvestMemory(tab){
  const a=tab.msgs[tab.msgs.length-1]; if(!a || a.role!=="assistant" || !a.text) return;
  const re=/^\s*cc-memory\s+(log|current|pivot)\s*:\s*(.+)$/gim;
  const hits=[]; let m;
  while((m=re.exec(a.text))) hits.push([m[1].toLowerCase(), m[2].trim()]);
  if(!hits.length) return;
  a.text=a.text.replace(re,"").replace(/\n{3,}/g,"\n\n").trim();   // hide the markers from the rendered chat
  tab.render();
  for(const [kind,val] of hits){ try{ await invoke("run_cc",{args:["task","memory",tab.taskId,"--"+kind,val]}); }catch(_){} }
  load();   // refresh facts (has_memory / memory contents)
}
// engine picker that lives in the composer (bottom), Cursor-style — opens upward
function engineMenu(anchor, tab, sync){
  const old=$("enginemenu"); if(old){ old.remove(); return; }
  const m=el("div"); m.id="enginemenu";
  ENGINES.forEach(e=>{
    const it=el("div","em-item"+(e===tab.engine?" on":""));
    it.append(el("span","e-glyph e-"+e, ENGINE_GLYPH[e]||"✦"), el("span",null, e==="codex"?"Codex":"Claude"));
    it.onclick=()=>{
      m.remove();
      if(e===tab.engine) return;
      if(tab.session && tab.sessionEngine && tab.sessionEngine!==e){ tab.session=null; tab.carry=true; }  // new chat + carry context
      tab.engine=e; localStorage.setItem("cc_engine", e); engine=e;
      sync(); renderTabbar();
    };
    m.appendChild(it);
  });
  document.body.appendChild(m);
  const r=anchor.getBoundingClientRect();
  m.style.left=Math.max(8, Math.min(r.left, window.innerWidth-m.offsetWidth-8))+"px";
  m.style.top=(r.top-m.offsetHeight-6)+"px";
  const close=(ev)=>{ if(!m.contains(ev.target)&&ev.target!==anchor){ m.remove(); document.removeEventListener("mousedown",close); } };
  setTimeout(()=>document.addEventListener("mousedown",close),0);
}
// past chat sessions of the task (recovered from claude's store via `cc task sessions`) — click to resume
async function openSessionPicker(t, anchor){
  const old=$("sessmenu"); if(old){ old.remove(); return; }
  const m=el("div"); m.id="sessmenu"; m.appendChild(el("div","sm-head","Прошлые чаты задачи"));
  let items=[];
  try{ items=JSON.parse((await invoke("run_cc",{args:["task","sessions",t.tid,"--json"]}))||"[]"); }catch(e){}
  if(!items.length) m.appendChild(el("div","sm-empty","нет прошлых сессий"));
  for(const sn of items){
    const it=el("div","sm-item"+(sn.kind==="service"?" service":""));
    const d=new Date(sn.mtime*1000), ts=("0"+d.getDate()).slice(-2)+"."+("0"+(d.getMonth()+1)).slice(-2);
    it.append(el("span","sm-meta", ts+" · "+sn.repo+" · "+sn.turns+"t"+(sn.kind==="service"?" · служебн.":"")),
              el("span","sm-prev", (sn.preview||"").replace(/\s+/g," ").slice(0,72)));
    it.onclick=async()=>{ m.remove();
      let hist=[]; try{ hist=JSON.parse((await invoke("run_cc",{args:["task","history",sn.sid,"--json"]}))||"[]"); }catch(_){}
      openChatTab(t, "claude", {sid:sn.sid, dir:sn.dir, history:hist, preview:sn.preview});
    };
    m.appendChild(it);
  }
  document.body.appendChild(m);
  const r=anchor.getBoundingClientRect();
  m.style.left=Math.max(8, Math.min(r.right-m.offsetWidth, window.innerWidth-m.offsetWidth-8))+"px";
  m.style.top=(r.bottom+6)+"px";
  const close=(ev)=>{ if(!m.contains(ev.target)&&ev.target!==anchor){ m.remove(); document.removeEventListener("mousedown",close); } };
  setTimeout(()=>document.addEventListener("mousedown",close),0);
}
async function openChatTab(t, eng, opts){
  opts = opts || {};
  if(opts.sid){ const ex=taskTabs(t.tid).find(x=>x.session===opts.sid); if(ex){ showTab(ex); return; } }  // one tab per resumed session
  if(!t.dir && !opts.dir){ setStatus("у задачи нет worktree-папки", true); return; }
  const resumed = !!opts.sid;
  const id="chat-"+(++seq);
  const num=taskTabs(t.tid).filter(x=>x.type==="chat").length + 1;   // short browser-tab name: "Чат 1", "Чат 2"…
  const pane=el("div","tab-pane chat");
  const list=el("div","chat-msgs");
  const composer=el("div","chat-composer");
  const inp=el("textarea","chat-inp"); inp.placeholder="Сообщение агенту…  (Enter — отправить, Shift+Enter — перенос)"; inp.rows=1;
  const bar=el("div","composer-bar");
  const esel=el("button","engine-sel");                              // model picker lives here, bottom of the chat
  const send=btn("▶", ()=>tab.send(), "send");
  bar.append(el("span","cb-gap"), esel, send);
  composer.append(inp, bar);
  pane.append(list, composer); $("tabbody").append(pane);
  const tab={ type:"chat",
              engine: resumed ? "claude" : (eng||engine),   // old cc TUI sessions are claude
              title: resumed ? ("↩ "+((opts.preview||"чат").split("\n")[0].slice(0,20))) : ("Чат "+num),
              el:pane, id, taskId:t.tid, dir: opts.dir || t.dir, taskDir: t.dir,
              // resumed chat is pinned to its session's repo cwd → let it also reach the task's other repos
              dirs: (resumed && t.dir && opts.dir && t.dir!==opts.dir) ? [t.dir] : [],
              msgs: resumed ? (opts.history||[]).map(m=>({role:m.role, text:m.text})) : [],
              session: opts.sid || null, sessionEngine: resumed ? "claude" : null,
              busy:false, injectedMem: resumed, toldMemory:false, carry:false };
  const syncEsel=()=>{ esel.innerHTML=""; esel.append(
      el("span","e-glyph e-"+tab.engine, ENGINE_GLYPH[tab.engine]||"✦"),
      el("span","es-name", tab.engine==="codex"?"Codex":"Claude"), el("span","caret-dn","⌄")); };
  esel.onclick=()=>engineMenu(esel, tab, ()=>{ syncEsel(); }); syncEsel();
  tab.render=()=>{ list.innerHTML=""; for(const m of tab.msgs){
      const d=el("div","msg "+m.role);
      if(m.role==="assistant"){ d.innerHTML = m.text ? mdRender(m.text) : (m.busy?'<span class="typing">…</span>':""); }
      else if(m.role==="tool"){ d.textContent="▸ "+m.text; }
      else { d.textContent=m.text; }
      list.appendChild(d);
    }
    if(!tab.msgs.length){ list.appendChild(el("div","chat-hint","Пустой чат. Напиши сообщение — агент подхватит память задачи и начнёт. Прошлые чаты задачи — кнопка ⟲ сверху.")); }
    list.scrollTop=list.scrollHeight; };
  tab.focus=()=>{ try{ inp.focus(); }catch(e){} };
  tab.send=async()=>{ const v=inp.value.trim(); if(!v||tab.busy) return; inp.value=""; inp.style.height="auto"; await sendChat(tab, v); };
  inp.onkeydown=(e)=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); tab.send(); } };
  inp.oninput=()=>{ inp.style.height="auto"; inp.style.height=Math.min(160, inp.scrollHeight)+"px"; };
  const un=await listen("chat-event",(e)=>{ if(e.payload && e.payload.id===id) handleChatEvent(tab, e.payload.line); });
  const ud=await listen("chat-done",(e)=>{ if(e.payload && e.payload.id===id){
    const last=tab.msgs[tab.msgs.length-1]; if(last&&last.busy) last.busy=false; tab.busy=false;
    if(last && last.role==="assistant" && !last.text){   // nothing came back → show why (exit code + stderr) instead of an empty bubble
      last.text = e.payload.code===0 ? "_(пустой ответ движка)_"
        : "✗ движок вышел с кодом "+e.payload.code+(e.payload.err?":\n```\n"+e.payload.err.trim()+"\n```":"");
    }
    tab.render(); harvestMemory(tab); updateChatContext(); } });
  tab.unlisten=()=>{un();ud();};
  tabs.push(tab); tab.render(); showTab(tab);     // chat opens ready; the agent only runs when you send
}
// ---- unified-diff parser + renderer (file blocks, line numbers, +/- coloring, collapsible) ----
function parseDiff(text){
  const repos=[]; const parts=(text||"").split(/^===== CCDIFF (.+?) =====$/m);
  if(parts.length>1){ for(let i=1;i<parts.length;i+=2) repos.push({repo:parts[i].trim(), files:parseDiffFiles(parts[i+1]||"")}); }
  else repos.push({repo:"", files:parseDiffFiles(text)});
  return repos;
}
function parseDiffFiles(body){
  const files=[]; let cur=null, hunk=null, o=0, n=0;
  for(const line of (body||"").split("\n")){
    if(line.startsWith("diff --git")){ cur={path:"", add:0, del:0, hunks:[], note:""}; files.push(cur); hunk=null;
      const m=line.match(/ b\/(.+)$/); if(m) cur.path=m[1]; continue; }
    if(!cur){ continue; }
    if(line.startsWith("+++ ")){ const m=line.match(/^\+\+\+ b\/(.+)/); if(m) cur.path=m[1]; continue; }
    if(line.startsWith("--- ")) continue;
    if(line.startsWith("new file")){ cur.note="new"; continue; }
    if(line.startsWith("deleted file")){ cur.note="deleted"; continue; }
    if(line.startsWith("rename ")||line.startsWith("similarity ")||line.startsWith("index ")||line.startsWith("old mode")||line.startsWith("new mode")) continue;
    if(line.startsWith("Binary files")){ cur.note="binary"; continue; }
    const hm=line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)/);
    if(hm){ o=+hm[1]; n=+hm[2]; hunk={header:line, lines:[]}; cur.hunks.push(hunk); continue; }
    if(!hunk) continue;
    if(line.startsWith("\\")) continue;            // "\ No newline at end of file"
    if(line.startsWith("+")){ cur.add++; hunk.lines.push({t:"add", o:"", n:n++, text:line.slice(1)}); }
    else if(line.startsWith("-")){ cur.del++; hunk.lines.push({t:"del", o:o++, n:"", text:line.slice(1)}); }
    else { hunk.lines.push({t:"ctx", o:o++, n:n++, text:line.slice(1)}); }
  }
  return files;
}
function renderDiff(box, text){
  box.innerHTML="";
  const repos=parseDiff(text);
  const anyFiles=repos.some(r=>r.files.length);
  if(!anyFiles){ box.append(el("div","diff-empty", ((text||"").replace(/^===== CCDIFF.*$/gm,"").trim())||"(нет изменений)")); return; }
  for(const repo of repos){
    if(repo.repo) box.append(el("div","diff-repo", repo.repo));
    if(!repo.files.length){ box.append(el("div","diff-empty","(нет изменений)")); continue; }
    for(const f of repo.files){
      const fb=el("div","diff-file");
      const head=el("div","diff-fhead");
      head.append(el("span","diff-caret","▾"), el("span","diff-path", f.path||"?"));
      if(f.note) head.append(el("span","diff-note", f.note));
      head.append(el("span","diff-stat", "+"+f.add+" −"+f.del));
      head.onclick=()=>fb.classList.toggle("collapsed");
      fb.append(head);
      const bodyEl=el("div","diff-body");
      for(const h of f.hunks){
        bodyEl.append(el("div","diff-hunk", h.header));
        for(const ln of h.lines){
          const row=el("div","diff-line "+ln.t);
          row.append(el("span","ln", ln.o===""?"":String(ln.o)), el("span","ln", ln.n===""?"":String(ln.n)));
          const code=el("span","diff-code"); code.textContent=(ln.t==="add"?"+":ln.t==="del"?"−":" ")+ln.text;
          row.append(code); bodyEl.append(row);
        }
      }
      if(!f.hunks.length) bodyEl.append(el("div","diff-hunk", f.note==="binary"?"(бинарный файл)":"(без изменений содержимого)"));
      fb.append(bodyEl); box.append(fb);
    }
  }
}
async function openDiffTab(t){
  const existing=taskTabs(t.tid).find(x=>x.type==="diff");   // one diff tab per task — focus + refresh, never duplicate
  if(existing){ showTab(existing); existing.reload&&existing.reload(); return; }
  const id="diff-"+(++seq);
  const pane=el("div","tab-pane diff"); const wrap=el("div","diff-wrap"); pane.append(wrap); $("tabbody").append(pane);
  const tab={ type:"diff", title:"diff", el:pane, id, taskId:t.tid, unlisten:null };
  tab.reload=async()=>{ wrap.innerHTML=""; wrap.append(el("div","diff-empty","загрузка diff…"));
    try{ renderDiff(wrap, (await invoke("run_cc",{args:["task","diff",t.tid]})) || ""); }
    catch(e){ wrap.innerHTML=""; wrap.append(el("div","diff-empty","✗ "+e)); } };
  tabs.push(tab); showTab(tab); tab.reload();
}
// ---- terminal tab: an interactive shell in the task folder (PTY + xterm) ----
// shared terminal core: a PTY shell + xterm in `cwd`, with a "run all" that pastes a dev oneliner.
// opts: {termKey (dedup), taskId (which tab strip it lives on), cwd, title, devArgs, hint, runLabel}
async function spawnTerminal(opts){
  if(typeof Terminal==="undefined"){ setStatus("xterm не загружен", true); return; }
  const existing=tabs.find(x=>x.type==="term" && x.termKey===opts.termKey);
  if(existing){ gotoTab(existing); return; }
  const id="term-"+(++seq);
  const pane=el("div","tab-pane term");
  const bar=el("div","term-bar"); const host=el("div","term-host");
  pane.append(bar, host); $("tabbody").append(pane);
  const tab={ type:"term", title:opts.title, el:pane, id, taskId:opts.taskId, termKey:opts.termKey,
              devArgs:opts.devArgs, dir:opts.cwd, term:null, fit:null, unlisten:null };
  const runBtn=btn(opts.runLabel||"▶ Запустить все репо", ()=>termRunAll(tab), "send"); runBtn.title=opts.hint||"";
  bar.append(runBtn, el("span","term-hint", opts.hint||""));
  const term=new Terminal({ fontFamily:"Menlo, monospace", fontSize:12, cursorBlink:true, theme:{ background:"#0e0e10", foreground:"#e6e6ea" } });
  const fit=new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(host); try{ fit.fit(); }catch(e){}
  tab.term=term; tab.fit=fit;
  tab.refit=()=>{ try{ fit.fit(); invoke("pty_resize",{id, rows:term.rows, cols:term.cols}); }catch(e){} };
  term.onData(d=> invoke("pty_write",{ id, data:Array.from(new TextEncoder().encode(d)) }));
  const uo=await listen("pty-output", e=>{ if(e.payload && e.payload.id===id) term.write(new Uint8Array(e.payload.data)); });
  const ux=await listen("pty-exit", e=>{ if(e.payload===id) term.write("\r\n\x1b[90m[процесс завершён]\x1b[0m\r\n"); });
  tab.unlisten=()=>{ try{uo();}catch(e){} try{ux();}catch(e){} };
  tab.onClose=async()=>{ try{ await invoke("pty_kill",{id}); }catch(e){} try{ term.dispose(); }catch(e){} };
  try{ await invoke("pty_spawn",{ id, cwd:opts.cwd, program:"" }); }
  catch(e){ term.write("✗ не запустил shell: "+e+"\r\n"); }
  tabs.push(tab); showTab(tab); setTimeout(()=>tab.refit&&tab.refit(),60);
}
function gotoTab(tab){   // bring a tab to front even if it lives on another task's strip
  if(tab.taskId){ const x=allTasksFlat().find(z=>z.t.tid===tab.taskId); if(x){ selectTask(x.t, x.pn, x.gkey); } }
  showTab(tab);
}
function openTermTab(t){   // per-task terminal: shell in the task folder (repos are subfolders, on the task branch)
  if(!t.dir){ setStatus("у задачи нет worktree-папки", true); return; }
  spawnTerminal({ termKey:"t:"+t.tid, taskId:t.tid, cwd:t.dir, title:"терминал", devArgs:["task","dev",t.tid],
                  hint:"shell в "+t.dir.split("/").pop()+" · репозитории — подпапки, уже на ветке задачи" });
}
async function openGroupTerm(gkey, t){   // group terminal: shell in the COMBINED worktree (all tasks merged)
  let d=null; try{ d=JSON.parse((await invoke("run_cc",{args:["group","dev",gkey,"--json"]}))||"{}"); }catch(e){ setStatus("✗ "+e, true); return; }
  if(!d || !d.ok){ setStatus(d&&d.reason ? d.reason : "общая ветка группы не собрана", true); return; }
  spawnTerminal({ termKey:"g:"+gkey, taskId:t.tid, cwd:d.dir, title:"combined: "+gkey, devArgs:["group","dev",gkey],
                  runLabel:"▶ Запустить combined", hint:"combined-ветка "+d.branch+" · все задачи группы вместе" });
}
async function termRunAll(tab){
  try{ const d=JSON.parse((await invoke("run_cc",{args:[...tab.devArgs,"--json"]}))||"{}");
    if(d.oneliner) await invoke("pty_write",{ id:tab.id, data:Array.from(new TextEncoder().encode(d.oneliner+"\n")) });
    else setStatus(d.reason || "нет dev-команд для запуска", true);
  }catch(e){ setStatus("✗ "+e, true); }
}

// ---- results modal + actions ----
function modal(title){
  const ov=el("div","overlay"); const box=el("div","modal");
  const hd=el("div","mhead"); hd.append(el("span",null,title), btn("✕", ()=>ov.remove(), "ghost"));
  const body=el("pre","mbody"); box.append(hd, body); ov.append(box); document.body.append(ov);
  return { ov, body };
}
// read-only view — memory is GENERATED from the chat (the agent emits cc-memory markers, the cockpit
// harvests them), not hand-edited. See harvestMemory.
async function openMemory(t){
  const ov=el("div","overlay"); const box=el("div","modal");
  const hd=el("div","mhead"); hd.append(el("span",null,"🧠 Память задачи · "+t.title), btn("✕",()=>ov.remove(),"ghost"));
  const body=el("pre","mbody");
  const foot=el("div","mem-foot"); foot.append(el("span","dim","Память пополняется из чата автоматически — здесь только просмотр."));
  box.append(hd, body, foot); ov.append(box); document.body.append(ov);
  body.textContent="загрузка…";
  try{ body.textContent=((await invoke("run_cc",{args:["task","memory",t.tid]}))||"").trim()||"(пусто — ещё ничего не зафиксировано из чата)"; }
  catch(e){ body.textContent="✗ "+e; }
}
async function openInCursor(dir){ try{ await invoke("open_editor",{path:dir}); }catch(e){ setStatus("не открыл Cursor: "+e, true); } }
async function openGroupInCursor(gkey){   // open the combined worktree (all group tasks merged) in Cursor
  try{ const d=JSON.parse((await invoke("run_cc",{args:["group","dev",gkey,"--json"]}))||"{}");
    if(d && d.ok) openInCursor(d.dir); else setStatus(d&&d.reason ? d.reason : "combined не собран", true);
  }catch(e){ setStatus("✗ "+e, true); }
}
// MR confirmation modal: show exactly WHERE each repo's MR goes + a Draft toggle before creating
function openMrModal(t, loose){
  const ov=el("div","overlay"); const box=el("div","modal form");
  const hd=el("div","mhead"); hd.append(el("span",null,"Создать MR · "+t.title), btn("✕",()=>ov.remove(),"ghost"));
  const body=el("div","nt-body");
  body.append(el("div","mr-cap", loose ? "⚠ Задача без эпика — MR пойдёт в master/main." : "MR по каждому изменённому репозиторию:"));
  const list=el("div","mr-targets");
  const repos=(t.repos||[]);
  repos.forEach(r=>{ const row=el("div","mr-trow");
    row.append(el("span","mr-repo", r.repo), el("span","mr-arrow","→"), el("span","mr-branch", r.base||"?"));
    if(r.mr) row.append(el("span","mr-has","уже есть MR ↗"));
    list.append(row); });
  if(!repos.length) list.append(el("div","dim","нет репозиториев"));
  body.append(list);
  const draftRow=el("label","mr-draft");
  const cb=document.createElement("input"); cb.type="checkbox"; cb.className="mr-cb";
  draftRow.append(cb, el("span",null,"Создать как Draft (WIP) — не готов к ревью"));
  body.append(draftRow);
  const foot=el("div","mem-foot"); const status=el("span","nt-status","");
  const create=async()=>{
    status.textContent="создаю MR…"; mk.disabled=true;
    const args=["task","mr",t.tid]; if(cb.checked) args.push("--draft");
    try{ const out=(await invoke("run_cc",{args}))||""; ov.remove(); const m=modal("MR — результат"); m.body.textContent=out.trim()||"(готово)"; load(); }
    catch(e){ status.textContent=""; mk.disabled=false; body.append(el("pre","nt-err","✗ "+e)); }
  };
  const mk=btn("Создать MR", create, loose?"warn":"primary");
  foot.append(status, mk);
  box.append(hd, body, foot); ov.append(box); document.body.append(ov);
}
// mark a task done → soft archive; it leaves the board, so drop back to the home overview
async function doneTask(t){
  if(!confirm("Готово — убрать «"+t.title+"» в архив?\n\nJira→Done, worktrees снимаются. Ветку/MR/чаты не трогаем — задачу можно вернуть через поиск (↩ Вернуть).")) return;
  const m=modal("⏳ убираю в архив …");
  try{ m.body.textContent=(await invoke("run_cc",{args:["task","done",t.tid]})||"(готово)").trim(); }
  catch(e){ m.body.textContent="✗ "+e; m.body.style.color="#f87171"; return; }
  goHome(); load();
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
  const cc=el("div"); cc.id="chatctx"; f.appendChild(cc); updateChatContext();   // "в этом чате" — skills/files/tools of the active chat
  const tip=(b,s)=>{ b.title=s; return b; };
  if(t.archived){   // archived task: only the way back
    f.appendChild(el("div","sec","ЗАДАЧА · В АРХИВЕ"+(t.archived_at?" · "+t.archived_at:"")));
    f.appendChild(el("div","row2 dim","Убрана с доски. Ветка/MR/чаты целы."));
    const arow=el("div","row2 acts");
    arow.append(tip(btn("↩ Вернуть из архива", ()=>runAction(["task","restore",t.tid],"вернуть "+t.tid+" из архива",false), "primary"), "Ре-материализовать worktrees из ветки и вернуть на доску"));
    f.appendChild(arow);
    return;   // no MR/diff/done actions for an archived task
  }
  f.appendChild(el("div","sec","ЗАДАЧА"));
  const trow=el("div","row2 acts");
  trow.append(
    tip(btn("Создать MR", ()=>openMrModal(t, loose), "primary"), "Создать MR по изменённым репозиториям (с подтверждением и выбором Draft)"),
    tip(btn("Diff", ()=>openDiffTab(t), "ghost"), "Изменения задачи (git diff) по всем репозиториям"));
  if(t.dir) trow.append(tip(btn("Открыть в Cursor", ()=>openInCursor(t.dir), "ghost"), "Открыть папку задачи в Cursor"));
  if(t.dir) trow.append(tip(btn("Терминал", ()=>openTermTab(t), "ghost"), "Терминал в папке задачи — запускай репозитории на их ветках, смотри логи"));
  trow.append(tip(btn("✓ Готово", ()=>doneTask(t), "ghost"), "Готово → убрать в архив (Jira→Done, worktrees снимаются; ветка/MR/чаты целы, вернуть можно через поиск)"));
  // memory folded into the action row: it's read-only and auto-filled from the chats, so it's just a view
  const memBtn=tip(btn("🧠 Память", ()=>openMemory(t), "ghost"),
                   "Память задачи: решения, направление, находки — копятся из чатов автоматически. Только просмотр.");
  if(!t.has_memory) memBtn.classList.add("muted");
  trow.append(memBtn);
  f.appendChild(trow);
  // combine toggle: pull this task's changes INTO the group's combined branch (or take them back out)
  // repos → target — task-level info, always visible
  f.appendChild(el("div","sec","репозитории → ветка-цель"));
  for(const r of t.repos){ const row=el("div","row2"); row.append(el("span","k", r.repo+" → "+r.base)); if(r.mr){ const a=el("a","lnk"," MR ↗"); a.onclick=()=>openExt(r.mr); row.append(a); } f.appendChild(row); }
  const mrs=t.repos.filter(r=>r.mr);
  const mrRow=el("div","row2 dim");
  mrRow.append(el("span",null,"MR: "+mrs.length+"/"+t.repos.length+(t.merged?"   ✅ влито":"")+(t.combined?"   ⊕ в общей ветке":"")));
  if(mrs.length && !t.merged){ const rb=btn("↻", ()=>refreshTaskMr(t.tid, true), "mini"); rb.title="Обновить статус MR из GitLab"; mrRow.append(rb); }
  f.appendChild(mrRow);
  // group + release actions — advanced, collapsed by default (rarely needed day-to-day, some hit prod)
  const gopen = localStorage.getItem("cc_group_open")==="1";
  const ghead=el("div","sec sec-toggle"); ghead.append(el("span","sec-caret", gopen?"▾":"▸"), el("span",null,"ГРУППА · РЕЛИЗ"+(SEL&&SEL.g?" — "+SEL.g:"")));
  ghead.title="Combine, тесты, stage, мёрж всех задач, релиз — продвинутые операции уровня группы/эпика";
  ghead.onclick=()=>{ localStorage.setItem("cc_group_open", gopen?"0":"1"); renderFacts(t); };
  f.appendChild(ghead);
  if(gopen){
    if(!loose){ const crow=el("div","row2 acts");
      if(t.combined) crow.append(tip(btn("⊖ Вынуть из группы", ()=>runAction(["group","combine",SEL.g,"--remove",t.tid],"вынуть "+t.tid+" из combined "+SEL.g,false), "ghost"), "Убрать изменения задачи из общей ветки группы"));
      else crow.append(tip(btn("⊕ Влить в группу", ()=>runAction(["group","combine",SEL.g,"--add",t.tid],"влить "+t.tid+" в combined "+SEL.g,false), "ghost"), "Добавить изменения задачи в общую ветку группы — чтобы тестировать несколько задач вместе"));
      f.appendChild(crow); }
    if(g && !loose){ const cn=(g.combined||[]).length;
      f.appendChild(el("div","row2 dim", cn? ("Объединено: "+cn+" задач → "+g.combined_branch) : "Объединено: пусто (общей ветки нет)"));
      f.appendChild(tip(btn("↻ Пересобрать общую ветку", ()=>runAction(["group","combine",SEL.g],"пересобрать combined "+SEL.g,false), "ghost"), "Заново собрать общую (combined) ветку группы из влитых задач"));
      if(cn){ const drow=el("div","row2 acts");   // local test of the WHOLE group = run the combined branch
        drow.append(tip(btn("▶ Запустить combined", ()=>openGroupTerm(SEL.g, t), "ghost"), "Терминал в combined-ветке — поднять dev-серверы всех задач группы вместе, смотреть логи"),
                    tip(btn("Combined в Cursor", ()=>openGroupInCursor(SEL.g), "ghost"), "Открыть combined-worktree (все задачи группы слиты) в Cursor"));
        f.appendChild(drow); } }
    const grow=el("div","row2 acts");
    grow.append(tip(btn("Test", ()=>runAction(["group","ops",SEL.g,"--kind","test"],"ops test "+SEL.g,false), "ghost"), "Прогнать тесты по группе"),
                tip(btn("Stage", ()=>runAction(["group","ops",SEL.g,"--kind","stage"],"ops stage "+SEL.g,false), "ghost"), "Задеплоить группу на stage"));
    f.appendChild(grow);
    if(!loose){ const g2=el("div","row2 acts");
      g2.append(tip(btn("Влить все задачи", ()=>runAction(["group","merge",SEL.g],"group merge "+SEL.g,false), "ghost"), "Слить MR всех задач группы в их целевые ветки"),
                tip(btn("Релиз: MR в master", ()=>runAction(["group","mr",SEL.g],"group mr "+SEL.g,true), "warn"), "Создать релизный MR эпика в master (прод!)"));
      f.appendChild(g2); }
  }
}
// "В ЭТОМ ЧАТЕ" (Claude-Cowork-style): skills + context files + tools used by the ACTIVE chat's session
function shortTool(n){ return (n||"").replace(/^mcp__/,"").replace(/__/g,":"); }
async function updateChatContext(){
  const box=$("chatctx"); if(!box) return;
  const tab=active;
  if(!tab || tab.type!=="chat" || !tab.session){ box.innerHTML=""; return; }
  let ctx=null; try{ ctx=JSON.parse((await invoke("run_cc",{args:["task","context",tab.session,"--json"]}))||"{}"); }catch(_){ return; }
  if($("chatctx")!==box) return;                              // facts re-rendered while we awaited
  const skills=ctx.skills||[], files=ctx.files||[], tools=ctx.tools||{};
  if(!skills.length && !files.length && !Object.keys(tools).length){ box.innerHTML=""; return; }
  box.innerHTML="";
  box.appendChild(el("div","sec","В ЭТОМ ЧАТЕ"));
  if(skills.length){ box.append(el("div","cc-lab","скиллы"));
    const row=el("div","cc-chips"); skills.forEach(s=>row.append(el("span","cc-skill", s))); box.append(row); }
  const top=Object.entries(tools).sort((a,b)=>b[1]-a[1]).slice(0,8);
  if(top.length){ box.append(el("div","cc-lab","инструменты"));
    const row=el("div","cc-chips"); top.forEach(([n,c])=>row.append(el("span","cc-tool", shortTool(n)+" ×"+c))); box.append(row); }
  if(files.length){ box.append(el("div","cc-lab","контекст — файлы ("+files.length+")"));
    const list=el("div","cc-files"); const all=ccCtxExpanded.has(tab.id);
    (all?files:files.slice(0,12)).forEach(fp=>{ const it=el("div","cc-file"); it.textContent=fp.split("/").pop(); it.title=fp; it.onclick=()=>openExt(fp); list.append(it); });
    box.append(list);
    if(!all && files.length>12){ const more=el("div","cc-more","ещё "+(files.length-12)+"…"); more.onclick=()=>{ ccCtxExpanded.add(tab.id); updateChatContext(); }; box.append(more); }
  }
}
const ccCtxExpanded=new Set();
function setupCenter(){
  const c=$("center"); c.innerHTML="";
  const home=el("div"); home.id="home"; c.append(home);            // triage home (no selection)
  const gv=el("div"); gv.id="groupview"; gv.style.display="none"; c.append(gv);   // group dashboard (group selected)
  const sc=el("div"); sc.id="scopechat"; sc.style.display="none"; c.append(sc);   // feature/project "ask" chat
  const op=el("div"); op.id="opsview"; op.style.display="none"; c.append(op);     // global ops console (deploys + history)
  const bar=el("div","tabbar"); bar.id="tabbar"; bar.style.display="none"; c.append(bar);
  const body=el("div","tabbody"); body.id="tabbody"; c.append(body);
  const f=$("facts"); f.innerHTML=""; f.append(el("div","empty","выбери задачу — здесь появятся MR, ветки и действия"));
}
// center has three modes: 'home' (triage) · 'group' (feature dashboard) · 'work' (task chat/diff/term)
function centerMode(mode){
  if(mode===true) mode="home"; if(mode===false) mode="work";   // back-compat with the old boolean calls
  centerView=mode;
  const set=(id,on)=>{ const e=$(id); if(e) e.style.display=on?"":"none"; };
  set("home", mode==="home"); set("groupview", mode==="group"); set("scopechat", mode==="scopechat"); set("opsview", mode==="ops"); set("tabbody", mode==="work");
  if(mode!=="work"){ const bar=$("tabbar"); if(bar) bar.style.display="none"; }
  $("app").classList.toggle("no-task", mode==="home");   // facts hidden only on home (group/work show it)
}
function goHome(){            // clicking the cc logo deselects → back to the overview
  SEL=null; hideCard();
  centerMode(true); renderHome(); renderTree();
  const f=$("facts"); f.innerHTML=""; f.append(el("div","empty","выбери задачу — здесь появятся MR, ветки и действия"));
}
// ---- global OPS console (view): where each repo is deployed (dev/stage/prod) + the shipping history ----
const OPS_SHIP=new Set(["ops.start","group.mr","group.merge","task.merge"]);   // "shipping" actions for the history feed
const OPS_LABEL={ "ops.start":"тест/деплой запущен", "group.mr":"релиз: MR в master", "group.merge":"влиты задачи группы", "task.merge":"влита задача" };
function openOps(){ SEL=null; hideCard(); centerMode("ops"); renderOps(); }
async function renderOps(){
  const v=$("opsview"); if(!v) return; v.innerHTML="";
  const head=el("div","ops-head");
  head.append(el("div","ops-title","Операции — где что залито + история"),
              btn("↻ Обновить деплои", ()=>renderOps(), "ghost"));
  v.append(head);
  // history first (fast: local audit)
  v.append(el("div","ops-sec","История (деплои · релизы · мёржи)"));
  const hist=el("div","ops-hist"); hist.append(el("div","dim","загрузка…")); v.append(hist);
  // deploy state (slow: network glab/EAS)
  v.append(el("div","ops-sec","Где что залито (dev · stage · prod)"));
  const dep=el("div","ops-dep"); dep.append(el("div","dim","опрашиваю glab/EAS… (это сетевой запрос)")); v.append(dep);
  try{ const recs=JSON.parse((await invoke("run_cc",{args:["log","--json","-n","60"]}))||"[]").filter(r=>OPS_SHIP.has(r.action));
    if($("opsview")!==v) return; hist.innerHTML="";
    if(!recs.length) hist.append(el("div","dim","пока нет записей о деплоях/релизах"));
    recs.slice(0,20).forEach(r=>{ const row=el("div","ops-hrow");
      const d=new Date((r.ts||0)*1000), ts=("0"+d.getDate()).slice(-2)+"."+("0"+(d.getMonth()+1)).slice(-2)+" "+("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2);
      row.append(el("span","ops-ht", ts), el("span","ops-ha", OPS_LABEL[r.action]||r.action));
      const who=r.epic||r.task||r.project||""; if(who) row.append(el("span","ops-hw", who));
      const ex=[r.kind,r.repo,r.base].filter(Boolean).join(" · "); if(ex) row.append(el("span","ops-he", ex));
      hist.append(row); });
  }catch(e){ hist.innerHTML=""; hist.append(el("div","dim","✗ история: "+e)); }
  try{ const dd=JSON.parse((await invoke("run_cc",{args:["deploys","--all","--json"]}))||"[]");
    if($("opsview")!==v) return; dep.innerHTML="";
    const byProj={}; dd.forEach(x=>{ (byProj[x.project]=byProj[x.project]||[]).push(x); });
    if(!Object.keys(byProj).length) dep.append(el("div","dim","нет данных о деплоях"));
    for(const [pn,rows] of Object.entries(byProj)){
      dep.append(el("div","ops-proj", pn));
      rows.forEach(x=>{ const row=el("div","ops-drow"); row.append(el("span","ops-repo", x.repo));
        if(x.kind==="eas"){ const ch=x.channels||{};
          row.append(envChip("staging", ch.staging), envChip("prod", ch.production)); }
        else { const e=x.envs||{}; ["dev","stage","prod"].forEach(k=> row.append(envChip(k, e[k]?(e[k].ref+"@"+e[k].sha):null))); }
        dep.append(row); });
    }
  }catch(e){ dep.innerHTML=""; dep.append(el("div","dim","✗ деплои: "+e)); }
}
function envChip(env, val){ const c=el("span","ops-env env-"+env);
  c.append(el("span","ops-envk", env), el("span","ops-envv", val||"—")); return c; }
// ---- GROUP view: the feature's dashboard (its tasks by status + next step + flow) ----
function groupOf(pn, gkey){ const p=STATE&&STATE.projects[pn]; return p&&(p.groups||[]).find(g=>g.key===gkey); }
function selectGroup(pn, gkey){   // click a group → open its feature dashboard
  SEL={p:pn, g:gkey, tid:null}; hideCard();
  collapsed.delete("proj:"+pn); collapsed.delete("group:"+pn+"/"+gkey);   // reveal it in the tree too
  centerMode("group"); renderTree(); renderGroupView(pn,gkey); renderGroupFacts(pn,gkey);
}
function groupNextStep(g, tasks){   // one-line "what to do next" for the feature
  const by=(s)=>tasks.filter(t=>t.status===s).length;
  const running=by("running"), review=tasks.filter(t=>t.status==="mr"||t.status==="review").length;
  const allMerged=tasks.length>0 && tasks.every(t=>t.merged);
  const comb=(g.combined||[]).length;
  if(running) return "🔵 "+running+" задач(и) ещё в работе — доведи их.";
  if(review)  return "🟡 "+review+" на ревью — проверь и влей MR в GitLab.";
  if(allMerged && !comb) return "✅ все смёржены — можно собрать combined и тест/релиз.";
  if(comb) return "⊕ собрано "+comb+" — запусти combined / тест / релиз.";
  return "Добавь задачи в фичу (Cmd+T → этот эпик).";
}
function renderGroupView(pn, gkey){
  const v=$("groupview"); if(!v) return; v.innerHTML="";
  const g=groupOf(pn,gkey); if(!g){ v.append(el("div","empty","группа не найдена")); return; }
  const active=(g.tasks||[]).filter(t=>!t.archived);
  const archived=(g.tasks||[]).filter(t=>t.archived).length;
  v.append(el("div","gv-title", (g.summary||g.key)));
  const sub=el("div","gv-sub"); sub.append(el("span","gv-key", gkey), el("span",null, "·"), el("span",null, pn),
              el("span",null,"·"), el("span",null, active.length+" задач"));
  if((g.combined||[]).length) sub.append(el("span",null,"·"), el("span","gv-comb","⊕ собрано "+(g.combined||[]).length));
  v.append(sub);
  const nx=el("div","gv-nextrow");
  const ntb=btn("+ Задача", ()=>openNewTask(gkey), "ghost"); ntb.title="Новая задача в этой фиче (Cmd+T при открытой фиче — то же)";
  const fb=btn("💬 Чат фичи", ()=>openFeatureChat(pn,gkey), "ghost"); fb.title="Чат про состояние фичи (агент знает её задачи и combined)";
  nx.append(el("div","gv-next", groupNextStep(g, active)), ntb, fb);
  v.append(nx);
  const SECTIONS=[
    {label:"🔵 В работе", match:t=>t.status==="running"},
    {label:"🟡 На ревью", match:t=>t.status==="mr"||t.status==="review"},
    {label:"⚠ Нужен ответ / упало", match:t=>t.status==="needs_input"||t.status==="failed"},
    {label:"✅ Готовы / влиты", match:t=>t.merged||t.status==="done"},
  ];
  for(const sec of SECTIONS){
    const items=active.filter(sec.match); if(!items.length) continue;
    v.append(el("div","gv-sec", sec.label+"  ("+items.length+")"));
    items.forEach(t=>{ const r=el("div","gv-row");
      r.append(statusMark(t.status), el("span","gv-rt", t.title));
      if((t.repos||[]).length) r.append(el("span","gv-rm", (t.repos||[]).map(x=>x.repo).join(", ")));
      const w=shortTime(t.activity); if(w) r.append(el("span","hr-when", w));
      r.onclick=()=>selectTask(t, pn, gkey); v.appendChild(r); });
  }
  if(archived) v.append(el("div","gv-arch", "в архиве: "+archived+" (найти через поиск)"));
  v.append(el("div","gv-hint","клик по задаче — открыть её чат; действия фичи — справа"));
}
function renderGroupFacts(pn, gkey){
  const f=$("facts"); f.innerHTML="";
  const g=groupOf(pn,gkey); if(!g){ f.append(el("div","empty","—")); return; }
  const tip=(b,s)=>{ b.title=s; return b; };
  f.appendChild(el("div","sec","ФИЧА · "+gkey));
  f.appendChild(el("div","row2 dim", (g.summary||gkey)));
  // the flow, top to bottom: collect → run/test → ship
  f.appendChild(el("div","sec","1 · СОБРАТЬ ДЛЯ ТЕСТА"));
  const cn=(g.combined||[]).length;
  f.appendChild(el("div","row2 dim", cn? ("Собрано "+cn+" задач → "+g.combined_branch) : "Пусто — влей задачи в фичу (в задаче: ⊕ Влить в группу)"));
  const r1=el("div","row2 acts");
  r1.append(tip(btn("↻ Пересобрать", ()=>runAction(["group","combine",gkey],"пересобрать combined "+gkey,false), "ghost"), "Заново собрать общую (combined) ветку из влитых задач"));
  if(cn){ r1.append(tip(btn("▶ Запустить combined", ()=>openGroupTermByKey(pn,gkey), "ghost"), "Терминал в combined-ветке — поднять все репо вместе"),
                   tip(btn("Cursor", ()=>openGroupInCursor(gkey), "ghost"), "Открыть combined-worktree в Cursor")); }
  f.appendChild(r1);
  f.appendChild(el("div","sec","2 · ПРОВЕРИТЬ"));
  const r2=el("div","row2 acts");
  r2.append(tip(btn("Тест", ()=>runAction(["group","ops",gkey,"--kind","test"],"ops test "+gkey,false), "ghost"), "Прогнать тесты по фиче"),
            tip(btn("Stage", ()=>runAction(["group","ops",gkey,"--kind","stage"],"ops stage "+gkey,false), "ghost"), "Задеплоить фичу на stage"));
  f.appendChild(r2);
  f.appendChild(el("div","sec","3 · ВЫКАТИТЬ"));
  const r3=el("div","row2 acts");
  r3.append(tip(btn("Влить все задачи", ()=>runAction(["group","merge",gkey],"group merge "+gkey,false), "ghost"), "Слить MR всех задач фичи в их целевые ветки"),
            tip(btn("Релиз: MR в master", ()=>runAction(["group","mr",gkey],"group mr "+gkey,true), "warn"), "Создать релизный MR фичи в master (прод!)"));
  f.appendChild(r3);
}
async function openGroupTermByKey(pn, gkey){   // group terminal needs an owner task tab; use the group's first active task
  const g=groupOf(pn,gkey); const t0=(g.tasks||[]).find(t=>!t.archived);
  if(!t0){ setStatus("в фиче нет активных задач для терминала", true); return; }
  openGroupTerm(gkey, t0);
}
// ---- scoped "ask" chat: a chat ABOUT a feature or a project (context-seeded, no task) ----
const scopeSess={};   // remember the session per scope key so re-opening continues the conversation
function statusWord(t){ return t.merged?"влито ✓":(STATUS_LABEL[t.status]||t.status); }
async function featureContext(pn, gkey){
  const g=groupOf(pn,gkey); if(!g) return {cwd:"", text:""};
  const active=(g.tasks||[]).filter(t=>!t.archived);
  let mem=""; try{ mem=((await invoke("run_cc",{args:["epic","memory",gkey]}))||"").trim(); }catch(_){}
  const lines=active.map(t=>"- "+t.title+" — "+statusWord(t)+((t.combined)?" · в combined":"")+(t.branch?"  ["+t.branch+"]":""));
  const cn=(g.combined||[]).length;
  const text="Это чат ПРО ФИЧУ (не про код одной задачи). Отвечай про её состояние/прогресс, что осталось, "
    +"помоги собрать/протестить/релизнуть. Сам код правится в задачах — новые правки = новые задачи в фиче.\n\n"
    +"Фича: "+(g.summary||gkey)+"  ("+gkey+", проект "+pn+")\n"
    +"Задачи ("+active.length+"):\n"+(lines.join("\n")||"—")+"\n"
    +"Combined: "+(cn?(cn+" задач → "+g.combined_branch):"пусто")+"\n"
    +(mem && /(?!^#)\S/.test(mem.replace(/^#.*$/gm,"").trim()) ? ("\nЦель/память фичи:\n"+mem+"\n") : "");
  const t0=active[0];
  const p=STATE.projects[pn]||{};
  return {cwd: (t0&&t0.dir) || p.path || "", text};
}
function projectContext(pn){
  const p=STATE.projects[pn]||{};
  const parts=[];
  for(const g of (p.groups||[]).filter(g=>!g.archived)){
    const active=(g.tasks||[]).filter(t=>!t.archived); if(!active.length) continue;
    if(g.loose){ active.forEach(t=>parts.push("- "+t.title+" — "+statusWord(t))); }
    else { parts.push("• ["+(g.summary||g.key)+" / "+g.key+"]  "+active.length+" задач: "+active.map(statusWord).join(", ")); }
  }
  const text="Это чат ПРО ПРОЕКТ "+pn+" — общая картина: какие фичи и задачи, что в каком состоянии, помоги "
    +"сориентироваться и оркестрировать. Конкретный код — в задачах.\n\nКартина проекта:\n"+(parts.join("\n")||"—");
  return {cwd: p.path || "", text};
}
function openScopedChat(opts){   // opts: {key, title, cwd, firstContext, backFn}
  if(!opts.cwd){ setStatus("нет рабочей папки для чата — нужна хотя бы одна задача/путь проекта", true); return; }
  centerMode("scopechat");
  const host=$("scopechat"); host.innerHTML="";
  const headbar=el("div","sc-head");
  headbar.append(btn("←", ()=>{ opts.backFn&&opts.backFn(); }, "ghost"), el("span","sc-title", opts.title));
  const pane=el("div","tab-pane chat sc-pane");
  const list=el("div","chat-msgs"); const composer=el("div","chat-composer");
  const inp=el("textarea","chat-inp"); inp.placeholder="Спроси про "+opts.scope+"…  (Enter — отправить)"; inp.rows=1;
  const bar=el("div","composer-bar"); const send=btn("▶", ()=>tab.send(), "send");
  bar.append(el("span","cb-gap"), send); composer.append(inp, bar);
  pane.append(list, composer); host.append(headbar, pane);
  const id="scope-"+(++seq);
  const tab={ type:"chat", engine, dir:opts.cwd, id, msgs:[], session: scopeSess[opts.key]||null,
              firstContext:opts.firstContext, injectedMem: !!scopeSess[opts.key], busy:false, dirs:[] };
  tab.render=()=>{ list.innerHTML=""; for(const m of tab.msgs){
      const d=el("div","msg "+m.role);
      if(m.role==="assistant"){ d.innerHTML = m.text ? mdRender(m.text) : (m.busy?'<span class="typing">…</span>':""); }
      else if(m.role==="tool"){ d.textContent="▸ "+m.text; } else { d.textContent=m.text; }
      list.appendChild(d); }
    if(!tab.msgs.length) list.appendChild(el("div","chat-hint", opts.hint||"Спроси о состоянии — агент знает контекст."));
    list.scrollTop=list.scrollHeight; };
  tab.send=async()=>{ const v=inp.value.trim(); if(!v||tab.busy) return; inp.value=""; inp.style.height="auto"; await sendChat(tab, v); };
  inp.onkeydown=(e)=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); tab.send(); } };
  inp.oninput=()=>{ inp.style.height="auto"; inp.style.height=Math.min(160, inp.scrollHeight)+"px"; };
  const un=listen("chat-event",(e)=>{ if(e.payload && e.payload.id===id) handleChatEvent(tab, e.payload.line); });
  const ud=listen("chat-done",(e)=>{ if(e.payload && e.payload.id===id){ const last=tab.msgs[tab.msgs.length-1]; if(last&&last.busy) last.busy=false; tab.busy=false; tab.render(); if(tab.session) scopeSess[opts.key]=tab.session; } });
  tab.render(); inp.focus();
}
async function openFeatureChat(pn, gkey){
  const c=await featureContext(pn,gkey); const g=groupOf(pn,gkey);
  openScopedChat({ key:"g:"+gkey, scope:"фичу", title:"💬 "+(g?(g.summary||gkey):gkey), cwd:c.cwd, firstContext:c.text,
                   hint:"Спроси про фичу: что готово, что осталось, собери/протестируй/релизни.",
                   backFn:()=>selectGroup(pn, gkey) });
}
function openProjectChat(pn){
  const c=projectContext(pn);
  openScopedChat({ key:"p:"+pn, scope:"проект", title:"💬 Проект "+pn, cwd:c.cwd, firstContext:c.text,
                   hint:"Спроси про проект: что где, что катить, общая картина.",
                   backFn:()=>goHome() });
}
// ---- scratch: a floating "throwaway" chat that DOESN'T add a tab; reuses one session per context ----
const scratchSess={};
function scratchCtx(){   // cwd + a light context from whatever is selected (task / feature / project / first project)
  const t=findTask();
  if(t) return {key:"t:"+t.tid, cwd:t.dir, label:"⚡ Быстрый · "+t.title,
                ctx:"Это быстрый (черновой) чат в контексте задачи «"+t.title+"». Можешь делать что прошу прямо в этой папке. Кратко."};
  if(SEL && SEL.g && SEL.tid===null){ const g=groupOf(SEL.p,SEL.g); const t0=(g&&g.tasks||[]).find(x=>!x.archived);
    return {key:"g:"+SEL.g, cwd:(t0&&t0.dir)||(STATE.projects[SEL.p]||{}).path||"",
            label:"⚡ Быстрый · "+(g?(g.summary||SEL.g):SEL.g), ctx:"Быстрый чат в контексте фичи «"+(g?(g.summary||SEL.g):SEL.g)+"». Кратко."}; }
  const pn=(SEL&&SEL.p) || Object.keys(STATE&&STATE.projects||{})[0];
  const p=(STATE&&STATE.projects&&STATE.projects[pn])||{};
  return {key:"p:"+(pn||"x"), cwd:p.path||"", label:"⚡ Быстрый"+(pn?" · "+pn:""), ctx:"Быстрый черновой чат. Кратко."};
}
function closeScratch(){ const p=$("scratch"); if(p){ try{p._un&&p._un();}catch(e){} p.remove(); } }
async function toggleScratch(){
  if($("scratch")){ closeScratch(); return; }
  const c=scratchCtx();
  if(!c.cwd){ setStatus("нет рабочей папки для быстрого чата — выбери задачу/проект", true); return; }
  const pan=el("div"); pan.id="scratch";
  const head=el("div","scr-head"); head.append(el("span","scr-title", c.label), btn("✕", closeScratch, "ghost"));
  const list=el("div","chat-msgs scr-msgs"); const composer=el("div","chat-composer");
  const inp=el("textarea","chat-inp"); inp.placeholder="Быстрый вопрос / правка…  (Enter)"; inp.rows=1;
  const bar=el("div","composer-bar"); const send=btn("▶", ()=>tab.send(), "send"); bar.append(el("span","cb-gap"), send);
  composer.append(inp, bar); pan.append(head, list, composer); document.body.append(pan);
  const id="scratch-"+(++seq);
  const tab={ type:"chat", engine, dir:c.cwd, id, msgs:[], session:scratchSess[c.key]||null,
              firstContext:c.ctx, injectedMem:!!scratchSess[c.key], busy:false, dirs:[] };
  tab.render=()=>{ list.innerHTML=""; for(const m of tab.msgs){ const d=el("div","msg "+m.role);
      if(m.role==="assistant"){ d.innerHTML=m.text?mdRender(m.text):(m.busy?'<span class="typing">…</span>':""); }
      else if(m.role==="tool"){ d.textContent="▸ "+m.text; } else d.textContent=m.text; list.appendChild(d); }
    if(!tab.msgs.length) list.appendChild(el("div","chat-hint","Черновой чат — не плодит вкладок. Контекст: "+c.label.replace("⚡ Быстрый · ","")+"."));
    list.scrollTop=list.scrollHeight; };
  tab.send=async()=>{ const v=inp.value.trim(); if(!v||tab.busy) return; inp.value=""; inp.style.height="auto"; await sendChat(tab, v); };
  inp.onkeydown=(e)=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); tab.send(); } else if(e.key==="Escape"){ e.preventDefault(); closeScratch(); } };
  inp.oninput=()=>{ inp.style.height="auto"; inp.style.height=Math.min(140, inp.scrollHeight)+"px"; };
  const un=await listen("chat-event",(e)=>{ if(e.payload&&e.payload.id===id) handleChatEvent(tab,e.payload.line); });
  const ud=await listen("chat-done",(e)=>{ if(e.payload&&e.payload.id===id){ const last=tab.msgs[tab.msgs.length-1]; if(last&&last.busy) last.busy=false; tab.busy=false; tab.render(); if(tab.session) scratchSess[c.key]=tab.session; } });
  pan._un=()=>{ try{un();}catch(e){} try{ud();}catch(e){} };
  tab.render(); inp.focus();
}
// Cmd+T → create a task under a project (epic-less) or under an epic. Defaults: --manual (no auto
// background agent — you drive it in the cockpit chat) + --no-jira (quick local task; link Jira later).
// Cmd+T preset: a selected feature → its epic key (task lands IN the feature); a task → its project; else none
function newTaskPreset(){ if(SEL && SEL.g && SEL.tid===null) return SEL.g; if(SEL && SEL.p) return SEL.p; return undefined; }
function openNewTask(preset){   // preset = project name OR epic key → preselects "куда добавить"
  if(!STATE || !STATE.projects) return;
  const projNames=Object.keys(STATE.projects);
  if(!projNames.length){ setStatus("нет проектов", true); return; }
  const ov=el("div","overlay"); const box=el("div","modal form");
  const hd=el("div","mhead"); hd.append(el("span",null,"Новая задача"), btn("✕",()=>ov.remove(),"ghost"));
  const body=el("div","nt-body");
  const field=(label,ctrl)=>{ const f=el("div","nt-field"); f.append(el("label","nt-label",label), ctrl); return f; };
  // the prompt IS the task — big, first, autofocused. No title/repos: cc names it and provisions all
  // the project's repos automatically (mono = one, multi = all). The agent starts in the background.
  const promptInp=el("textarea","mem-inp nt-prompt"); promptInp.rows=5;
  promptInp.placeholder="Опиши задачу — что нужно сделать. Агент запустится сразу и начнёт работу.";
  // one "куда" selector: each project, with its epics nested (value = project name OR epic key)
  const where=document.createElement("select"); where.className="mem-inp";
  for(const pn of projNames){
    const og=document.createElement("optgroup"); og.label=pn;
    const o0=document.createElement("option"); o0.value=pn; o0.textContent="под проект "+pn; og.append(o0);
    for(const g of (STATE.projects[pn].groups||[]).filter(g=>!g.loose)){
      const o=document.createElement("option"); o.value=g.key; o.textContent="↳ "+(g.summary||g.key); og.append(o);
    }
    where.append(og);
  }
  if(preset){ const o=[...where.options].find(o=>o.value===preset); if(o) where.value=preset; }   // project or feature
  body.append(field("Задача", promptInp), field("Куда добавить", where));
  const foot=el("div","mem-foot"); const status=el("span","nt-status","");
  const run=async()=>{
    const desc=promptInp.value.trim(); if(!desc){ promptInp.focus(); status.textContent="опиши задачу"; return; }
    status.textContent="запускаю в фоне…"; runBtn.disabled=true;
    try{
      await invoke("run_cc",{args:["task","add", where.value, "--prompt", desc]});   // no --manual → bg agent; no --repos → all project repos
      ov.remove(); await load();
      const fresh=activeTasksFlat().sort((a,b)=>(b.t.activity||0)-(a.t.activity||0))[0];   // newest = the task we just created
      if(fresh) selectTask(fresh.t, fresh.pn, fresh.gkey);
    }catch(e){ status.textContent=""; runBtn.disabled=false; body.append(el("pre","nt-err","✗ "+e)); }
  };
  const runBtn=btn("▶ Запустить", run, "primary");
  foot.append(status, runBtn);
  promptInp.onkeydown=(e)=>{ if((e.metaKey||e.ctrlKey)&&e.key==="Enter"){ e.preventDefault(); run(); } };
  box.append(hd, body, foot); ov.append(box); document.body.append(ov);
  promptInp.focus();
}
// ---- global task search (Cmd+P) ----
// fuzzy subsequence: every query char must appear in order; bonuses for word-start + contiguous runs
function fuzzyScore(q, text){
  if(!q) return {score:0, pos:[]};
  const Q=q.toLowerCase(), T=(text||"").toLowerCase();
  let ti=0, score=0, pos=[], prev=-2;
  for(const c of Q){
    let f=-1;
    for(let k=ti;k<T.length;k++){ if(T[k]===c){ f=k; break; } }
    if(f<0) return null;
    const boundary = f===0 || /[\s\-\/\[\]_.()]/.test(T[f-1]);
    score += (f===prev+1) ? 8 : 1;     // contiguous run
    if(boundary) score += 12;          // start of a word
    pos.push(f); prev=f; ti=f+1;
  }
  return {score, pos};
}
function groupSummaryOf(pn, gkey){ const p=STATE&&STATE.projects[pn]; const g=p&&(p.groups||[]).find(x=>x.key===gkey); return g?(g.summary||g.key):(gkey||""); }
function snippetOf(t){ return (t.prompt||"").replace(/\s+/g," ").trim(); }
function taskHaystack(x){   // find by what it's ABOUT, not just the name: + description, + epic/group, + branch, + repos
  return [x.t.title, snippetOf(x.t), x.pn, x.gkey||"", groupSummaryOf(x.pn,x.gkey), x.t.branch||"",
          (x.t.repos||[]).map(r=>r.repo).join(" ")].join("  ");
}
function searchAll(q){   // tasks (content-aware) + groups; active first, then score, then recency
  const tasks=allTasksFlat();
  if(!q.trim())
    return tasks.filter(x=>!isArchived(x)).sort((a,b)=>(b.t.activity||0)-(a.t.activity||0)).slice(0,14).map(x=>({type:"task", x, arch:false, pos:[]}));
  const out=[];
  for(const x of tasks){
    const hay=fuzzyScore(q, taskHaystack(x)); if(!hay) continue;
    const titleM=fuzzyScore(q, x.t.title||"");
    out.push({type:"task", x, arch:isArchived(x), pos: titleM?titleM.pos:[], score: hay.score + (titleM?titleM.score+25:0)});
  }
  const seen=new Set();
  for(const [pn,p] of Object.entries(STATE.projects||{})) for(const g of (p.groups||[])){
    if(g.loose) continue; const key=pn+"/"+g.key; if(seen.has(key)) continue; seen.add(key);
    const gm=fuzzyScore(q, (g.summary||"")+"  "+g.key); if(!gm) continue;
    out.push({type:"group", g, pn, arch:!!g.archived, pos:gm.pos, score: gm.score+12});
  }
  out.sort((a,b)=> (a.arch-b.arch) || (b.score-a.score) || (((b.x&&b.x.t.activity)||0)-((a.x&&a.x.t.activity)||0)));
  return out.slice(0,16);
}
function revealGroup(pn, g){   // search → group: open it in the tree + land on its first task
  collapsed.delete("proj:"+pn); collapsed.delete("group:"+pn+"/"+g.key);
  const t0=(g.tasks||[]).find(t=>!t.archived) || (g.tasks||[])[0];
  if(t0) selectTask(t0, pn, g.key); else renderTree();
}
function hlTitle(title, pos){
  const set=new Set(pos), span=el("span","pal-title");
  for(let i=0;i<title.length;i++){
    if(set.has(i)) span.append(el("b","hl", title[i]));
    else span.appendChild(document.createTextNode(title[i]));
  }
  return span;
}
function openSearch(){
  const existing=$("palette"); if(existing){ existing.querySelector(".pal-input").focus(); return; }
  const ov=el("div","overlay pal-ov"); ov.id="palette";
  const box=el("div","palette-box");
  const inp=el("input","pal-input"); inp.placeholder="Поиск по названию И описанию задач/групп — по всем проектам, вкл. архив";
  const list=el("div","pal-list");
  box.append(inp, list); ov.append(box); document.body.append(ov);
  let results=[], sel=0;
  const markSel=()=>{ [...list.children].forEach((c,i)=>c.classList.toggle("sel", i===sel)); const cur=list.children[sel]; if(cur&&cur.scrollIntoView) cur.scrollIntoView({block:"nearest"}); };
  const taskRow2=(r,i)=>{
    const t=r.x.t;
    const row=el("div","pal-row two"+(i===sel?" sel":"")+(r.arch?" arch":""));
    const top=el("div","pal-top");
    top.append(statusMark(t.status), el("span","hr-proj", r.x.pn));
    top.append(r.pos.length ? hlTitle(t.title, r.pos) : el("span","pal-title", t.title));
    if(r.arch) top.append(el("span","pal-arch","архив"));
    const w = r.arch && t.archived_at ? t.archived_at.slice(5,10) : shortTime(t.activity);
    if(w) top.append(el("span","hr-when", w));
    row.append(top);
    // second line — what disambiguates identical titles: epic/group · branch · description
    const meta=[groupSummaryOf(r.x.pn, r.x.gkey), t.branch, snippetOf(t)].filter(Boolean).join("  ·  ");
    if(meta) row.append(el("div","pal-sub", meta));
    return row;
  };
  const groupRow2=(r,i)=>{
    const g=r.g, n=(g.tasks||[]).filter(t=>!t.archived).length;
    const row=el("div","pal-row two grp"+(i===sel?" sel":"")+(r.arch?" arch":""));
    const top=el("div","pal-top");
    top.append(el("span","pal-gico","▣"), el("span","hr-proj", r.pn));
    top.append(r.pos.length ? hlTitle(g.summary||g.key, r.pos) : el("span","pal-title", g.summary||g.key));
    if(r.arch) top.append(el("span","pal-arch","архив"));
    top.append(el("span","hr-when", "группа · "+n));
    row.append(top, el("div","pal-sub", g.key));
    return row;
  };
  const render=()=>{
    list.innerHTML="";
    results.forEach((r,i)=>{ const row = r.type==="group" ? groupRow2(r,i) : taskRow2(r,i);
      row.onclick=()=>pick(i); row.onmouseenter=()=>{ sel=i; markSel(); }; list.appendChild(row); });
    if(!results.length) list.append(el("div","pal-empty","ничего не найдено"));
  };
  const refresh=()=>{ results=searchAll(inp.value); sel=0; render(); };
  const pick=(i)=>{ const r=results[i]; if(!r) return; ov.remove();
    if(r.type==="group") revealGroup(r.pn, r.g); else selectTask(r.x.t, r.x.pn, r.x.gkey); };
  inp.oninput=refresh;
  inp.onkeydown=(e)=>{
    if(e.key==="ArrowDown"){ e.preventDefault(); sel=Math.min(results.length-1, sel+1); markSel(); }
    else if(e.key==="ArrowUp"){ e.preventDefault(); sel=Math.max(0, sel-1); markSel(); }
    else if(e.key==="Enter"){ e.preventDefault(); pick(sel); }
    else if(e.key==="Escape"){ e.preventDefault(); ov.remove(); }
  };
  ov.onclick=(e)=>{ if(e.target===ov) ov.remove(); };
  refresh(); inp.focus();
}
// ---- command palette (Cmd+P) + rebindable keymap ----
// Commands are the single source of truth for what's runnable; the keymap (default + user overrides
// in localStorage) maps a chord → command. The palette lists them and lets you rebind each.
const COMMANDS=[
  {id:"new-chat", key:"mod+t", label:"Новый чат", hint:"в текущей задаче", when:()=>!!findTask(),
    run:()=>{ const t=findTask(); if(t) openChatTab(t, engine); else openNewTask(newTaskPreset()); }},
  {id:"new-task", label:"Новая задача…", run:()=>openNewTask(newTaskPreset())},
  {id:"scratch", key:"mod+j", label:"Быстрый чат (scratch)", run:()=>toggleScratch()},
  {id:"ops", key:"mod+o", label:"Операции (деплои · история)", run:()=>openOps()},
  {id:"search", key:"mod+shift+f", label:"Поиск задач…", run:()=>openSearch()},
  {id:"palette", key:"mod+p", label:"Палитра команд…", run:()=>openCommandPalette()},
  {id:"home", label:"На обзор (домой)", run:()=>goHome()},
  {id:"diff", label:"Открыть diff", when:()=>!!findTask(), run:()=>{ const t=findTask(); if(t) openDiffTab(t); }},
  {id:"memory", label:"Память задачи…", when:()=>!!findTask(), run:()=>{ const t=findTask(); if(t) openMemory(t); }},
  {id:"collapse", label:"Свернуть всё дерево", run:()=>{ allKeys().forEach(k=>collapsed.add(k)); renderTree(); }},
  {id:"expand", label:"Развернуть всё дерево", run:()=>{ collapsed.clear(); renderTree(); }},
  {id:"refresh", label:"Обновить", run:()=>load()},
];
let USERKEYS={}; try{ USERKEYS=JSON.parse(localStorage.getItem("cc_keymap")||"{}"); }catch(_){ USERKEYS={}; }
function keyFor(id){ const c=COMMANDS.find(x=>x.id===id); return USERKEYS[id]!==undefined ? USERKEYS[id] : (c?c.key:undefined); }
function setKey(id, chord){
  for(const c of COMMANDS){ if(c.id!==id && keyFor(c.id)===chord) USERKEYS[c.id]=""; }   // a chord binds one command
  USERKEYS[id]=chord; localStorage.setItem("cc_keymap", JSON.stringify(USERKEYS));
}
function chordOf(e){   // only mod-key combos are app shortcuts (so plain typing never triggers a command)
  if(!(e.metaKey||e.ctrlKey)) return null;
  const k=(e.key||"").toLowerCase();
  if(k==="meta"||k==="control"||k==="shift"||k==="alt") return null;
  let c="mod"; if(e.altKey) c+="+alt"; if(e.shiftKey) c+="+shift"; return c+"+"+k;
}
function prettyChord(ch){ return !ch ? "—" : ch.split("+").map(p=> p==="mod"?"⌘":p==="shift"?"⇧":p==="alt"?"⌥":p.toUpperCase()).join(""); }
let capturing=false;
function globalKeydown(e){
  if(capturing) return;
  const chord=chordOf(e);
  const pal=$("cmdpal");
  if(pal){ if(chord && chord===keyFor("palette")){ e.preventDefault(); pal.remove(); } return; }  // Cmd+P toggles palette
  if(document.querySelector(".overlay")) return;   // another modal/search owns the keys while open
  if(!chord) return;
  for(const c of COMMANDS){ const k=keyFor(c.id); if(k && k===chord){ e.preventDefault(); c.run(); return; } }
}
function startCapture(cmd, chip, rerender){
  if(capturing) return;
  capturing=true; chip.textContent="нажми…"; chip.classList.add("capturing");
  const onKey=(ev)=>{
    ev.preventDefault(); ev.stopPropagation();
    if(ev.key==="Escape"){ done(); return; }
    const chord=chordOf(ev); if(!chord) return;   // wait for a real chord (ignore lone modifiers)
    setKey(cmd.id, chord); done();
  };
  const done=()=>{ capturing=false; chip.classList.remove("capturing"); document.removeEventListener("keydown",onKey,true); rerender&&rerender(); };
  document.addEventListener("keydown", onKey, true);   // capture phase → beats the global dispatcher
}
function openCommandPalette(){
  const ex=$("cmdpal"); if(ex){ ex.remove(); return; }   // toggle
  const ov=el("div","overlay pal-ov"); ov.id="cmdpal";
  const box=el("div","palette-box");
  const inp=el("input","pal-input"); inp.placeholder="Команда…   (клик по чипу справа — переназначить)";
  const list=el("div","pal-list");
  box.append(inp, list); ov.append(box); document.body.append(ov);
  const labelOf=c=> typeof c.label==="function"?c.label():c.label;
  let items=[], sel=0;
  const mark=()=>{ [...list.children].forEach((c,i)=>c.classList&&c.classList.toggle("sel", i===sel)); const cur=list.children[sel]; if(cur&&cur.scrollIntoView) cur.scrollIntoView({block:"nearest"}); };
  const render=()=>{
    list.innerHTML="";
    items.forEach((c,i)=>{
      const row=el("div","pal-row cmd"+(i===sel?" sel":""));
      row.append(el("span","cmd-label", labelOf(c)));
      if(c.hint) row.append(el("span","cmd-hint", c.hint));
      const chip=el("span","pal-key", prettyChord(keyFor(c.id))); chip.title="переназначить (Esc — отмена)";
      chip.onclick=(ev)=>{ ev.stopPropagation(); startCapture(c, chip, render); };
      row.append(chip);
      row.onclick=()=>runCmd(i); row.onmouseenter=()=>{ if(!capturing){ sel=i; mark(); } };
      list.appendChild(row);
    });
    if(!items.length) list.append(el("div","pal-empty","нет команд"));
  };
  const refresh=()=>{ const q=inp.value.trim().toLowerCase();
    items=COMMANDS.filter(c=> c.id!=="palette" && (!c.when||c.when())).filter(c=> !q || labelOf(c).toLowerCase().includes(q));
    sel=0; render(); };
  const runCmd=(i)=>{ const c=items[i]; if(!c) return; ov.remove(); c.run(); };
  inp.oninput=refresh;
  inp.onkeydown=(e)=>{ if(capturing) return;
    if(e.key==="ArrowDown"){ e.preventDefault(); sel=Math.min(items.length-1, sel+1); mark(); }
    else if(e.key==="ArrowUp"){ e.preventDefault(); sel=Math.max(0, sel-1); mark(); }
    else if(e.key==="Enter"){ e.preventDefault(); runCmd(sel); }
    else if(e.key==="Escape"){ e.preventDefault(); ov.remove(); }
  };
  ov.onclick=(e)=>{ if(e.target===ov) ov.remove(); };
  refresh(); inp.focus();
}
// flatten every task across projects/groups, keeping its project + group for selectTask
function allTasksFlat(){   // EVERYTHING incl. archived — used by search
  const out=[];
  for(const [pn,p] of Object.entries(STATE.projects||{}))
    for(const g of (p.groups||[]))
      for(const t of (g.tasks||[])) out.push({t, pn, gkey:g.key, garch:!!g.archived});
  return out;
}
function isArchived(x){ return !!(x.t.archived || x.garch); }   // archived task OR task in an archived group
function activeTasksFlat(){ return allTasksFlat().filter(x=>!isArchived(x)); }   // board view (tree/home/what's-new)
function homeRow(x){
  const r=el("div","home-row");
  r.append(statusMark(x.t.status), el("span","hr-proj", x.pn), el("span","hr-title", x.t.title));
  const w=shortTime(x.t.activity); if(w){ const sp=el("span","hr-when", w); sp.title="трогали "+relTime(x.t.activity); r.append(sp); }
  r.onclick=()=>selectTask(x.t, x.pn, x.gkey);
  return r;
}
const HOME_LIMIT=7; const homeExpanded=new Set();   // long sections collapse to N with an "ещё…" expander
// triage overview: what needs YOU, what's in review, what's running — only non-empty sections
function renderHome(){
  const h=$("home"); if(!h) return; h.innerHTML="";
  if(!STATE){ return; }
  const all=activeTasksFlat();   // home triage = active board only
  const htop=el("div","home-top");
  htop.append(el("div","home-title","cc — обзор"), btn("⚙ Операции", ()=>openOps(), "ghost"));
  h.append(htop);
  const SECTIONS=[
    {label:"⚠ Ждут тебя",  cls:"wait",   match:t=>t.status==="needs_input"||t.status==="failed"},
    {label:"◑ На ревью",   cls:"review", match:t=>t.status==="mr"||t.status==="review"},
    {label:"◐ В работе",   cls:"work",   match:t=>t.status==="running"},
  ];
  let shown=0;
  for(const sec of SECTIONS){
    const items=all.filter(x=>sec.match(x.t)).sort((a,b)=>(b.t.activity||0)-(a.t.activity||0));
    if(!items.length) continue;
    shown+=items.length;
    const box=el("div","home-sec "+sec.cls);
    box.append(el("div","home-sec-h", sec.label+"  ("+items.length+")"));
    const exp=homeExpanded.has(sec.cls);
    (exp?items:items.slice(0,HOME_LIMIT)).forEach(x=>box.append(homeRow(x)));
    if(!exp && items.length>HOME_LIMIT){
      const more=el("div","home-more","ещё "+(items.length-HOME_LIMIT)+"…");
      more.onclick=()=>{ homeExpanded.add(sec.cls); renderHome(); };
      box.append(more);
    }
    h.append(box);
  }
  if(!shown){   // all quiet → show the most recently touched as a soft landing
    h.append(el("div","home-calm","Всё спокойно — ничего не ждёт ответа."));
    const recent=all.sort((a,b)=>(b.t.activity||0)-(a.t.activity||0)).slice(0,6);
    if(recent.length){ const box=el("div","home-sec"); box.append(el("div","home-sec-h","недавние")); recent.forEach(x=>box.append(homeRow(x))); h.append(box); }
  }
  h.append(el("div","home-hint","клик по строке откроет задачу"));
}
// drag the dividers to resize the panes (like Cursor); widths persist across restarts
function setupResizers(){
  const app=$("app");
  const num=(v,d)=> parseInt(getComputedStyle(app).getPropertyValue("--"+v)) || d;
  app.style.setProperty("--sw", (+localStorage.getItem("cc_sw")||286)+"px");
  app.style.setProperty("--fw", (+localStorage.getItem("cc_fw")||320)+"px");
  const drag=(rz, v, sign, min, max, store, def)=>{
    if(!rz) return;
    rz.addEventListener("mousedown",(e)=>{
      e.preventDefault();
      const x0=e.clientX, w0=num(v,def);
      rz.classList.add("dragging"); document.body.classList.add("col-resizing");
      const move=(ev)=>{ const w=Math.max(min, Math.min(max, w0+sign*(ev.clientX-x0))); app.style.setProperty("--"+v, w+"px"); };
      const up=()=>{ document.removeEventListener("mousemove",move); document.removeEventListener("mouseup",up);
        rz.classList.remove("dragging"); document.body.classList.remove("col-resizing");
        localStorage.setItem(store, num(v,def)); };
      document.addEventListener("mousemove",move); document.addEventListener("mouseup",up);
    });
  };
  drag($("rz-left"),  "sw", +1, 180, 460, "cc_sw", 286);   // drag right → wider sidebar
  drag($("rz-right"), "fw", -1, 220, 540, "cc_fw", 320);   // drag left  → wider facts pane
  ["rz-left","rz-right"].forEach(idr=>{ const r=$(idr); if(r) r.addEventListener("mouseup", ()=>{ if(active&&active.refit) active.refit(); }); });
}
window.addEventListener("DOMContentLoaded", ()=>{ setupCenter(); setupResizers(); $("refresh").onclick=load;
  const brand=document.querySelector(".brand"); if(brand){ brand.style.cursor="pointer"; brand.title="На обзор"; brand.onclick=goHome; }
  document.addEventListener("keydown", globalKeydown);   // Cmd+T new chat · Cmd+Shift+F search · Cmd+P palette (rebindable)
  window.addEventListener("resize", ()=>{ if(active && active.refit) active.refit(); });   // keep the terminal fitted
  const sb=el("button","",""); sb.id="scratchbtn"; sb.textContent="⚡"; sb.title="Быстрый чат (Cmd+J) — черновой, без вкладки";
  sb.onclick=()=>toggleScratch(); document.body.appendChild(sb);
  let loading=false; const reload=async()=>{ if(loading) return; loading=true; try{ await load(); } finally{ loading=false; } };
  try{ invoke("watch_state"); listen("state-changed", reload); }catch(e){}   // realtime: refresh the moment cc writes state
  load(); setInterval(reload, 10000);   // slow safety poll (worktree-activity recency doesn't touch state.json)
  setInterval(pollMrs, 90000); });      // bounded network MR-state poll → catch GitLab merges on any task

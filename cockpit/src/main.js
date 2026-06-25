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
let tabs = [], active = null, seq = 0;   // middle = per-task chat/diff tabs; `active` is the shown tab object
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
    renderTree();
    const t=findTask();
    if(t) renderFacts(t);                 // a task is open → refresh its facts (never disturb open chats)
    else { renderHome(); centerMode(true); }   // nothing selected → live triage overview (refreshes each tick)
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
    const realGroups=p.groups.filter(g=>!g.loose).sort((a,b)=>groupAct(b)-groupAct(a));
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
      gh.onmouseenter=()=>showGroupCard(g, gh); gh.onmouseleave=hideCard;
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
}

// ---- middle: browser-style chat tabs, per task. No launcher — a default chat opens on select. ----
const lastTab = {};                 // tid -> last shown tab (restore the right chat when you return to a task)
const opening = new Set();          // guard so the periodic refresh can't double-open the default chat
function taskTabs(tid){ return tabs.filter(t=>t.taskId===tid); }
function selectTask(t, pn, gkey){
  SEL={p:pn,g:gkey,tid:t.tid};
  centerMode(false);
  renderTree(); renderFacts(t); showTaskChats(t);
}
function showTaskChats(t){          // bring this task's tabs to the front; open a default chat if it has none
  if(!t){ renderTabbar(); return; }
  const mine=taskTabs(t.tid);
  if(!mine.length){ ensureChat(t); return; }
  showTab((lastTab[t.tid] && mine.includes(lastTab[t.tid])) ? lastTab[t.tid] : mine[0]);
}
async function ensureChat(t){
  if(taskTabs(t.tid).length || opening.has(t.tid)) return;
  opening.add(t.tid);
  try{
    let opts={};
    try{   // resume the task's most recent REAL chat (old cc TUI conversation), if any
      const sess=JSON.parse((await invoke("run_cc",{args:["task","sessions",t.tid,"--json"]}))||"[]").filter(x=>x.kind==="chat");
      if(sess.length){
        const s0=sess[0];
        const hist=JSON.parse((await invoke("run_cc",{args:["task","history",s0.sid,"--json"]}))||"[]");
        opts={sid:s0.sid, dir:s0.dir, history:hist, preview:s0.preview};
      }
    }catch(_){}
    if(taskTabs(t.tid).length) return;   // a "+" opened one while we were fetching
    await openChatTab(t, engine, opts);
  } finally { opening.delete(t.tid); }
}
function renderTabbar(){
  const bar=$("tabbar"); bar.innerHTML="";
  const t=findTask();
  if(!t){ bar.style.display="none"; return; }
  bar.style.display="flex";
  for(const tb of taskTabs(t.tid)){
    const chip=el("div","tab"+(tb===active?" active":""));
    const ic=el("span","tab-ic", tb.type==="diff" ? "⟚" : (ENGINE_GLYPH[tb.engine]||"✦"));
    if(tb.type!=="diff") ic.classList.add("e-"+tb.engine);
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
  tabs.forEach(x=>{ x.el.style.display = x===tb ? (x.type==="chat"?"flex":"block") : "none"; });
  renderTabbar();
  if(tb.focus) try{ tb.focus(); }catch(e){}
}
function closeTab(tb){
  const i=tabs.indexOf(tb); if(i<0) return;
  try{ tb.unlisten&&tb.unlisten(); }catch(e){}
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
    let mem=""; try{ mem=((await invoke("run_cc",{args:["task","memory",tab.taskId]}))||"").trim(); }catch(_){}
    const hasMem = mem && /\S/.test(mem.replace(/^#.*$/gm,"").replace(/^##.*$/gm,"").trim());
    if(hasMem) ctx+="Память задачи (общий контекст всех чатов):\n"+mem+"\n\n";
    tab.injectedMem=true;
  }
  if(tab.carry){                                  // engine just changed → the new session has no native context
    const dig=tab.msgs.filter(m=>m.role!=="tool"&&m.text).slice(-8).map(m=>(m.role==="user"?"Я: ":"Агент: ")+m.text).join("\n\n");
    if(dig) ctx+="Контекст прошлого разговора (был другой движок), продолжаем здесь:\n"+dig+"\n\n";
    tab.carry=false;
  }
  if(!tab.toldMemory){   // teach the agent to self-record into the shared task memory (cockpit harvests the markers)
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
    if(!tab.msgs.length){ list.appendChild(el("div","chat-hint","Новый чат по задаче. Память задачи подхватится в первом сообщении.")); }
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
    tab.render(); harvestMemory(tab); } });
  tab.unlisten=()=>{un();ud();};
  tabs.push(tab); tab.render(); showTab(tab);     // chat opens ready; the agent only runs when you send
}
async function openDiffTab(t){
  const existing=taskTabs(t.tid).find(x=>x.type==="diff");   // one diff tab per task — focus + refresh, never duplicate
  if(existing){ showTab(existing); existing.reload&&existing.reload(); return; }
  const id="diff-"+(++seq);
  const pane=el("div","tab-pane diff"); const pre=el("pre","diffpre","загрузка diff…"); pane.append(pre); $("tabbody").append(pane);
  const tab={ type:"diff", title:"diff", el:pane, id, taskId:t.tid, unlisten:null };
  tab.reload=async()=>{ pre.style.color=""; pre.textContent="загрузка diff…";
    try{ pre.textContent = (await invoke("run_cc",{args:["task","diff",t.tid]}) || "(пусто)").trim() || "(нет изменений)"; }
    catch(e){ pre.textContent="✗ "+e; pre.style.color="#f87171"; } };
  tabs.push(tab); showTab(tab); tab.reload();
}

// ---- results modal + actions ----
function modal(title){
  const ov=el("div","overlay"); const box=el("div","modal");
  const hd=el("div","mhead"); hd.append(el("span",null,title), btn("✕", ()=>ov.remove(), "ghost"));
  const body=el("pre","mbody"); box.append(hd, body); ov.append(box); document.body.append(ov);
  return { ov, body };
}
async function openMemory(t){
  const ov=el("div","overlay"); const box=el("div","modal");
  const hd=el("div","mhead"); hd.append(el("span",null,"🧠 Память задачи · "+t.title), btn("✕",()=>ov.remove(),"ghost"));
  const body=el("pre","mbody");
  const foot=el("div","mem-foot");
  const inp=el("input","mem-inp"); inp.placeholder="запись в лог / новое направление…";
  const refresh=async()=>{ body.textContent="загрузка…"; try{ body.textContent=((await invoke("run_cc",{args:["task","memory",t.tid]}))||"").trim()||"(пусто)"; }catch(e){ body.textContent="✗ "+e; } };
  const act=async(flag, confirmMsg)=>{ const v=inp.value.trim(); if(!v){ inp.focus(); return; } if(confirmMsg&&!confirm(confirmMsg)) return;
    try{ await invoke("run_cc",{args:["task","memory",t.tid,flag,v]}); inp.value=""; await refresh(); load(); }catch(e){ body.textContent="✗ "+e; } };
  inp.onkeydown=(e)=>{ if(e.key==="Enter") act("--log"); };
  foot.append(inp,
    btn("+ В лог", ()=>act("--log"), "ghost"),
    btn("Задать «Текущее»", ()=>act("--current"), "ghost"),
    btn("🔄 Сменить направление", ()=>act("--pivot","Записать разворот? Текущее уйдёт в лог как заброшенное, направление очистится."), "warn"));
  box.append(hd, body, foot); ov.append(box); document.body.append(ov);
  refresh();
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
  trow.append(btn("Diff", ()=>openDiffTab(t), "ghost"));
  if(t.dir) trow.append(btn("Папка", ()=>openExt(t.dir), "ghost"));
  f.appendChild(trow);
  // combine toggle: pull this task's changes INTO the group's combined branch (or take them back out)
  if(!loose){ const crow=el("div","row2 acts");
    if(t.combined) crow.append(btn("⊖ Вынуть из группы", ()=>runAction(["group","combine",SEL.g,"--remove",t.tid],"вынуть "+t.tid+" из combined "+SEL.g,false), "ghost"));
    else crow.append(btn("⊕ Влить в группу", ()=>runAction(["group","combine",SEL.g,"--add",t.tid],"влить "+t.tid+" в combined "+SEL.g,false)));
    f.appendChild(crow); }
  // shared task memory — the knowledge every chat of this task should see
  f.appendChild(el("div","sec","ПАМЯТЬ ЗАДАЧИ"));
  const mrow=el("div","row2 acts");
  mrow.append(btn(t.has_memory?"🧠 Открыть":"🧠 Завести", ()=>openMemory(t)));
  f.appendChild(mrow);
  if(!t.has_memory) f.appendChild(el("div","row2 dim","ещё не заведена — решения/направление/находки"));
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
  const home=el("div"); home.id="home"; c.append(home);   // triage home shown when no task is selected
  const bar=el("div","tabbar"); bar.id="tabbar"; bar.style.display="none"; c.append(bar);
  const body=el("div","tabbody"); body.id="tabbody"; c.append(body);
  const f=$("facts"); f.innerHTML=""; f.append(el("div","empty","выбери задачу — здесь появятся MR, ветки и действия"));
}
// center has two modes: triage HOME (no task) vs the task's chat/diff tabs (WORK)
function centerMode(home){
  const h=$("home"), b=$("tabbody");
  if(h) h.style.display = home ? "block" : "none";
  if(b) b.style.display = home ? "none" : "";
  if(home){ const bar=$("tabbar"); if(bar) bar.style.display="none"; }
  $("app").classList.toggle("no-task", !!home);   // hide the facts pane while nothing is selected
}
function goHome(){            // clicking the cc logo deselects → back to the overview
  SEL=null; hideCard();
  centerMode(true); renderHome(); renderTree();
  const f=$("facts"); f.innerHTML=""; f.append(el("div","empty","выбери задачу — здесь появятся MR, ветки и действия"));
}
// Cmd+T → create a task under a project (epic-less) or under an epic. Defaults: --manual (no auto
// background agent — you drive it in the cockpit chat) + --no-jira (quick local task; link Jira later).
function openNewTask(presetProject){
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
  if(presetProject && STATE.projects[presetProject]) where.value=presetProject;
  body.append(field("Задача", promptInp), field("Куда добавить", where));
  const foot=el("div","mem-foot"); const status=el("span","nt-status","");
  const run=async()=>{
    const desc=promptInp.value.trim(); if(!desc){ promptInp.focus(); status.textContent="опиши задачу"; return; }
    status.textContent="запускаю в фоне…"; runBtn.disabled=true;
    try{
      await invoke("run_cc",{args:["task","add", where.value, "--prompt", desc]});   // no --manual → bg agent; no --repos → all project repos
      ov.remove(); await load();
      const fresh=allTasksFlat().sort((a,b)=>(b.t.activity||0)-(a.t.activity||0))[0];   // newest = the task we just created
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
function taskHaystack(x){   // everything worth matching, title first
  return [x.t.title, x.pn, x.gkey||"", x.t.branch||"", (x.t.repos||[]).map(r=>r.repo).join(" ")].join("  ");
}
function searchTasks(q){
  const all=allTasksFlat();
  if(!q.trim()) return all.sort((a,b)=>(b.t.activity||0)-(a.t.activity||0)).slice(0,14).map(x=>({x,pos:[]}));
  const out=[];
  for(const x of all){
    const combined=fuzzyScore(q, taskHaystack(x));
    if(!combined) continue;
    const titleM=fuzzyScore(q, x.t.title||"");
    out.push({x, score: combined.score + (titleM ? titleM.score+25 : 0), pos: titleM?titleM.pos:[]});  // title weighs more
  }
  out.sort((a,b)=> b.score-a.score || (b.x.t.activity||0)-(a.x.t.activity||0));   // recency breaks ties
  return out.slice(0,14);
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
  const inp=el("input","pal-input"); inp.placeholder="Поиск задач по всем проектам…  (название · проект · эпик · ветка · репо)";
  const list=el("div","pal-list");
  box.append(inp, list); ov.append(box); document.body.append(ov);
  let results=[], sel=0;
  const markSel=()=>{ [...list.children].forEach((c,i)=>c.classList.toggle("sel", i===sel)); const cur=list.children[sel]; if(cur&&cur.scrollIntoView) cur.scrollIntoView({block:"nearest"}); };
  const render=()=>{
    list.innerHTML="";
    results.forEach((r,i)=>{
      const row=el("div","pal-row"+(i===sel?" sel":""));
      row.append(statusMark(r.x.t.status), el("span","hr-proj", r.x.pn));
      row.append(r.pos.length ? hlTitle(r.x.t.title, r.pos) : el("span","pal-title", r.x.t.title));
      if(r.x.t.branch) row.append(el("span","pal-meta", r.x.t.branch));
      const w=shortTime(r.x.t.activity); if(w) row.append(el("span","hr-when", w));
      row.onclick=()=>pick(i); row.onmouseenter=()=>{ sel=i; markSel(); };
      list.appendChild(row);
    });
    if(!results.length) list.append(el("div","pal-empty","ничего не найдено"));
  };
  const refresh=()=>{ results=searchTasks(inp.value); sel=0; render(); };
  const pick=(i)=>{ const r=results[i]; if(!r) return; ov.remove(); selectTask(r.x.t, r.x.pn, r.x.gkey); };
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
// flatten every task across projects/groups, keeping its project + group for selectTask
function allTasksFlat(){
  const out=[];
  for(const [pn,p] of Object.entries(STATE.projects||{}))
    for(const g of (p.groups||[]))
      for(const t of (g.tasks||[])) out.push({t, pn, gkey:g.key});
  return out;
}
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
  const all=allTasksFlat();
  h.append(el("div","home-title","cc — обзор"));
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
}
window.addEventListener("DOMContentLoaded", ()=>{ setupCenter(); setupResizers(); $("refresh").onclick=load;
  const brand=document.querySelector(".brand"); if(brand){ brand.style.cursor="pointer"; brand.title="На обзор"; brand.onclick=goHome; }
  document.addEventListener("keydown",(e)=>{
    if((e.metaKey||e.ctrlKey) && (e.key==="t"||e.key==="T") && !e.shiftKey){   // Cmd/Ctrl+T → new task
      e.preventDefault(); if(!$("enginemenu")&&!$("sessmenu")) openNewTask(SEL?SEL.p:undefined);
    } else if((e.metaKey||e.ctrlKey) && (e.key==="p"||e.key==="P")){            // Cmd/Ctrl+P → search tasks
      e.preventDefault(); openSearch();
    }
  });
  load(); setInterval(load, 5000); });

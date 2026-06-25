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
  const lbl=el("span","label", t.title);     // full title + context live in the hover-card now
  row.append(statusMark(t.status), lbl);
  const w=shortTime(t.activity); if(w){ const sp=el("span","when", w); sp.title="трогали "+relTime(t.activity); row.append(sp); }
  row.onclick=()=>{ SEL={p:pn,g:gkey,tid:t.tid}; renderTree(); renderFacts(t); renderLauncher(t); };
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

// ---- middle: launcher + tabbar + tabbody (persistent so terminals survive) ----
function engineToggle(){
  const seg=el("div","seg");
  ENGINES.forEach(e=>{
    const o=el("button","seg-opt"+(e===engine?" on":""), e);
    o.onclick=()=>{ engine=e; localStorage.setItem("cc_engine", e); renderLauncher(findTask()); };
    seg.appendChild(o);
  });
  return seg;
}
function renderLauncher(t){
  const l=$("launcher"); l.innerHTML="";
  if(!t){ l.append(el("span","dim","Выбери задачу слева")); return; }
  const head=el("div","lc-head");
  head.append(statusMark(t.status), el("span","lc-title", t.title),
              el("span","lc-meta", t.branch+" · "+(STATUS_LABEL[t.status]||t.status)));
  const acts=el("div","lc-acts");
  acts.append(engineToggle(),
              btn("+ Чат", ()=>openChatTab(t, engine), "primary"),
              btn("+ Diff", ()=>openDiffTab(t), "ghost"));
  l.append(head, acts);
}
function renderTabbar(){
  const bar=$("tabbar"); bar.innerHTML="";
  bar.style.display = tabs.length ? "flex" : "none";
  tabs.forEach((tb,i)=>{
    const chip=el("div","tab"+(i===active?" active":""));
    const ic=el("span","tab-ic", tb.type==="diff" ? "⟚" : (ENGINE_GLYPH[tb.engine]||"✦"));
    if(tb.type!=="diff") ic.classList.add("e-"+tb.engine);
    chip.append(ic, el("span","tab-title", tb.title));
    const x=el("span","x","✕"); x.title="Закрыть"; x.onclick=(e)=>{ e.stopPropagation(); closeTab(i); }; chip.append(x);
    chip.onclick=()=>showTab(i); chip.title=tb.title; bar.appendChild(chip);
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
  if(ev.type==="system" && ev.subtype==="init" && ev.session_id){ tab.session=ev.session_id; return; }
  if(ev.type==="thread.started" && ev.thread_id){ tab.session=ev.thread_id; return; }   // codex
  if(ev.type==="stream_event" && ev.event){
    const e2=ev.event;
    if(e2.type==="content_block_delta" && e2.delta && e2.delta.type==="text_delta"){ if(a&&a.role==="assistant"){ a.text+=e2.delta.text; tab.render(); } return; }
    if(e2.type==="content_block_start" && e2.content_block && e2.content_block.type==="tool_use"){ tab.msgs.splice(tab.msgs.length-1,0,{role:"tool",text:e2.content_block.name||"tool"}); tab.render(); return; }
    return;
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
    await invoke(cmd, { id:tab.id, cwd:tab.dir, engine:tab.engine, prompt, session: tab.session||"" });
  }catch(e){ a.text="✗ не запустил движок: "+e; a.busy=false; tab.busy=false; tab.render(); }
}
async function openChatTab(t, engine){
  if(!t.dir){ setStatus("у задачи нет worktree-папки", true); return; }
  const id="chat-"+(++seq);
  const pane=el("div","tab-pane chat");
  const list=el("div","chat-msgs");
  const composer=el("div","chat-composer");
  const inp=el("textarea","chat-inp"); inp.placeholder="Сообщение агенту…  (Enter — отправить, Shift+Enter — перенос)"; inp.rows=1;
  composer.append(inp, btn("▶", ()=>tab.send(), "primary send"));
  pane.append(list, composer); $("tabbody").append(pane);
  const tab={ type:"chat", engine, title:t.title, el:pane, id, taskId:t.tid, dir:t.dir, msgs:[], session:null, busy:false };
  tab.render=()=>{ list.innerHTML=""; for(const m of tab.msgs){
      const d=el("div","msg "+m.role);
      if(m.role==="assistant"){ d.innerHTML = m.text ? mdRender(m.text) : (m.busy?'<span class="typing">…</span>':""); }
      else if(m.role==="tool"){ d.textContent="▸ "+m.text; }
      else { d.textContent=m.text; }
      list.appendChild(d);
    } list.scrollTop=list.scrollHeight; };
  tab.send=async()=>{ const v=inp.value.trim(); if(!v||tab.busy) return; inp.value=""; inp.style.height="auto"; await chatTurn(tab, v); };
  inp.onkeydown=(e)=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); tab.send(); } };
  inp.oninput=()=>{ inp.style.height="auto"; inp.style.height=Math.min(160, inp.scrollHeight)+"px"; };
  const un=await listen("chat-event",(e)=>{ if(e.payload && e.payload.id===id) handleChatEvent(tab, e.payload.line); });
  const ud=await listen("chat-done",(e)=>{ if(e.payload && e.payload.id===id){ const last=tab.msgs[tab.msgs.length-1]; if(last&&last.busy) last.busy=false; tab.busy=false; tab.render(); } });
  tab.unlisten=()=>{un();ud();};
  tabs.push(tab); showTab(tabs.length-1);
  // first turn: inject the task's shared memory + kick off (memory shown compactly, not as a wall of text)
  let mem=""; try{ mem=((await invoke("run_cc",{args:["task","memory",t.tid]}))||"").trim(); }catch(_){}
  const hasMem = mem && /\S/.test(mem.replace(/^#.*|^##.*/gm,"").trim());
  const prompt=(hasMem?"Память задачи (общий контекст всех чатов):\n"+mem+"\n\n":"")+"Задача: "+t.title+"\n\nРазберись и начни работу. Когда примешь решение или сменишь направление — кратко зафиксируй.";
  chatTurn(tab, prompt, "▶ старт: "+t.title+(hasMem?"  · с памятью задачи":""));
}
async function openDiffTab(t){
  const pane=el("div","tab-pane"); const pre=el("pre","diffpre","загрузка diff…"); pane.append(pre); $("tabbody").append(pane);
  const tab={ type:"diff", title:t.title, el:pane };
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
  c.append(el("div","launcher")); $("center").lastChild.id="launcher";
  const bar=el("div","tabbar"); bar.id="tabbar"; c.append(bar);
  const body=el("div","tabbody"); body.id="tabbody"; c.append(body);
  renderLauncher(null);
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
        localStorage.setItem(store, num(v,def));
        const tb=tabs[active]; if(tb&&tb.fit){ try{tb.fit.fit();}catch(_){} } };
      document.addEventListener("mousemove",move); document.addEventListener("mouseup",up);
    });
  };
  drag($("rz-left"),  "sw", +1, 180, 460, "cc_sw", 286);   // drag right → wider sidebar
  drag($("rz-right"), "fw", -1, 220, 540, "cc_fw", 320);   // drag left  → wider facts pane
}
window.addEventListener("resize", ()=>{ const tb=tabs[active]; if(tb&&tb.fit){ try{tb.fit.fit();}catch(e){} } });
window.addEventListener("DOMContentLoaded", ()=>{ setupCenter(); setupResizers(); $("refresh").onclick=load; load(); setInterval(load, 5000); });

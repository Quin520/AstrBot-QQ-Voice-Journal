"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("voice-journal-token") || "";
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("token")) {
  token = fragment.get("token");
  sessionStorage.setItem("voice-journal-token", token);
  history.replaceState(null, "", location.pathname);
}
let selected = "", members = [], current = null, busy = false;
const blobs = [];
const stateLabels = {receiving:"B 端录音同步中", recording:"采集中", stopped:"采集已停止", imported:"已导入", interrupted:"采集中断", error:"需要处理", queued:"等待转写", processing:"正在转写", done:"转写完成", pending:"待审核", approved:"已认定", rejected:"已排除"};
function node(tag, text, cls) { const e=document.createElement(tag); if(text !== undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function button(text, fn, cls="small secondary") { const e=node("button",text,cls); e.onclick=()=>action(fn,e); return e; }
function tell(text) { $("notice").textContent=text; $("notice").hidden=!text; }
async function api(path, method="GET", body) {
  const headers={Authorization:"Bearer "+token};
  if(body && !(body instanceof FormData))headers["Content-Type"]="application/json";
  const response=await fetch("/api/"+path,{method,headers,body:body instanceof FormData?body:body?JSON.stringify(body):undefined});
  const value=await response.json();
  if(response.status===401){$("login").hidden=false;$("workspace").hidden=true;}
  if(!response.ok)throw new Error(value.error||"请求失败");
  return value;
}
async function action(fn, element) {
  if(element)element.disabled=true;
  try { await fn(); } catch(error) {tell(error.message);} finally {if(element)element.disabled=false;}
}
async function loadDevices() {
  const data=await api("devices"); $("device").replaceChildren();
  const empty=node("option","选择 QQ 正在使用的输出设备");empty.value="";$("device").append(empty);
  for(const item of data.devices){const e=node("option",item.name);e.value=item.index;$("device").append(e);}
  if(!data.devices.length)tell("没有发现 WASAPI 回环设备，请检查 Windows 输出设备。");
}
async function loadMembers() {
  members=(await api("members")).members;$("member-list").replaceChildren();
  for(const item of members){const e=node("option",item.nickname+" · "+item.qq);e.value=item.qq;$("member-list").append(e);}
  tell("已同步 "+members.length+" 位群成员，仅用于身份选择。");
}
async function refresh() {
  if(busy)return;busy=true;
  try {
    const [status,listing,voices]=await Promise.all([api("status"),api("sessions"),api("voices")]);
    $("login").hidden=true;$("workspace").hidden=false;
    $("group-label").textContent="目标群 · "+(status.group_id||"请在插件中配置群号");
    const remote=status.remote||{enabled:false};
    $("remote-state").textContent=remote.enabled?"B 端接收已开启 · 端口 "+remote.port+(remote.last_upload?" · 最近收到录音 "+new Date(remote.last_upload*1000).toLocaleTimeString():" · 等待连接"):"请在插件配置中启用 B 端录音接收，并重启 AstrBot。";
    $("pair-button").disabled=!remote.enabled;$("revoke-button").disabled=!remote.enabled;
    if(!$("remote-address").value){const address=(remote.addresses||[]).find(a=>a.startsWith("100."));if(address)$("remote-address").value="https://"+address+":"+remote.port;}
    $("start-button").hidden=!!status.active_session;$("start-button").disabled=!status.capture_enabled;
    $("stop-button").hidden=!status.active_session;
    $("capture-state").textContent=status.active_session?"正在采集 · "+Math.floor((Date.now()/1000-status.capture_started)/60)+" 分钟":status.queued?"还有 "+status.queued+" 个分段待处理":"尚未采集";
    $("sessions").replaceChildren();
    for(const item of listing.sessions){const e=button(item.title,async()=>{selected=item.id;await refreshDetail();await refresh();},"session-button"+(selected===item.id?" active":""));e.append(node("small",new Date(item.created*1000).toLocaleString()+" · "+(stateLabels[item.status]||item.status)+" · "+item.pending+" 组待审核"));$("sessions").append(e);}
    if(!listing.sessions.length)$("sessions").append(node("p","还没有记录。","muted"));
    $("voices").replaceChildren();
    for(const person of voices.people){const row=node("div",undefined,"row");row.append(node("span",person.nickname+" · "+person.examples+" 个样本"));row.append(button("忘记",async()=>{if(!confirm("撤销此人在目标群的所有声纹认定？记录会保留，之后可重新审核。"))return;await api("forget","POST",{qq:person.qq});await refreshDetail();await refresh();}));$("voices").append(row);}
    if(!voices.people.length)$("voices").append(node("p","确认声音分组后，会在这里积累有效样本。","muted"));
    if(!selected&&listing.sessions.length){selected=listing.sessions[0].id;await refreshDetail();}
    $("summary-button").disabled=status.summarizing.includes(selected);
    if(status.summarizing.includes(selected))$("summary-state").textContent="正在生成复盘…";
  } finally {busy=false;}
}
async function sampleAudio(group,index,container,trigger) {
  const response=await fetch("/api/sample/"+group.id+"/"+index,{headers:{Authorization:"Bearer "+token}});
  if(!response.ok)throw new Error("无法载入试听片段。");
  const url=URL.createObjectURL(await response.blob());blobs.push(url);
  const player=document.createElement("audio");player.controls=true;player.src=url;container.replaceChild(player,trigger);await player.play();
}
async function refreshDetail() {
  if(!selected)return;
  current=await api("session/"+selected);
  while(blobs.length)URL.revokeObjectURL(blobs.pop());
  $("empty").hidden=true;$("record").hidden=false;
  $("record-title").textContent=current.title;
  $("record-status").textContent=(stateLabels[current.status]||current.status)+(current.error?" · "+current.error:"");
  $("end-remote-button").hidden=current.status!=="receiving";
  $("chunks").replaceChildren();
  for(const [index,chunk] of current.chunks.entries()){
    const row=node("div",undefined,"chunk row");row.append(node("span","分段 "+(index+1)+(chunk.source?" · "+chunk.source:"")+" · "+(stateLabels[chunk.status]||chunk.status)+(chunk.error?" · "+chunk.error:"")));
    if(chunk.status==="error")row.append(button("重试",async()=>{await api("retry/"+chunk.id,"POST",{});await refreshDetail();}));$("chunks").append(row);
  }
  $("clusters").replaceChildren();
  const labels=new Map();
  for(const [index,group] of current.clusters.entries()){
    const label="声音组 "+(index+1)+(group.source?" · "+group.source:"");labels.set(group.id,group.status==="approved"?group.nickname+"（"+group.qq+"）":label+"（身份未确认）");
    const card=node("div",undefined,"voice-card "+group.status);
    const heading=node("div",undefined,"row");heading.append(node("h3",label),node("span",stateLabels[group.status],"pill"));card.append(heading);
    const samples=node("div",undefined,"samples");
    group.samples.forEach((_,i)=>{const b=button("试听 "+(i+1),()=>sampleAudio(group,i,samples,b));samples.append(b);});card.append(samples);
    const texts=current.segments.filter(s=>s.cluster_id===group.id).slice(0,3).map(s=>"["+s.start.toFixed(1)+"s] "+s.text);card.append(node("p",texts.join("\n"),"excerpt"));
    card.append(node("p",group.warning+(group.eligible?" 确认后可用于后续匹配。":""),"warning"));
    const candidate=group.candidates.ranked[0];
    const fields=node("div",undefined,"identity");
    const qq=node("input"), nickname=node("input");qq.inputMode="numeric";qq.setAttribute("list","member-list");qq.placeholder="QQ 号";nickname.placeholder="昵称";qq.value=group.qq;nickname.value=group.nickname;
    qq.oninput=()=>{const person=members.find(p=>p.qq===qq.value);if(person)nickname.value=person.nickname;};
    const lq=node("label","归属 QQ"),ln=node("label","昵称");lq.append(qq);ln.append(nickname);fields.append(lq,ln);
    if(candidate){const c=node("div",undefined,"candidate");c.append(node("span",(group.candidates.suggested?"可能是 ":"待核对候选 ")+candidate.nickname+" · 相似度 "+candidate.score.toFixed(3)));c.append(button("填入候选",()=>{qq.value=candidate.qq;nickname.value=candidate.nickname;}));card.append(c);}
    card.append(fields);
    const actions=node("div",undefined,"actions");
    actions.append(button(group.status==="approved"?"更正认定":"确认是此人",async()=>{await api("review/"+group.id,"POST",{action:"approve",qq:qq.value.trim(),nickname:nickname.value.trim()});tell("身份已保存。复盘需按新身份重新生成。");await refreshDetail();await refresh();},"small"));
    actions.append(button("混合人声／排除",async()=>{await api("review/"+group.id,"POST",{action:"reject"});await refreshDetail();await refresh();}));
    if(group.status!=="pending")actions.append(button("撤销认定",async()=>{await api("review/"+group.id,"POST",{action:"revoke"});await refreshDetail();await refresh();}));
    card.append(actions);$("clusters").append(card);
  }
  if(!current.clusters.length)$("clusters").append(node("p","转写完成后，声音分组会出现在这里。","muted"));
  $("summary").textContent=current.summary||"暂未生成复盘。";
  $("summary-state").textContent=current.summary_error?"生成失败："+current.summary_error:current.summary_revision!==current.revision&&current.summary?"有新增发言或身份已更改；下面是旧版复盘，请重新生成。":"复盘仅保存在面板，不自动更改人物设置。";
  $("transcript").textContent=current.segments.map(s=>"["+s.start.toFixed(1)+"–"+s.end.toFixed(1)+"s] "+labels.get(s.cluster_id)+"："+s.text).join("\n\n");
}
$("login-button").onclick=()=>action(async()=>{token=$("token").value.trim();sessionStorage.setItem("voice-journal-token",token);await refresh();});
$("pair-button").onclick=()=>action(async()=>{const data=await api("remote/profile","POST",{address:$("remote-address").value.trim()});const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const link=node("a");link.href=url;link.download="A-连接.voicejournal";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);tell("连接文件已导出。将它传给自己的 B 电脑，在采集器中导入。");},$("pair-button"));
$("revoke-button").onclick=()=>action(async()=>{if(!confirm("旧连接文件将立即失效。B 需导入新文件才能继续同步；本地录音仍保留。"))return;await api("remote/revoke","POST",{});tell("旧连接已撤销，请重新导出连接文件。");},$("revoke-button"));
$("devices-button").onclick=()=>action(loadDevices,$("devices-button"));
$("members-button").onclick=()=>action(loadMembers,$("members-button"));
$("refresh-button").onclick=()=>action(async()=>{await refresh();await refreshDetail();});
$("start-button").onclick=()=>action(async()=>{if($("device").value==="")throw new Error("请选择输出设备。");const data=await api("capture/start","POST",{device:Number($("device").value),confirmed:$("confirmed").checked});selected=data.session_id;tell("采集已开始。停止后会自动处理最后一个分段。");await refresh();await refreshDetail();},$("start-button"));
$("stop-button").onclick=()=>action(async()=>{await api("capture/stop","POST",{});tell("已停止采集，正在处理录音。");await refresh();await refreshDetail();},$("stop-button"));
$("import-button").onclick=()=>action(async()=>{const file=$("audio-file").files[0];if(!file)throw new Error("请选择音频文件。");if(file.size>256*1024*1024)throw new Error("文件最大 256 MB。");const body=new FormData();body.append("audio",file);selected=(await api("import","POST",body)).session_id;tell("音频已进入处理队列。");await refresh();await refreshDetail();},$("import-button"));
$("summary-button").onclick=()=>action(async()=>{await api("summary/"+selected,"POST",{});tell("已开始生成复盘。");await refresh();});
$("delete-button").onclick=()=>action(async()=>{if(!confirm("删除本次录音、转写和审核记录？来源于本次记录的声纹样本也会删除。"))return;await api("delete/"+selected,"POST",{});selected="";current=null;$("record").hidden=true;$("empty").hidden=false;await refresh();});
$("end-remote-button").onclick=()=>action(async()=>{if(!confirm("结束 A 对这条录音的接收？这不会远程停止 B 的麦克风；未传完的录音将留在 B，不再自动传到 A。"))return;await api("remote/end/"+selected,"POST",{});await refreshDetail();await refresh();});
$("export-button").onclick=()=>{const blob=new Blob([$("transcript").textContent+"\n\n复盘：\n"+$("summary").textContent],{type:"text/plain;charset=utf-8"});const url=URL.createObjectURL(blob);const link=node("a");link.href=url;link.download="语音手记-"+selected.slice(0,8)+".txt";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
if(token)action(refresh);else $("login").hidden=false;
setInterval(()=>{if(!token||$("workspace").hidden)return;action(async()=>{await refresh();const editing=["INPUT","SELECT"].includes(document.activeElement.tagName)||[...document.querySelectorAll("audio")].some(a=>!a.paused);if(!editing&&selected)await refreshDetail();});},8000);

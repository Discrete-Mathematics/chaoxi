'use strict';
const $ = id => document.getElementById(id);
const modeNames = {economy:'经济模式', fast:'极速模式'};
const emptyAccount = {user:null,balance:0,modes:{economy:{live:false},fast:{live:false}},paymentsEnabled:false};
let account=emptyAccount, connected=false, owner='guest', chats=[], active=null, mode='economy';
let busy=false, attachment=null, initialised=false, paymentKey=null;
const money=n => '¥'+Number(n||0).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:6});
const date=n=>new Date(n).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
function el(tag,cls,txt) {
  const n=document.createElement(tag);
  if(cls) n.className=cls;
  if(txt!=null) n.textContent=txt;
  return n;
}
function btn(label,fn,cls='btn'){
  const b=el('button',cls,label); b.type='button'; b.addEventListener('click',fn); return b;
}
const txt=(id,value)=>{if($(id)) $(id).textContent=value;};
function errorText(err){return err?.message || '操作未完成，请重试。';}
async function api(path,payload){
  let response;
  try { response=await fetch('/api/'+path,{method:payload===undefined?'GET':'POST',credentials:'same-origin',
    headers:payload===undefined?{'Accept':'application/json'}:{'Content-Type':'application/json','Accept':'application/json'},
    body:payload===undefined?undefined:JSON.stringify(payload)}); }
  catch { throw Error('暂时无法连接服务。请检查网络后重试。'); }
  if(!response.headers.get('content-type')?.includes('application/json')) throw Error('此页面处于浏览器演示模式。');
  const body=await response.json();
  if(!response.ok) throw Object.assign(Error(body.error || '服务暂时不可用。'),{status:response.status});
  return body;
}
function drawer(open=false){
  document.body.classList.toggle('nav-open',open); $('menuBtn').setAttribute('aria-expanded',String(open));
}
function panel(title){
  drawer(); const d=$('appDialog'); d.replaceChildren(); d.setAttribute('aria-label',title);
  const head=el('div','dialog-head'); head.append(el('h2','',title));
  const close=btn('×',()=>d.close(),'btn dialog-close');close.setAttribute('aria-label','关闭');head.append(close);
  const body=el('div','dialog-body'); d.append(head,body); if(!d.open)d.showModal();
  return body;
}
function note(msg){const p=panel('提示');p.append(el('p','notice',msg));}
function field(parent,title,type='text'){
  const label=el('label','field'),input=el('input'); input.type=type;
  label.append(el('span','',title),input);parent.append(label);return input;
}
function selectChat(chat=null){
  active=chat; if(chat)mode=chat.mode;
  attachment=null; $('prompt').value=''; renderAttachment(); drawer(); render(); endScroll();
}
function endScroll(){requestAnimationFrame(()=>{const s=$('scrollPane');if(s)s.scrollTop=s.scrollHeight;});}
function render(){
  const live=Boolean(account.user && account.modes[mode]?.live && connected);
  txt('modeLabel',modeNames[mode]); txt('accountLabel',account.user?.username || '访客空间');
  txt('balanceLabel',money(account.balance));txt('statusLabel',live?'模型已连接':'演示模式');
  document.querySelector('.connection')?.classList.toggle('is-live', live);
  txt('threadTitle',active?.title || '新对话');
  $('welcome').hidden=!!active; $('messages').hidden=!active;
  $('messages').replaceChildren(); $('historyList').replaceChildren();
  for(const c of chats.slice(0,5)){
    const b=btn(c.title,()=>selectChat(c),'history-item'+(c===active?' active':'')); b.title=c.title; $('historyList').append(b);
  }
  if(!chats.length)$('historyList').append(el('p','empty-history','还没有对话记录'));
  for(const m of active?.messages || []){
    const block=el('article','message '+m.role);
    const label=el('div','message-meta',m.role==='user'?'你':('潮汐 · '+(m.demo?'演示回复':modeNames[m.mode||active.mode])));
    const content=el('div','message-body',m.content);block.append(label,content);
    if(m.usage && !m.demo)block.append(el('small','muted',m.usage.input+' 输入 / '+m.usage.output+' 输出 tokens · '+money(m.cost)));
    if(m.role==='assistant'){
      const copy=btn('复制',async()=>{try{await navigator.clipboard.writeText(m.content);copy.textContent='已复制';}catch{note('可选中回复文字复制。');}},'copy-reply');
      block.append(copy);
    }
    $('messages').append(block);
  }
  if(active?.pending){
    const n=el('div','reply-state');
    if(busy) n.textContent='潮汐正在回复…';
    else{
      n.append(el('span','',active.pending.error || '上次请求尚未确认。'),
        btn('重新请求',()=>submitTurn(active),'btn'),btn('结束这次请求',async()=>{
          const c=active; delete c.pending; await persist(c); render();
        },'btn'));
    }
    $('messages').append(n);
  }
  $('prompt').disabled=busy||!initialised; $('sendBtn').disabled=busy||!initialised||!!active?.pending;
  for(const id of ['newChat','accountBtn','historyBtn','importBtn','modeBtn','walletBtn','attachBtn']) $(id).disabled=busy;
  for(const b of $('historyList').querySelectorAll('button'))b.disabled=busy;
}
async function loadUser(next){
  account=next; owner=account.user?'u:'+account.user.id:'guest';
  chats=await TideHistory.list(owner); selectChat(); 
}
async function persist(c){
  await TideHistory.change(owner,[c]);
  chats=[c,...chats.filter(x=>x.id!==c.id)];
}
function renderAttachment(){
  const bar=$('attachmentBar'); bar.replaceChildren();bar.hidden=!attachment;
  if(attachment)bar.append(el('span','',attachment.name),btn('移除',()=>{attachment=null;renderAttachment();},'copy-reply'));
}
async function submitTurn(c){
  if(busy || !c?.pending)return;
  const pending=c.pending; busy=true; render();endScroll();
  try{
    await persist(c);
    // The immutable request id survives a refresh or lost response. A retry must carry the same context.
    const messages=c.messages.map(({role,content})=>({role,content}));
    let result;
    if(connected){
      result=await api('chat',{messages,requestId:pending.id,mode:pending.mode,expectedUserId:account.user?.id??null});
    } else result={demo:true,balance:0};
    const content=result.demo?tideExample(messages):result.reply;
    c.messages.push({role:'assistant',content,mode:pending.mode,demo:result.demo,usage:result.usage,cost:result.cost});
    account.balance=result.balance;c.time=Date.now();delete c.pending;
    await persist(c);
  }catch(e){
    if(c.pending)c.pending.error=errorText(e);
    else note('回复已显示，浏览器保存失败。请导出备份。');
    try{await persist(c);}catch{note('浏览器空间不足或无法写入。请导出对话备份。');}
  }finally{busy=false;render();endScroll();$('prompt').focus();}
}
async function send(event){
  event?.preventDefault(); if(!initialised||busy||active?.pending)return;
  const typed=$('prompt').value.trim();
  if(!typed && !attachment){$('prompt').focus();return;}
  let content=typed;
  if(attachment)content+=(typed?'\n\n':'')+'【附件：'+attachment.name+'】\n'+attachment.content;
  const c=active||{id:crypto.randomUUID(),title:(typed||attachment.name).slice(0,32),mode,messages:[]};
  c.mode=mode;c.time=Date.now();c.pending={id:crypto.randomUUID(),mode};c.messages.push({role:'user',content});active=c;
  attachment=null;renderAttachment();$('prompt').value='';await submitTurn(c);
}
function modes(){
  const p=panel('选择对话模式');
  for(const [id,name] of Object.entries(modeNames)){
    const config=account.modes[id]||{};
    const b=btn('',async()=>{mode=id;if(active){active.mode=id;await persist(active);}render();$('appDialog').close();},'mode-option'+(id===mode?' selected':''));
    b.disabled=!!active?.pending;
    b.append(el('strong','',name+(id===mode?' ✓':'')),el('span','muted',id==='economy'?'日常交流，优先控制成本':'使用直连模型，适合深入讨论'));
    b.append(el('small','muted',config.live ? ('输入 '+money(config.inputRate)+' · 输出 '+money(config.outputRate)+' / 百万 tokens'):'当前为免费演示'));
    p.append(b);
  }
  if(active?.pending)p.append(el('p','notice','请先结束待确认的请求，再切换模式。'));
}
function historyPanel(){
  const p=panel('对话记录');const search=field(p,'搜索标题或正文','search');search.placeholder='输入关键词';
  const list=el('div','record-list');p.append(list);
  function fill(){
    const q=search.value.trim().toLowerCase();
    const rows=chats.filter(c=>(c.title+' '+c.messages.map(m=>m.content).join(' ')).toLowerCase().includes(q));
    list.replaceChildren();
    if(!rows.length)list.append(el('p','muted',q?'没有找到匹配的对话。':'第一条消息发送后，对话会出现在这里。'));
    for(const c of rows){
      const row=el('div','panel-row');
      const open=btn('',()=>{$('appDialog').close();selectChat(c);},'record-open');
      open.append(el('strong','',c.title),el('small','muted',date(c.time)+' · '+c.messages.length+' 条消息'));
      const del=btn('删除',()=>{const d=panel('删除这条对话？');d.append(el('p','',c.title),el('p','muted','删除后只能通过已有备份恢复。'),btn('取消',historyPanel),btn('确认删除',async()=>{
        await TideHistory.change(owner,[c],true);chats=chats.filter(x=>x!==c);if(active===c)selectChat();render();historyPanel();
      },'btn danger'));},'copy-reply');
      row.append(open,del);list.append(row);
    }
  }
  search.oninput=fill;fill();
}
function backupPanel(){
  const p=panel('记录迁移');p.append(el('p','muted','记录保存在当前浏览器。导出一份备份，即可带到另一台设备继续聊天。'));
  const exportBtn=btn('导出潮汐备份',()=>{
    const records=chats.map(({title,time,mode,messages})=>({title,time,mode,messages}));
    const blob=new Blob([JSON.stringify({format:'tide-v1',conversations:records},null,2)],{type:'application/json'});
    const link=el('a');link.href=URL.createObjectURL(blob);link.download='潮汐对话-'+new Date().toISOString().slice(0,10)+'.json';
    link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);
  });exportBtn.disabled=!chats.length;
  const input=el('input');input.type='file';input.accept='.json,.zip';input.hidden=true;
  const msg=el('p','notice');msg.hidden=true;
  input.onchange=async()=>{
    const f=input.files[0];if(!f)return;
    try{
      const imported=await TideHistory.read(f);
      const key=c=>JSON.stringify([c.title,c.messages.map(m=>[m.role,m.content])]);
      const used=new Set(chats.map(key));const unique=imported.filter(c=>{const k=key(c);if(used.has(k))return false;used.add(k);return true;});
      if(!unique.length){msg.hidden=false;msg.textContent='这些对话已存在。';input.value='';return;}
      const q=panel('确认导入');q.append(el('p','',f.name),el('p','muted','将添加 '+unique.length+' 条对话，跳过 '+(imported.length-unique.length)+' 条重复记录。现有记录会保留。'));
      q.append(btn('返回',backupPanel),btn('导入对话',async e=>{e.currentTarget.disabled=true;try{await TideHistory.change(owner,unique);chats=await TideHistory.list(owner);render();historyPanel();}catch(err){note(errorText(err));}},'btn primary'));
    }catch(err){input.value='';msg.hidden=false;msg.textContent='无法导入：'+errorText(err);}
  };
  p.append(el('h3','','导出本机对话'),el('p','muted','当前空间共 '+chats.length+' 条对话。'),exportBtn,
    el('h3','','从备份导入'),el('p','muted','支持潮汐 JSON、DeepSeek 会话 JSON 和 v3 ZIP。DeepSeek 迁移仅保留所选分支的问答正文。'),btn('选择备份文件',()=>input.click()),input,msg);
}
function accountPanel(register=false){
  const p=panel(account.user?'我的账户':(register?'创建账户':'登录潮汐'));
  if(!connected){p.append(el('p','notice','这里是浏览器演示版。运行项目中的启动脚本后，即可使用本机账户与钱包。'));return;}
  if(account.user){
    p.append(el('h3','',account.user.username),el('p','muted','当前对话保存在此设备的账户空间。'),btn('退出登录',async()=>{
      try{const state=await api('logout',{});await loadUser(state?.modes?state:await api('session'));$('appDialog').close();}
      catch(e){note(errorText(e));}
    }));return;
  }
  const f=el('form','auth-form'),name=field(f,'用户名'),pass=field(f,'密码','password');
  name.required=pass.required=true;name.autocomplete='username';pass.autocomplete=register?'new-password':'current-password';
  if(register){name.minLength=2;name.maxLength=40;pass.minLength=8;pass.maxLength=128;pass.placeholder='至少 8 个字符';}
  const msg=el('p','notice');msg.hidden=true;const go=el('button','btn primary',register?'创建并登录':'登录');go.type='submit';
  f.append(msg,go);f.onsubmit=async e=>{e.preventDefault();go.disabled=true;
    try{await loadUser(await api(register?'register':'login',{username:name.value.trim(),password:pass.value}));$('appDialog').close();}
    catch(err){msg.hidden=false;msg.textContent=errorText(err);}finally{go.disabled=false;}
  };p.append(f,btn(register?'已有账户，返回登录':'创建新账户',()=>accountPanel(!register),'text-button'));
}
async function wallet(){
  const p=panel('余额与用量');
  try{
    if(connected){
      const state=await api('session');
      if((state.user?.id??null)!==(account.user?.id??null))await loadUser(state);else account=state;
    }
    if(!p.isConnected)return;
    render();p.append(el('small','muted','可用余额'),el('div','wallet-total',money(account.balance)));
    if(!account.user){p.append(el('p','notice','访客可免费体验演示对话。登录后可查看个人余额与用量。'),btn(connected?'登录账户':'查看账户说明',()=>accountPanel(),'btn primary'));return;}
    if(account.paymentsEnabled){
      p.append(el('h3','','充值金额'));const amounts=el('div','dialog-actions');p.append(amounts);
      for(const cents of [1000,3000,10000]) amounts.append(btn('¥'+cents/100,()=>{
        const q=panel('确认充值');q.append(el('p','','充值金额 '+money(cents/100)),el('p','muted','将前往 Stripe 收银台。付款到账后可在用量明细中查看。'));
        q.append(btn('返回',wallet),btn('前往支付',async e=>{const b=e.currentTarget;b.disabled=true;
          try{
            if(!paymentKey || paymentKey.cents!==cents || paymentKey.owner!==owner) paymentKey={id:crypto.randomUUID(),cents,owner};
            const res=await api('payments/checkout',{amount_cents:cents,idempotency_key:paymentKey.id,expectedUserId:account.user.id});
            const url=new URL(res.url);if(url.protocol!=='https:' || url.hostname!=='checkout.stripe.com')throw Error('收银台地址异常。');
            location.assign(url.href);
          }catch(err){b.disabled=false;q.append(el('p','notice',errorText(err)));}
        },'btn primary'));
      }));
    }else p.append(el('p','notice','充值尚未开放。管理员配置支付渠道后可充值。'));
    p.append(el('h3','','用量明细'),btn('刷新明细',wallet,'text-button'));
    const {items}=await api('ledger');if(!p.isConnected)return;
    if(!items.length)p.append(el('p','muted','还没有收支记录。免费演示不会扣减余额。'));
    for(const item of items){
      const row=el('div','panel-row'),info=el('div');info.append(el('strong','',item.type==='topup'?'账户充值':item.type==='credit'?'体验额度':modeNames[item.mode]||'对话用量'),
        el('small','muted',date(item.createdAt)+(item.type==='chat'?' · '+item.input+' 输入 / '+item.output+' 输出':'')));
      row.append(info,el('strong','',(item.amount>0?'+':'')+money(item.amount)));p.append(row);
    }
  }catch(err){p.append(el('p','notice',errorText(err)));}
}
document.addEventListener('DOMContentLoaded',async()=>{
  $('newChat').onclick=()=>{selectChat();$('prompt').focus();};
  $('historyBtn').onclick=historyPanel;$('importBtn').onclick=backupPanel;$('walletBtn').onclick=wallet;
  $('accountBtn').onclick=()=>accountPanel();$('modeBtn').onclick=modes;$('composer').onsubmit=send;
  $('menuBtn').onclick=()=>drawer(!document.body.classList.contains('nav-open'));$('scrim').onclick=()=>drawer();
  document.addEventListener('keydown',e=>{if(e.key==='Escape')drawer();});
  $('appDialog').addEventListener('click',e=>{if(e.target===$('appDialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
  $('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();send();}};
  for(const b of document.querySelectorAll('.suggestion[data-prompt]')) b.onclick=()=>{$('prompt').value=b.dataset.prompt;$('prompt').focus();};
  $('attachBtn').onclick=()=>$('attachInput').click();
  $('attachInput').onchange=async e=>{
    const f=e.target.files[0];e.target.value='';if(!f)return;
    if(f.size>256*1024 || !/\.(txt|md|csv)$/i.test(f.name)){note('请选择 256 KB 以内的 TXT、Markdown 或 CSV 文本。');return;}
    try{attachment={name:f.name,content:await f.text()};renderAttachment();$('prompt').focus();}catch{note('文本读取失败。');}
  };
  for(const id of ['newChat','accountBtn','historyBtn','importBtn','modeBtn','walletBtn','attachBtn','sendBtn','prompt']) $(id).disabled=true;
  try{if(location.protocol!=='file:'){account=await api('session');connected=true;}}catch{}
  try{await loadUser(account);initialised=true;render();if(new URLSearchParams(location.search).has('payment'))await wallet();}
  catch(e){note('当前浏览器无法使用本地会话存储。请使用完整浏览器打开应用。'+errorText(e));}
});

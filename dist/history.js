/* Local conversations are partitioned by account. Each save only replaces one chat. */
const TideHistory = (() => {
  let database;
  async function db() {
    if (!database) database = new Promise((resolve, reject) => {
      const open = indexedDB.open('tide-studio', 1);
      open.onupgradeneeded = () => {
        const chats = open.result.createObjectStore('chats', {keyPath:['owner','id']});
        chats.createIndex('owner', 'owner');
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
    return database;
  }
  async function list(owner) {
    const conn = await db();
    return new Promise((resolve, reject) => {
      const req = conn.transaction('chats').objectStore('chats').index('owner').getAll(owner);
      req.onsuccess = () => resolve(req.result.sort((a,b) => b.time - a.time));
      req.onerror = () => reject(req.error);
    });
  }
  async function change(owner, items, remove = false) {
    const conn = await db();
    return new Promise((resolve, reject) => {
      const tx = conn.transaction('chats','readwrite');
      const store = tx.objectStore('chats');
      for (const item of items) {
        if (remove) store.delete([owner,item.id]);
        else store.put({...item,owner});
      }
      tx.oncomplete = resolve;
      tx.onabort = () => reject(tx.error || Error('浏览器无法保存对话。'));
    });
  }
  const decoder = new TextDecoder('utf-8', {fatal:true});
  const MAX_TEXT = 32 * 1024 * 1024;
  function fail(message) { throw Error(message); }
  const checksumTable = Array.from({length:256}, (_,n) => {
    let v=n;
    for(let i=0;i<8;i++) v=(v>>>1)^((v&1)?0xedb88320:0);
    return v>>>0;
  });
  function crc32(bytes) {
    let value=0xffffffff;
    for (const byte of bytes) value=(value>>>8)^checksumTable[(value^byte)&255];
    return (~value)>>>0;
  }
  async function unpack(bytes) {
    const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const u16 = i => v.getUint16(i,true), u32 = i => v.getUint32(i,true);
    let end=bytes.length-22;
    while (end>=Math.max(0,bytes.length-65557) && u32(end)!==0x06054b50) end--;
    if (end<0 || u32(end)!==0x06054b50) fail('ZIP 文件不完整。');
    if (u16(end+4)||u16(end+6)) fail('请使用单卷 ZIP 文件。');
    const directory=u32(end+16);
    if (end+22+u16(end+20)!==bytes.length || directory+u32(end+12)!==end
        || u16(end+8)!==u16(end+10)) fail('ZIP 目录不完整。');
    let offset=directory, total=0;
    const legacy=[], sessions=new Map(), seen=new Set();
    let version, index;
    for (let i=0;i<u16(end+10);i++) {
      if(offset+46>end || u32(offset)!==0x02014b50) fail('ZIP 目录损坏。');
      const flags=u16(offset+8), method=u16(offset+10), crc=u32(offset+16);
      const packed=u32(offset+20), size=u32(offset+24), local=u32(offset+42);
      const len=u16(offset+28), name=decoder.decode(bytes.subarray(offset+46,offset+46+len));
      offset+=46+len+u16(offset+30)+u16(offset+32);
      if(offset>end) fail('ZIP 目录损坏。');
      if (name!=='manifest.json' && name!=='chat_session_index.json' && !/^chat_sessions\/[^/]+\.json$/.test(name) && !/(^|\/)conversations\.json$/.test(name)) continue;
      if(seen.has(name)) fail('压缩包包含重复的会话文件。');
      seen.add(name);
      total+=size;
      if(total>MAX_TEXT || flags&1 || ![0,8].includes(method)) fail('仅支持未加密的普通 ZIP，正文上限 32 MB。');
      if(local+30>directory || u32(local)!==0x04034b50) fail('ZIP 数据损坏。');
      const start=local+30+u16(local+26)+u16(local+28);
      if(start+packed>directory || decoder.decode(bytes.subarray(local+30,local+30+u16(local+26)))!==name) fail('ZIP 文件不完整。');
      let content=bytes.subarray(start,start+packed);
      if(method===8) {
        const reader=new Blob([content]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
        const buffer=new Uint8Array(size);
        let pos=0;
        for(;;) {
          const {value,done}=await reader.read();
          if(done) break;
          if(pos+value.length>size) { await reader.cancel(); fail('解压长度异常。'); }
          buffer.set(value,pos); pos+=value.length;
        }
        if(pos!==size) fail('ZIP 数据不完整。');
        content=buffer;
      }
      if(content.length!==size || crc32(content)!==crc) fail('文件校验失败。');
      const data=JSON.parse(decoder.decode(content));
      if(name==='manifest.json') version=data.export_format_version;
      else if(name==='chat_session_index.json') index=data;
      else if(name.startsWith('chat_sessions/')) {
        if(typeof data?.id!=='string' || name!=='chat_sessions/'+data.id+'.json') fail('会话编号与文件名不符。');
        sessions.set(data.id, data);
      } else {
        const records=Array.isArray(data)?data:[data];
        if(legacy.length+records.length>10000) fail('备份中的会话数量异常。');
        for(const record of records) legacy.push(record);
      }
    }
    if(offset!==end) fail('ZIP 目录损坏。');
    if(!sessions.size && !seen.has('manifest.json') && !seen.has('chat_session_index.json')) return legacy;
    if(version!==3) fail('DeepSeek 备份缺少 v3 格式清单。');
    if(!Array.isArray(index) || index.length!==sessions.size || new Set(index.map(row=>row?.id)).size!==index.length
        || !index.every(row=>sessions.has(row?.id))) fail('DeepSeek 会话索引不完整。');
    return index.map(row=>({...sessions.get(row.id), updated_at:row.updated_at ?? sessions.get(row.id).updated_at}));
  }
  function branch(leaf, find, parent) {
    const result=[], seen=new Set();
    for(let id=leaf;id!=null;){
      const node=find(id);
      if(!node || seen.has(id)) fail('会话分支数据不完整。');
      seen.add(id); result.push(node); id=parent(node);
    }
    return result.reverse();
  }
  function parseChat(chat) {
    let rows=chat?.messages;
    if(chat?.mapping){
      const mapping=chat.mapping;
      const leaf=chat.current_node || Object.values(mapping).filter(x=>!x.children?.length).at(-1)?.id;
      rows=branch(leaf,id=>mapping[id],x=>x.parent).map(x=>x.message).filter(Boolean);
    } else if(Array.isArray(rows) && rows.some(m=>m && 'parent_id' in m)) {
      if(!rows.every(m=>Number.isSafeInteger(m?.id) && m.id>=0 && 'parent_id' in m)) fail('会话消息编号无效。');
      const byId=new Map(rows.map(m=>[m.id,m]));
      if(byId.size!==rows.length) fail('会话包含重复消息编号。');
      const leaf=byId.has(chat.current_message_id) ? chat.current_message_id : rows.reduce((max,m)=>Math.max(max,m.id),0);
      rows=branch(leaf,id=>byId.get(id),x=>x.parent_id);
    }
    if(!Array.isArray(rows)) fail('会话缺少 messages 列表。');
    const messages=[];
    for(const row of rows){
      const role=String(row?.role || row?.author?.role || '').toLowerCase();
      const fragments=row?.fragments || row?.content?.fragments;
      if(!role && chat.mapping && Array.isArray(fragments)){
        let group;
        for(const fragment of fragments){
          const inferred=fragment?.type==='REQUEST'?'user':fragment?.type==='RESPONSE'?'assistant':null;
          if(!inferred || typeof fragment.content!=='string' || !fragment.content.trim()) continue;
          if(group?.role===inferred) group.content+='\n'+fragment.content;
          else { group={role:inferred,content:fragment.content}; messages.push(group); }
        }
        continue;
      }
      if(!['user','assistant'].includes(role)) continue;
      const content=typeof row.content==='string' ? row.content :
        Array.isArray(fragments) ? fragments.filter(x=>['REQUEST','RESPONSE'].includes(x.type)).map(x=>x.content).join('\n') :
        (row.content?.parts || []).filter(x=>typeof x==='string').join('\n');
      if(content.trim()) messages.push({role,content,...(row.demo===true?{demo:true}:{})});
    }
    if(!messages.length) return null;
    const rawTime=chat.time??chat.updated_at??chat.update_time;
    const ts=typeof rawTime==='number' ? (rawTime<1e11?rawTime*1000:rawTime) : Date.parse(rawTime);
    return {id:crypto.randomUUID(),title:String(chat.title || messages[0].content).slice(0,90),messages,time:Number.isFinite(ts)?ts:Date.now(),mode:chat.mode==='fast'?'fast':'economy'};
  }
  async function read(file) {
    if(file.size>64*1024*1024) fail('单个备份文件最大为 64 MB。');
    let data;
    if(/\.zip$/i.test(file.name)) data=await unpack(new Uint8Array(await file.arrayBuffer()));
    else data=JSON.parse(await file.text());
    const records=Array.isArray(data)?data:data?.conversations || [data];
    if(!Array.isArray(records) || records.length>10000) fail('备份中的会话数量异常。');
    const chats=records.map(parseChat).filter(Boolean);
    if(!chats.length) fail('没有找到可导入的对话正文。');
    return chats;
  }
  return {list,change,read};
})();

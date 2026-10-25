import {TurnPlayer,shouldSend} from './player.js?v=interruptible-preparation-v1';
const options=window.COMPANION_CHAT_OPTIONS||{};
const $=id=>document.getElementById(id), history=$('history');
let ws,active=null,seq=0,composing=false,rows=[],reconnectTimer,ready=false,revision=0,busyRetries=0,closing=false,clearing=false;
let ownsPlayback=false;
let connectionEpoch=0;
let dialogueBackend=null,latestUsage=null;
let inputRequest=null;
let inputStartupTimer=null;
function inputOpened(){clearTimeout(inputStartupTimer);inputStartupTimer=null;}
let voiceState='off',voiceAvailable=false;
function actionEvent(type,detail){if(typeof CustomEvent==='function')window.dispatchEvent(new CustomEvent(type,{detail}));}
function inputActivity(editing){actionEvent('companion:activity',{draft:!!$('message').value,editing,active:true});}
const voiceLabels={off:'点击语音对话后开启本机麦克风；停顿自动发送，播完继续听。',starting:'正在打开麦克风…',listening:'正在听 · 停顿约一秒自动发送',recognizing:'正在识别这句话…',replying:'正在回答 · 播放结束后继续听',error:'语音对话已停止'};
const voiceErrors={voice_busy:'麦克风已被另一个聊天窗口使用。',voice_unavailable:'请通过 N.E.K.O. 融合入口启动服务后使用语音。',voice_microphone_unavailable:'无法打开默认麦克风，请检查设备和 Windows 麦克风权限。',voice_capture_overrun:'音频采集出现中断，请重新开启语音。',voice_capture_format:'麦克风音频格式不符合要求。',voice_asr_timeout:'语音识别超时，已停止，未自动重试。',voice_asr_upstream:'语音识别服务暂不可用，已停止。',voice_asr_protocol:'识别服务返回异常，已停止。',voice_reply_failed:'本轮回答或播放未完成，语音会话已停止。',voice_reply_timeout:'本轮回答等待超时，语音会话已停止。',free_tier_exhausted:'供应商额度已耗尽，语音会话已停止。',account_arrearage:'供应商账户余额受限，语音会话已停止。',provider_rate_limited:'供应商限流，语音会话已停止。'};
function showVoice(state){
  actionEvent('companion:voice-state',{state});
  voiceState=state;
  if($('voice')){$('voice').disabled=!ready||!voiceAvailable;$('voice').textContent=['off','error'].includes(state)?'语音对话':'结束语音';$('voice').setAttribute?.('aria-pressed',String(!['off','error'].includes(state)));}
  if($('voice-status'))$('voice-status').textContent=voiceAvailable?(voiceLabels[state]||state):'语音对话需要 N.E.K.O. 融合服务。';
}
function stopVoice(){if(!['off','error'].includes(voiceState))command({type:'voice_stop'});showVoice('off');}
if($('voice'))$('voice').onclick=()=>{
  if(!ready||!voiceAvailable)return;
  if(!['off','error'].includes(voiceState)){stopVoice();silence();return;}
  closeInput();revision++;silence();$('error').textContent='';showVoice('starting');command({type:'voice_start'});
};
function closeInput(){inputOpened();if(inputRequest){command({type:'close_input'});inputRequest=null;inputActivity(false);}}
function openInput(){
  if(!options.nativeInput||inputRequest)return;
  if(!ready||closing||clearing){$('error').textContent='正在连接，请稍后再打开输入。';return;}
  const layout=options.inputLayout?.();
  if(!layout){$('error').textContent='输入区域暂不可用，请展开聊天后再试。';return;}
  inputRequest=crypto.randomUUID();$('error').textContent='';
  command({type:'open_input',requestId:inputRequest,text:$('message').value,layout});
  inputStartupTimer=setTimeout(()=>{closeInput();$('error').textContent='输入窗启动超时，请再次点击输入框重试。';},12000);
}
window.addEventListener('companion:input-request',openInput);
window.addEventListener('companion:input-close',closeInput);
const labels={idle:'就绪',generating:'正在组织回答',buffering:'正在准备语音',speaking:'正在说话',cancelled:'已停止',error:'出现故障'};
const turnLabels={...labels,completed:'已完成',interrupted:'已中断'};
function renderUsage(usage){
  latestUsage=usage;
  const node=$('usage');if(!node)return;
  if(usage.mode==='simulation'){node.textContent='模拟模式 · 不调用云服务';return;}
  if(!usage.reserved||!usage.limits){node.textContent='今日用量暂不可读取；云调用会按本地限制检查。';return;}
  const r=usage.reserved,l=usage.limits;
  const count=key=>l[key]===null?`${r[key]}（不限额）`:`${r[key]}/${l[key]}`;
  node.textContent=`今日文字 ${count('chat_calls')} 次 · 语音 ${count('tts_calls')} 次 · 估计语音 ${count('tts_estimated_characters')} 字。北京时间按日统计；失败和取消也计入尝试，非账单或余额。${dialogueBackend==='neko'?'N.E.K.O. 前台对话和后台记忆共用文字请求统计。':''}`;
  if(usage.voiceReserved)node.textContent+=` 语音识别 ${usage.voiceReserved.asr_calls} 次，输入约 ${Math.round(usage.voiceReserved.audio_milliseconds/1000)} 秒。`;
}
function showBackend(node,backend){
  if(dialogueBackend!==backend){dialogueBackend=backend;if(latestUsage)renderUsage(latestUsage);}
  if(backend!=='neko')return;
  if(node)node.textContent='N.E.K.O. 对话 · 温柔女声';
  const note=$('clear-confirm');
  if(note?.firstChild)note.firstChild.textContent='清除当前页面记录？N.E.K.O. 对话和记忆仍会保留。';
  const footer=document.querySelector('footer');
  if(footer)footer.textContent='对话与记忆保存在此电脑；生成回复与语音需要发送至云服务。刷新不会重播。清除页面记录不删除 N.E.K.O. 记忆。';
}
function command(msg){if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify({v:1,...msg}));}
const player=new TurnPlayer($('player'),(event,phase)=>{
  command({type:'played',turnId:event.turnId,segmentId:event.segmentId,phase});
  if(phase==='ended'){const row=rows.find(r=>r.turnId===event.turnId);if(row){row.playedText=(row.playedText||'')+event.text;render();}}
  $('subtitle').textContent=phase==='started'?event.text:'';
},message=>{const old=active;active=null;command({type:'cancel',turnId:old});const row=rows.find(r=>r.turnId===old);if(row){row.status='interrupted';render();}$('error').textContent=message;},
(event,audio)=>window.companionActions?.prepareSpeech(event,audio),()=>window.companionActions?.finishSpeech(),
(event,audio)=>window.companionActions?.speechStarted(event,audio));
function render(){
  const follow=history.scrollHeight-history.scrollTop-history.clientHeight<45||!history.children.length;
  history.replaceChildren();
  for(const r of rows){
    const el=document.createElement('article');el.className='turn';el.dataset.turn=r.turnId;
    for(const [cls,text] of [['user',r.userText],['reply',r.generatedText||'…'],['meta',`${turnLabels[r.status]||r.status} · 已播完 ${r.playedText?.length||0} 字对应的语音`]]){
      const p=document.createElement('div');p.className=cls;p.textContent=text;el.append(p);
    }
    if(r.generatedText && ['error','completed','interrupted'].includes(r.status)){
      const b=document.createElement('button');b.type='button';b.className='retry';b.textContent='仅重试语音';
      b.onclick=()=>start({type:'retry_audio',sourceTurnId:r.turnId},'[仅重试语音]',r.generatedText);el.append(b);
    }
    history.append(el);
  }
  if(follow)history.scrollTop=history.scrollHeight;
}
  function silence(){const old=active;player.stop();actionEvent('companion:reply-state',{state:'cancelled'});active=null;$('subtitle').textContent='';if(old)command({type:'cancel',turnId:old});return old;}
function start(msg,user,text=''){
  if(!ready||clearing||ws?.readyState!==WebSocket.OPEN){$('error').textContent='尚未连接或正在清除记录，请稍后再试。';return;}
  closeInput();
  stopVoice();
  revision++;
  const old=silence();
  const previous=rows.find(r=>r.turnId===old);if(previous&&!['completed','error'].includes(previous.status))previous.status='interrupted';
  active=crypto.randomUUID();player.begin(active);$('error').textContent='';
  actionEvent('companion:reply-state',{state:'generating',turnId:active});
  rows.push({turnId:active,userText:user,generatedText:text,playedText:'',status:'generating'});render();
  command({...msg,turnId:active});
}
function send(){const text=$('message').value.trim();if(!text){openInput();return;}start({type:'send',text,fault:$('fault')?.checked?'tts':'none'},text);if(active){$('message').value='';window.dispatchEvent?.(new Event('companion:draft-change'));}}
$('composer').onsubmit=e=>{e.preventDefault();if(!composing)send();};
$('message').addEventListener('input',()=>inputActivity(false));
window.addEventListener('companion:draft-change',()=>inputActivity(false));
$('message').addEventListener('compositionstart',()=>{composing=true;inputActivity(true);});
$('message').addEventListener('compositionend',()=>{composing=false;inputActivity(false);});
$('message').addEventListener('keydown',e=>{if(shouldSend(e,composing)){e.preventDefault();send();}});
$('stop').onclick=()=>{stopVoice();const old=silence();const r=rows.find(r=>r.turnId===old);if(r)r.status='interrupted';$('status').textContent='已停止';render();};
$('clear').onclick=()=>{if(!ready)return;$('clear-confirm').hidden=false;};
$('clear-no').onclick=()=>{$('clear-confirm').hidden=true;};
$('clear-yes').onclick=()=>{if(!ready)return;closeInput();revision++;silence();command({type:'clear_history'});$('clear-confirm').hidden=true;};
async function connect(){
  if(closing)return;
  const epoch=++connectionEpoch;
  ready=false;$('send').disabled=true;
  try {
    const info=options.endpoint?{mode:'cloud'}:await (await fetch('/api/bootstrap')).json();
    if(closing||epoch!==connectionEpoch)return;
    if(window.dispatchEvent)window.dispatchEvent(new Event('companion:ready'));
    const modeNode=$('mode-info')||document.querySelector('header p:last-child');
    if(modeNode)modeNode.textContent=info.cloudBlock==='account_arrearage'?'百炼账号存在余额相关限制，云调用已暂停。':info.cloudBlock==='free_tier_exhausted'?'免费额度已耗尽，云调用已暂停。':info.mode==='cloud'?'文字交流 · 温柔女声':'模拟文字与测试提示音';
    showBackend(modeNode,info.dialogueBackend);
    seq=0; const socket=new WebSocket(options.endpoint||`ws://${location.host}/ws?shared=1`);ws=socket;
    const current=()=>epoch===connectionEpoch&&socket===ws&&!closing;
    socket.onopen=()=>{if(current())$('status').textContent='正在恢复记录…';};
    socket.onmessage=event=>{
      if(!current())return;
      const m=JSON.parse(event.data);if(m.v!==1||m.seq<=seq)return;seq=m.seq;
      if(m.type==='native_input_activity'&&m.requestId===inputRequest){inputOpened();if(!m.opened)actionEvent('companion:activity',{editing:true,draft:m.hasText===true});return;}
      if(m.type==='state')actionEvent('companion:reply-state',{state:m.state,turnId:m.turnId});
      if(m.type==='native_input_result'){
        if(!inputRequest||m.requestId!==inputRequest)return;inputOpened();inputRequest=null;inputActivity(false);
        if(m.action==='send'||m.action==='draft'||m.action==='stop'){
          if(typeof m.text!=='string'||m.text.length>2000)return;
          $('message').value=m.text;
          window.dispatchEvent?.(new Event('companion:draft-change'));
          if(m.action==='send')send();
          if(m.action==='stop')$('stop').onclick();
        }else $('error').textContent=m.action==='busy'?'已有输入窗打开，请先完成或关闭它。':'输入窗未能打开，请使用项目交流页输入。';
        return;
      }
      if(m.usage)renderUsage(m.usage);
      if(m.type==='voice_state'){showVoice(m.voiceState);if(m.code)$('error').textContent=voiceErrors[m.code]||'语音会话已停止，请检查服务后重试。';return;}
      if(m.type==='voice_turn'&&ownsPlayback){revision++;player.stop();active=m.turnId;player.begin(active);$('error').textContent='';rows.push({turnId:active,userText:m.userText,generatedText:'',playedText:'',status:'generating'});render();return;}
      if(m.type==='control_lost'){closeInput();actionEvent('companion:reply-state',{state:'cancelled'});showVoice('off');player.stop();active=null;ownsPlayback=false;$('subtitle').textContent='';$('status').textContent='已切换到另一处聊天';return;}
      if(m.type==='control_acquired'){ownsPlayback=true;return;}
      if(m.type==='history_cleared'){player.stop();active=null;rows=[];$('subtitle').textContent='';render();return;}
      if(m.type==='snapshot'){
        if(m.requestRevision!==undefined&&m.requestRevision!==revision)return;
        // History synchronization must never cancel a newer local turn.
        const local=rows.find(r=>r.turnId===active);
        rows=m.history.filter(r=>r.turnId!==active);if(local)rows.push(local);
        ownsPlayback=m.ownsPlayback!==false;ready=true;busyRetries=0;$('send').disabled=false;render();
        voiceAvailable=m.voiceAvailable===true;showVoice(m.voiceState||'off');
        if(options.endpoint&&modeNode)modeNode.textContent=m.testMode?'模拟文字与测试提示音':'文字交流 · 温柔女声';
        showBackend(modeNode,m.dialogueBackend);
        if(!active)$('status').textContent=labels[m.state]||m.state;return;
      }
      if(m.type==='turn_started'&&!rows.some(r=>r.turnId===m.turnId)){rows.push({turnId:m.turnId,userText:m.userText||'[语音重试]',generatedText:'',playedText:'',status:'generating'});render();}
      const row=rows.find(r=>r.turnId===m.turnId);
      if(m.type==='text_delta'&&row){row.generatedText=m.text;if(ownsPlayback)actionEvent('companion:reply-progress',{chars:(m.text||'').length,turnId:m.turnId});render();}
      if(m.type==='audio_segment'&&ownsPlayback&&m.turnId===active)player.enqueue(m);
      if(m.type==='synthesis_done'&&ownsPlayback&&m.turnId===active)player.finish(m.turnId);
      if(m.type==='state'){$('status').textContent=labels[m.state]||m.state;$('subtitle').textContent=m.subtitle||'';if(row){row.status=m.state;render();}}
      if(m.type==='error'){$('error').textContent=m.message+(m.code?`（错误码：${m.code}）`:'');if(row){row.status='error';render();}}
      if(m.type==='cancelled'&&row){row.status='interrupted';render();}
      if(m.type==='turn_done'){if(row)row.status='completed';if(m.turnId===active){active=null;player.stop();}command({type:'refresh',requestRevision:revision});}
    };
    socket.onclose=e=>{if(!current())return;inputOpened();inputRequest=null;inputActivity(false);actionEvent('companion:reply-state',{state:'error'});ready=false;showVoice('off');player.stop();active=null;ownsPlayback=false;$('send').disabled=true;$('subtitle').textContent='';$('status').textContent=e.code===4009?'聊天窗口数量已满，请关闭多余窗口。':e.code===1008?'本地会话连接已失效，请重新打开窗口。':'连接中断；文字保留，恢复后不会自动重播。';
      if(e.code===4009){if(busyRetries++<4)reconnectTimer=setTimeout(connect,1500);else $('status').textContent='播放窗口仍被占用，请关闭旧窗口后刷新。';}
      else if(e.code!==1008)reconnectTimer=setTimeout(connect,1500);};
    socket.onerror=()=>{if(current())$('error').textContent='本地连接失败，请检查服务是否启动。';};
  }catch{if(closing||epoch!==connectionEpoch)return;$('status').textContent='服务不可用，正在重连…';reconnectTimer=setTimeout(connect,1500);}
}
window.addEventListener('pagehide',()=>{stopVoice();closeInput();closing=true;connectionEpoch++;clearTimeout(reconnectTimer);player.stop();ws?.close();});
window.addEventListener('pageshow',e=>{if(e.persisted){closing=false;connect();}});
window.addEventListener('companion:suspend',()=>{stopVoice();closeInput();closing=true;connectionEpoch++;ready=false;clearTimeout(reconnectTimer);silence();ws?.close();$('send').disabled=true;$('status').textContent='已暂停';});
window.addEventListener('companion:resume',()=>{if(closing){closing=false;connect();}});
connect();

// The same dialogue module owns chat, memory, voice and playback in every host.
window.COMPANION_CHAT_OPTIONS=Object.freeze({nativeInput:false});
window.COMPANION_ACTION_OPTIONS=Object.freeze({mediaBase:'/standalone-assets/actions/',externalStyle:true,externalVisibility:true});
window.purpleActionHost=Object.freeze({claim(){},release(){
  document.getElementById('stage-status').textContent='角色画面不可用，请检查独立素材后重新打开。';
}});
const audio=document.getElementById('player'),mute=document.getElementById('mute');
mute.onclick=()=>{audio.muted=!audio.muted;mute.textContent=audio.muted?'取消静音':'静音';mute.setAttribute('aria-pressed',String(audio.muted));};
document.getElementById('volume').oninput=e=>{audio.volume=Number(e.target.value);};
// Host events are also used by ordinary browsers; duplicate transitions are ignored.
let suspended=false,hostSuspended=false,loaded=false;
const desktop=window.chrome?.webview && new URLSearchParams(location.search).get('desktop')==='1';
if(desktop){
  document.body.classList.add('desktop-wallpaper');
  const launcher=document.createElement('button');launcher.id='desktop-chat-launcher';
  launcher.textContent='和知夏聊聊';launcher.title='Ctrl + Alt + C';
  launcher.onclick=()=>window.chrome.webview.postMessage('open-chat');document.body.append(launcher);
  const overlay=document.createElement('p');overlay.id='desktop-subtitle';overlay.setAttribute('aria-live','polite');document.body.append(overlay);
  new MutationObserver(()=>{overlay.textContent=document.getElementById('subtitle').textContent;}).observe(document.getElementById('subtitle'),{childList:true,subtree:true,characterData:true});
  window.companionDesktop=Object.freeze({
    snapshot(){return {
      turns:[...document.querySelectorAll('#history .turn')].map(row=>({user:row.querySelector('.user')?.textContent||'',reply:row.querySelector('.reply')?.textContent||'',meta:row.querySelector('.meta')?.textContent||''})),
      history:[...document.querySelectorAll('#history .turn')].map(row=>[row.querySelector('.user')?.textContent,row.querySelector('.reply')?.textContent,row.querySelector('.meta')?.textContent].filter(Boolean).join('\n')).join('\n\n'),
      status:document.getElementById('error').textContent||document.getElementById('status').textContent,
      usage:document.getElementById('usage').textContent,
      ready:!document.getElementById('send').disabled,
      voiceReady:!document.getElementById('voice').disabled,
      voiceLabel:document.getElementById('voice').textContent,
      muted:audio.muted
    };},
    command(command,value){
      const input=document.getElementById('message');
      if(command==='send'&&document.getElementById('send').disabled)return false;
      if(command==='draft'||command==='send'){
        input.value=String(value||'').slice(0,2000);input.dispatchEvent(new Event('input'));
        if(command==='send')document.getElementById('composer').dispatchEvent(new Event('submit',{cancelable:true}));
        if(command==='send')return input.value==='';
      }else if(['stop','voice','mute'].includes(command))document.getElementById(command).click();
      else if(command==='stopVoice'&&document.getElementById('voice').getAttribute('aria-pressed')==='true')document.getElementById('voice').click();
      else if(command==='volume'){
        const slider=document.getElementById('volume');slider.value=String(Math.max(0,Math.min(1,Number(value))));slider.dispatchEvent(new Event('input'));
      }else if(command==='retry')document.querySelector('#history .turn:last-child .retry')?.click();
      return true;
    },
    placeLauncher(x,y,width,height){Object.assign(launcher.style,{left:x+'px',top:y+'px',width:width+'px',height:height+'px'});}
  });
}
function updateSuspended(){
  const value=hostSuspended||(!desktop&&document.hidden);
  if(!loaded)return;
  if(suspended===value)return;
  suspended=value;
  window.dispatchEvent(new CustomEvent('purple:host-state',{detail:{paused:value}}));
  window.dispatchEvent(new Event(value?'companion:suspend':'companion:resume'));
}
function setSuspended(value){hostSuspended=value===true;updateSuspended();}
window.companionHost=Object.freeze({setSuspended,get suspended(){return suspended;}});
document.addEventListener('visibilitychange',updateSuspended);
window.addEventListener('companion:action-error',()=>{document.getElementById('error').textContent='角色动作加载失败，请检查独立素材后重新打开。';});
// Load actions before dialogue so the first audio waits for its introduction.
await import('/wallpaper/action-player.js?v=interruptible-preparation-v1');
await import('/web/app.js?v=interruptible-preparation-v1');
loaded=true;updateSuspended();

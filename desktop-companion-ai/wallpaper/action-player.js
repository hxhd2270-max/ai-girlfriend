// Six-clip companion with one speech introduction and a continuous loop. Source audio is never used for dynamic speech.
(() => {
  'use strict';
  if (window.companionActions) return;
  const stage = document.getElementById('stage');
  if (!stage || !window.purpleActionHost) return;
  const options=window.COMPANION_ACTION_OPTIONS||{};
  const mediaBase=options.mediaBase||'http://127.0.0.1:8765/purple/actions/';
  if(!options.externalStyle){
  const style = document.createElement('style');
  style.textContent = '.action-surface{position:absolute;inset:0;z-index:5;pointer-events:none;background:#111}.action-surface video{position:absolute;inset:0;width:100%;height:100%;object-fit:var(--fit,cover);filter:brightness(var(--brightness,1));opacity:0;transition:opacity .16s linear}body.has-action-player #film,body.has-action-player #filmNext{visibility:hidden}body.has-action-player .now-playing p{font-size:0}body.has-action-player .now-playing p:after{content:attr(data-action);font-size:11px}body.has-action-player #playButton,body.has-action-player #modeButton,body.has-action-player #momentButton{visibility:hidden}';
  document.head.append(style);
  }
  const surface = document.createElement('div'); surface.className = 'action-surface';
  const decks = [document.createElement('video'), document.createElement('video')];
  // Keep speaking media decoded on separate decks; switches do not reload it.
  const speechDecks = [document.createElement('video'), document.createElement('video')];
  const allDecks=[...decks,...speechDecks];
  allDecks.forEach(v => { v.muted=true; v.playsInline=true; v.preload='auto'; v.crossOrigin='anonymous'; surface.append(v); });
  speechDecks.forEach(v=>{v.src=mediaBase+'speaking.webm?v=dialogue-v002';v.loop=true;v.load();});
  stage.append(surface);
  const names={reading:'安静阅读','to-listening':'放下书，听你说',listening:'我在听','to-speaking':'准备回答',speaking:'正在回答','to-reading':'继续阅读'};
  let front=null, state=null, pending=null, version=0, failed=false, paused=false;
  let engaged=false, busy=false, voice=false, draft=false, editing=false, talking=false, lastActivity=Date.now();
  let speechEpoch=0, speechPrep=null;
  let speechAudio=null, speechEvent=null, speechTurn=null, fallbackRunning=false;
  let preparingTurn=null;
  const thinkingAudio=document.createElement('audio');
  thinkingAudio.src=mediaBase+'thinking-voice.wav?v=thinking-v001';
  thinkingAudio.preload='auto';
  thinkingAudio.id='companion-thinking-audio';
  document.body.append(thinkingAudio);
  let waiting=false, thinkingPlayed=false, thinkingTimer=null, clipTimer=null;
  let thinkingEpoch=0, clipEpoch=0, clipActive=false, shortClip=false, repeats=0, deferred=false;
  let waitStarted=null, bufferStarted=null, outputChars=0;
  const waitHistory=[];
  const clamp=(x,lo,hi)=>Math.max(lo,Math.min(hi,x));
  const mean=values=>values.reduce((sum,x)=>sum+x,0)/values.length;
  function remainingWait(){
    if(waitStarted===null)return 0;
    let estimate=waitHistory.length?mean(waitHistory.map(x=>x.total)):12000;
    if(bufferStarted!==null){
      const samples=waitHistory.filter(x=>x.chars>0&&x.buffer>0);
      const tts=outputChars>0?clamp(outputChars*(samples.length?mean(samples.map(x=>x.buffer/x.chars)):90),1800,30000):5000;
      estimate=bufferStarted-waitStarted+tts;
    }
    const remaining=Math.max(0,estimate-(Date.now()-waitStarted));
    document.body.dataset.thinkingEstimateMs=String(Math.round(remaining));
    return remaining;
  }
  function beginThinking(){
    if(waiting)return;
    waiting=true;waitStarted=Date.now();bufferStarted=null;outputChars=0;repeats=0;deferred=false;
  }
  function stopThinking(){
    thinkingEpoch++;clipEpoch++;clipActive=false;
    clearTimeout(thinkingTimer);clearTimeout(clipTimer);thinkingTimer=null;clipTimer=null;
    waiting=false;thinkingPlayed=false;waitStarted=null;bufferStarted=null;
    thinkingAudio.pause();thinkingAudio.currentTime=0;
    document.body.dataset.thinkingAudio='stopped';
    delete document.body.dataset.thinkingEstimateMs;
  }
  function canThink(){return waiting&&state==='thinking'&&!paused&&!failed;}
  function scheduleHum(){
    if(!canThink()||repeats>=2||Date.now()-waitStarted>=45000)return;
    const epoch=thinkingEpoch;
    const gap=3000;
    thinkingTimer=setTimeout(()=>{
      thinkingTimer=null;
      if(epoch!==thinkingEpoch||!canThink()||Date.now()-waitStarted>=45000)return;
      const remaining=remainingWait();
      if(!deferred&&remaining>0&&remaining<1800){
        deferred=true;
        thinkingTimer=setTimeout(()=>{thinkingTimer=null;if(epoch===thinkingEpoch&&canThink()&&Date.now()-waitStarted<45000)playThinking(true);},2000);
      }else playThinking(true);
    },gap);
  }
  function finishThinkingClip(){
    if(!clipActive)return;
    clipActive=false;clearTimeout(clipTimer);clipTimer=null;
    thinkingAudio.pause();document.body.dataset.thinkingAudio='quiet';
    scheduleHum();
  }
  function playThinking(short=false){
    if(!canThink())return;
    const epoch=thinkingEpoch, clip=++clipEpoch;
    shortClip=short;clipActive=true;
    if(short)repeats++;
    // Reuse only the opening hum; exclude the later spoken phrase.
    thinkingAudio.currentTime=short?.15:0;
    const dialogueAudio=document.getElementById('player');
    thinkingAudio.muted=dialogueAudio?.muted===true;
    thinkingAudio.volume=clamp(dialogueAudio?.volume??1,0,1);
    document.body.dataset.thinkingAudio='starting';
    void thinkingAudio.play().then(()=>{
      if(epoch!==thinkingEpoch||clip!==clipEpoch||!clipActive)return;
      document.body.dataset.thinkingAudio=short?'humming':'playing';
      if(short)clipTimer=setTimeout(()=>{if(epoch===thinkingEpoch&&clip===clipEpoch)finishThinkingClip();},630);
    }).catch(error=>{
      if(epoch!==thinkingEpoch||clip!==clipEpoch||!canThink())return;
      clipActive=false;
      document.body.dataset.thinkingAudio=error?.name||'playback-error';
      window.dispatchEvent(new CustomEvent('companion:thinking-audio-error',{detail:{code:error?.name||'playback-error'}}));
      const hint=document.getElementById('error');
      if(hint)hint.textContent='思考提示音未能播放，请点击聊天区域后重新发送。正式回答不受影响。';
    });
  }
  thinkingAudio.addEventListener('ended',()=>{if(thinkingAudio.ended)finishThinkingClip();});
  thinkingAudio.addEventListener('timeupdate',()=>{if(clipActive&&shortClip&&thinkingAudio.currentTime>=.78)finishThinkingClip();});
  function think(){
    if(waiting&&!talking&&!paused&&!failed&&(state==='listening'||state==='thinking')&&!pending)void show('thinking');
  }
  const idleMs=3000;
  const delay=ms=>new Promise(r=>setTimeout(r,ms));
  function activity(){engaged=true;lastActivity=Date.now(); if(state==='reading'||state==='to-reading'||pending?.state==='to-reading')void show('to-listening');}
  function setBusy(value){if(busy&&!value)lastActivity=Date.now();busy=value;}
  function waitEvent(v, event, check, timeout=4000){
    if(check())return Promise.resolve();
    return new Promise((resolve,reject)=>{
      const done=()=>{if(check()){clean();resolve();}}, bad=()=>{clean();reject(new Error('video-unavailable'));};
      const timer=setTimeout(bad,timeout);
      const clean=()=>{clearTimeout(timer);v.removeEventListener(event,done);v.removeEventListener('error',bad);};
      v.addEventListener(event,done);v.addEventListener('error',bad);
    });
  }
  function fail(){
    stopThinking();
    failed=true;version++;pending=null;talking=false;
    allDecks.forEach(v=>{v.pause();v.muted=true;});
    // Hold the last visible frame. If initial loading failed, reveal the old still.
    if(!front){surface.hidden=true;document.body.classList.remove('has-action-player');window.purpleActionHost.release();}
    window.dispatchEvent(new CustomEvent('companion:action-error',{detail:{code:'video-unavailable'}}));
    const error=document.getElementById('error');if(error)error.textContent='动作视频加载失败，已暂停画面；请检查本地服务后重载壁纸。';
  }
  async function show(next){
    if(failed || paused)return;
    if(pending?.state===next)return pending.promise;
    if(state===next&&!pending)return;
    const id=++version, incoming=decks.find(v=>v!==front)||decks[0];
    const job={state:next,promise:null};pending=job;
    job.promise=(async()=>{
      try{
        incoming.pause();incoming.muted=true;incoming.style.opacity='0';
        incoming.src=mediaBase+next+'.webm?v='+ (next==='thinking'?'thinking-v001':'dialogue-v002');
        incoming.loop=['reading','listening','thinking'].includes(next);incoming.load();
        await waitEvent(incoming,'loadeddata',()=>incoming.readyState>=2);
        if(id!==version || paused)return;
        await incoming.play();
        if(id!==version || paused){incoming.pause();return;}
        // A decoded frame is ready before making this surface visible.
        if(incoming.requestVideoFrameCallback)await Promise.race([new Promise(r=>incoming.requestVideoFrameCallback(r)),delay(250)]);
        if(id!==version || paused)return;
        const old=front;front=incoming;state=next;pending=null;
        surface.hidden=false;incoming.style.opacity='1';
        if(old&&old!==incoming){old.muted=true;old.style.opacity='0';setTimeout(()=>{if(front!==old&&!pending)old.pause();},180);}
        document.body.dataset.companionAction=next;
        document.querySelector('.now-playing p')?.setAttribute('data-action',next==='thinking'?'让我想一下':names[next]);
        if(next==='thinking'&&waiting&&!thinkingPlayed){
          thinkingPlayed=true;
          document.body.dataset.thinkingAudio='scheduled';
          const epoch=thinkingEpoch;
          thinkingTimer=setTimeout(()=>{thinkingTimer=null;if(epoch===thinkingEpoch)playThinking();},1000);
        }
        window.dispatchEvent(new CustomEvent('companion:action-state',{detail:{state:next}}));
        if(next==='reading'&&engaged)void show('to-listening');
        if(next==='listening'&&!engaged)void show('to-reading');
      }catch{if(id===version)fail();}
    })();
    return job.promise;
  }
  decks.forEach(v=>v.addEventListener('ended',()=>{
    if(v!==front||paused||pending)return;
    if(state==='to-listening')void show('listening');
    else if(state==='to-reading')void show(engaged?'to-listening':'reading');
  }));
  async function seek(v,time){
    if(Math.abs(v.currentTime-time)<.018&&v.readyState>=2)return;
    v.currentTime=time;
    await waitEvent(v,'seeked',()=>!v.seeking&&v.readyState>=2);
  }
  const speechStart=4/24; // First clearly open mouth in shot02: frame 4.
  async function enterSpeech(epoch, restart){
    const incoming=restart?(speechDecks.find(v=>v!==front)||speechDecks[0]):front;
    incoming.pause();
    await waitEvent(incoming,'loadeddata',()=>incoming.readyState>=2);
    if(epoch!==speechEpoch||paused||!talking)return;
    if(restart)await seek(incoming,speechStart);
    if(epoch!==speechEpoch||paused||!talking)return;
    version++;pending=null;
    const old=front;front=incoming;state='speaking';
    incoming.playbackRate=1;incoming.loop=true;incoming.style.opacity='1';surface.hidden=false;
    if(old&&old!==incoming){old.pause();old.style.opacity='0';}
    document.body.dataset.companionAction='speaking';
    document.body.dataset.speechSegment='continuous-loop';
    document.querySelector('.now-playing p')?.setAttribute('data-action',names.speaking);
    window.dispatchEvent(new CustomEvent('companion:action-state',{detail:{state:'speaking',segment:'continuous-loop'}}));
  }
  function beginSpeechPreparation(turn){
    if(turn==null||talking||preparingTurn===turn||failed||paused)return;
    preparingTurn=turn;stopThinking();activity();
    // Speaking decks are already loaded. Seek them while model/TTS work is in
    // flight so audio readiness has no decode/seek work on its critical path.
    for(const deck of speechDecks)void (async()=>{
      try{await waitEvent(deck,'loadeddata',()=>deck.readyState>=2);if(!talking&&preparingTurn===turn)await seek(deck,speechStart);}
      catch{}
    })();
    // The introduction fills model/TTS latency. It is scenery, never an audio gate.
    const start=async()=>{
      const deadline=Date.now()+18000;
      while(!failed&&!paused&&preparingTurn===turn&&(state==='reading'||state==='to-listening'||state==='to-reading'||pending)){
        if(Date.now()>deadline)return;
        await delay(30);
      }
      if(!failed&&!paused&&!talking&&preparingTurn===turn&&state!=='to-speaking')await show('to-speaking');
    };
    void start();
  }
  function finishSpeech(){
    if(talking)lastActivity=Date.now();
    speechEpoch++;talking=false;preparingTurn=null;speechPrep=null;speechAudio=null;speechEvent=null;speechTurn=null;fallbackRunning=false;
    speechDecks.forEach(v=>v.pause());
    delete document.body.dataset.speechSegment;
    // Completion/cancellation interrupts the introduction as well as the loop.
    // Never seek the old material's 11.34-second smile or replay an outro.
    if(state==='speaking'||state==='to-speaking'||pending?.state==='to-speaking')void show('listening');
  }
  async function prepareSpeech(event=null,audio=null){
    if(waitStarted!==null&&waiting){
      waitHistory.push({total:Date.now()-waitStarted,buffer:bufferStarted===null?0:Date.now()-bufferStarted,chars:outputChars});
      if(waitHistory.length>8)waitHistory.shift();
    }
    stopThinking();
    if(failed||paused)throw new Error('video-unavailable');
    if(speechPrep&&speechAudio===audio&&speechEvent===event)return speechPrep;
    const continuing=talking&&state==='speaking'&&speechAudio===audio&&event?.turnId!=null&&speechTurn===event.turnId;
    const epoch=++speechEpoch;talking=true;activity();
    speechAudio=audio;speechEvent=event;speechTurn=event?.turnId??null;fallbackRunning=false;
    speechPrep=(async()=>{
      try{
        if(!continuing){
          // Audio readiness interrupts every visual transition. Speaking media
          // is preloaded and becomes the next visible decoded frame directly.
          if(failed)throw new Error('video-unavailable');
          if(paused||epoch!==speechEpoch)return;
        }
        if(failed)throw new Error('video-unavailable');
        if(paused||epoch!==speechEpoch)return;
        await enterSpeech(epoch,!continuing);
        // Browser playback owns the start signal when an audio element exists.
        // State-only bridges have no audio owner and may run immediately.
        if(epoch===speechEpoch)fallbackRunning=speechAudio===null;
      }catch(error){if(epoch===speechEpoch)fail();throw error;}
    })();
    return speechPrep;
  }
  function updateSpeech(){
    if(state!=='speaking'||!talking||!speechDecks.includes(front))return;
    const running=speechAudio?!speechAudio.paused&&!speechAudio.ended:fallbackRunning;
    if(!running)front.pause();
    else if(front.paused)void front.play().catch(()=>fail());
    // Native looping, normal speed, no PCM-driven pose changes or seek chasing.
  }
  function speechStarted(event,audio){
    if(failed||paused||!talking||state!=='speaking'||speechAudio!==audio||speechTurn!==event?.turnId)return false;
    fallbackRunning=true;
    if(front&&speechDecks.includes(front)&&front.paused)void front.play().catch(()=>fail());
    return true;
  }
  window.companionActions=Object.freeze({prepareSpeech,finishSpeech,beginSpeechPreparation,speechStarted,get state(){return state;},
    config:{mode:'dialogue-continuous-v002',idleMs,speechStart,introduction:'to-speaking',outro:'listening'}});
  window.addEventListener('companion:activity',e=>{
    const d=e.detail||{};
    if(typeof d.draft==='boolean')draft=d.draft;
    if(typeof d.editing==='boolean')editing=d.editing;
    if(d.active!==false)activity();
  });
  window.addEventListener('companion:voice-state',e=>{const next=!['off','error'].includes(e.detail?.state);if(voice&&!next)lastActivity=Date.now();voice=next;if(voice)activity();});
  window.addEventListener('companion:reply-state',e=>{
    const reply=e.detail?.state;
    if(['generating','buffering'].includes(reply)&&!talking){
      beginThinking();
      if(reply==='buffering'&&bufferStarted===null)bufferStarted=Date.now();
      remainingWait();activity();think();
      if(reply==='generating')beginSpeechPreparation(e.detail?.turnId);
    }
    if(['idle','cancelled','error'].includes(reply)){
      stopThinking();
      if(state==='thinking'||pending?.state==='thinking')void show('listening');
    }
    setBusy(['generating','buffering','speaking'].includes(e.detail?.state));
    if(busy)activity();
    if(['idle','cancelled','error'].includes(e.detail?.state))finishSpeech();
  });
  window.addEventListener('companion:reply-progress',e=>{
    if(waiting){outputChars=Math.max(outputChars,Number(e.detail?.chars)||0);remainingWait();}
    if((Number(e.detail?.chars)||0)>0)beginSpeechPreparation(e.detail?.turnId);
  });
  window.addEventListener('purple:ai-state',e=>{
    // The local audio owner controls completion. A delayed state-only packet
    // must not tear down its plan and restart a fallback from the opening.
    if(speechAudio)return;
    if(e.detail?.active&&!e.detail?.speaking&&!talking){beginThinking();activity();think();}
    if(!e.detail?.active){stopThinking();if(state==='thinking'||pending?.state==='thinking')void show('listening');}
    setBusy(e.detail?.active===true);
    if(e.detail?.active)activity();
    // State bridge supports replies played from the browser; local playback
    // instead uses prepareSpeech before starting audio.
    if(e.detail?.speaking&&!talking)void prepareSpeech().catch(()=>{});
    if(!e.detail?.active&&talking)finishSpeech();
  });
  function suspend(value){
    if(paused===value)return;
    paused=value;if(paused){stopThinking();version++;pending=null;allDecks.forEach(v=>v.pause());finishSpeech();}
    else if(!failed){state=null;void show(engaged?'listening':'reading');}
  }
  window.addEventListener('purple:host-state',e=>suspend(e.detail?.paused===true));
  window.addEventListener('pagehide',()=>suspend(true));
  window.addEventListener('pageshow',e=>{if(e.persisted)suspend(false);});
  if(!options.externalVisibility)document.addEventListener('visibilitychange',()=>suspend(document.hidden));
  setInterval(()=>{
    if(paused||failed)return;
    think();
    if(talking&&!speechPrep)return;
    updateSpeech();
    if(engaged&&!busy&&!talking&&!voice&&!draft&&!editing&&Date.now()-lastActivity>=idleMs){engaged=false;if(state==='listening')void show('to-reading');}
    // All delivered source tracks stay muted: only the dialogue player owns audio.
    // Unmuting an autoplay video can also pause it in Chromium.
    if(front)front.muted=true;
  },40);
  window.purpleActionHost.claim();document.body.classList.add('has-action-player');
  void show('reading');
})();

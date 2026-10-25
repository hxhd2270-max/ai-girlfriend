// PCM analysis stays local and never changes the audio or dialogue text.
export function speechPlan(bytes) {
  try {
    const v=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
    const tag=p=>String.fromCharCode(...bytes.subarray(p,p+4));
    if(tag(0)!=='RIFF'||tag(8)!=='WAVE')return null;
    let format,offset=0,size=0;
    for(let p=12;p+8<=bytes.length;){
      const n=v.getUint32(p+4,true),end=p+8+n;if(end>bytes.length)return null;
      if(tag(p)==='fmt '&&n>=16)format={codec:v.getUint16(p+8,true),channels:v.getUint16(p+10,true),rate:v.getUint32(p+12,true),bits:v.getUint16(p+22,true)};
      if(tag(p)==='data'){offset=p+8;size=n;}
      p=end+(n%2);
    }
    if(!format||format.codec!==1||format.bits!==16||format.channels<1||format.channels>2||format.rate<8000||format.rate>96000||!size)return null;
    const stride=2*format.channels,frames=Math.floor(size/stride),duration=frames/format.rate;
    if(duration>31)return null;
    const block=Math.max(1,Math.round(format.rate*.02)),energy=[];
    for(let i=0;i<frames;i+=block){
      let sum=0,count=0;
      for(let j=i;j<Math.min(i+block,frames);j++)for(let c=0;c<format.channels;c++){const x=v.getInt16(offset+j*stride+c*2,true)/32768;sum+=x*x;count++;}
      energy.push(Math.sqrt(sum/count));
    }
    const peak=Math.max(...energy),threshold=Math.max(.0015,Math.min(.012,peak*.035));
    const voiced=[];
    for(let i=0;i<energy.length;i++)if(energy[i]>=threshold){
      const start=Math.max(0,i*block/format.rate-.04),end=Math.min(duration,(i+1)*block/format.rate+.06);
      // Word and ordinary sentence gaps must not restart a full-body gesture.
      const last=voiced.at(-1);if(last&&start-last.end<.90)last.end=end;else voiced.push({start,end});
    }
    // A short isolated syllable between pauses is not a useful gesture. Keep
    // it with the neighbouring phrase instead of flashing talk/smile/talk.
    for(let i=0;i<voiced.length&&voiced.length>1;){
      if(voiced[i].end-voiced[i].start>=1.6){i++;continue;}
      if(i+1<voiced.length){voiced[i+1].start=voiced[i].start;voiced.splice(i,1);}
      else{voiced[i-1].end=voiced[i].end;voiced.splice(i,1);}
    }
    const spans=[];let cursor=0,index=0;
    const pause=(start,end)=>{if(end>start)spans.push({start,end,kind:'pause',sourceStart:4.42,sourceEnd:6.06,rate:1});};
    for(const part of voiced){
      pause(cursor,part.start);let start=part.start;
      // Divide long continuous speech evenly, avoiding a tiny final gesture.
      const chunks=Math.ceil((part.end-part.start)/3.9),length=(part.end-part.start)/chunks;
      for(let i=0;i<chunks;i++){
        const b=index++%2===1,sourceStart=b?6.34:.30,sourceEnd=b?11.04:4.08;
        spans.push({start,end:start+length,kind:b?'talk-b':'talk-a',sourceStart,sourceEnd,rate:1});
        // Short replies use a short portion at normal speed.
        start+=length;
      }
      cursor=part.end;
    }
    pause(cursor,duration);return {duration,spans,method:'pcm-energy-stable-phrases-v2'};
  } catch {return null;}
}

export function speechMotion(plan,time) {
  if(!plan)return null;
  const i=plan.spans.findIndex(s=>time>=s.start&&time<s.end);
  if(i<0)return {id:'tail',kind:'pause',time:11.34,rate:1,hold:true};
  const s=plan.spans[i],elapsed=Math.max(0,time-s.start);
  return {id:String(i),kind:s.kind,time:s.sourceStart+Math.min(elapsed*s.rate,s.sourceEnd-s.sourceStart-.02),rate:s.rate,
    limit:s.sourceEnd-.02,hold:elapsed>=s.sourceEnd-s.sourceStart-.02};
}
if(typeof window!=='undefined')window.companionSpeechMotion=Object.freeze({speechPlan,speechMotion});

// One audio element; generation guard also protects async play() resolution.
export class TurnPlayer {
  constructor(audio, report, error, beforePlay=null, onStop=null, afterStart=null) {
    this.beforePlay=beforePlay; this.onStop=onStop; this.afterStart=afterStart;
    this.audio=audio; this.report=report; this.error=error; this.turn=null;
    this.queue=[]; this.current=null; this.generation=0; this.url=null;
    this.complete=false;
    this.seen=new Set();
    audio.addEventListener('ended',()=>this.ended());
    audio.addEventListener('error',()=>{if(this.current)this.fail('语音播放失败，可手动重试。');});
  }
  stop() {
    this.onStop?.();
    this.generation++; this.turn=null; this.queue=[]; this.current=null; this.complete=false;
    this.seen.clear();
    this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load();
    if(this.url)URL.revokeObjectURL(this.url); this.url=null;
  }
  begin(turn) { this.stop(); this.turn=turn; }
  finish(turn) {
    if(turn!==this.turn)return false;
    this.complete=true;
    if(!this.current&&!this.queue.length)this.onStop?.();
    return true;
  }
  enqueue(event) {
    if(event.turnId!==this.turn || typeof event.testAudio!=="boolean" || event.mime!=="audio/wav" || !Number.isFinite(event.durationMs) || event.durationMs<=0)return false;
    const maxMs=event.testAudio?8000:30000;
    if(this.seen.has(event.segmentId))return false;
    if(this.queue.length>=4 || (this.current?.durationMs||0)+this.queue.reduce((s,e)=>s+e.durationMs,0)+event.durationMs>maxMs){
      this.fail('音频缓冲超过上限，已停止。'); return false;
    }
    this.seen.add(event.segmentId);
    this.queue.push(event); this.pump(); return true;
  }
  async pump() {
    if(this.current || !this.queue.length)return;
    const event=this.queue.shift(), gen=this.generation; this.current=event;
    try {
      const bytes=Uint8Array.from(atob(event.audio),c=>c.charCodeAt(0));
      this.url=URL.createObjectURL(new Blob([bytes],{type:event.mime}));
      this.audio.src=this.url;
      if(this.beforePlay)await this.beforePlay({...event,motionPlan:speechPlan(bytes)},this.audio);
      if(gen!==this.generation || this.turn!==event.turnId)return;
      await this.audio.play();
      if(gen!==this.generation || this.turn!==event.turnId)return;
      this.afterStart?.(event,this.audio);
      this.report(event,'started');
    } catch { if(gen===this.generation)this.fail('浏览器未能播放语音。请点击“重试语音”。'); }
  }
  ended() {
    const event=this.current; if(!event || event.turnId!==this.turn)return;
    this.report(event,'ended'); this.current=null;
    if(this.url)URL.revokeObjectURL(this.url); this.url=null;
    if(!this.queue.length&&this.complete)this.onStop?.();
    this.pump();
  }
  fail(message) {this.stop();this.error(message);}
}
export function shouldSend(e,composing){return e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!composing&&e.keyCode!==229;}

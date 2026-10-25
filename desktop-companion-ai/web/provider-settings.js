const $ = s => document.querySelector(s);
const names = {'token-plan':'百炼 Token Plan','chat-completions':'通用文字 · Chat Completions','anthropic-messages':'Anthropic · Messages','responses':'OpenAI · Responses','gemini-content':'Google · Gemini Generate Content','openai-audio':'通用音频 · Speech / Transcriptions'};
const authOptions = {bearer:{bearer:'API Key · Bearer',none:'无密钥（如本地服务）'},'anthropic-messages':{'x-api-key':'API Key · x-api-key',none:'无密钥（如本地服务）'},'gemini-content':{'x-goog-api-key':'API Key · x-goog-api-key',none:'无密钥（如本地服务）'}};
const node=(tag,text='',css='')=>{const e=document.createElement(tag);e.textContent=text;e.className=css;return e;};
export class ApiSettings {
  constructor({api,alertBox,onChange}) {
    Object.assign(this,{api,alertBox,onChange,state:null,draft:null,baseline:'',loading:false,saving:false,error:false});
    $('#refresh-api').addEventListener('click',()=>{if(!this.dirty()||confirm('重新读取会清除本页未保存的修改，是否继续？'))this.load();});
    $('#add-api-profile').addEventListener('click',()=>this.add());
  }
  dirty(){return this.draft!==null&&JSON.stringify(this.draft)!==this.baseline;}
  accept(){this.draft=structuredClone(this.state.config);for(const p of Object.values(this.draft.profiles)){delete p.hasKey;p.key='';}this.baseline=JSON.stringify(this.draft);}
  async load(){
    if(this.loading||this.saving)return;this.loading=true;this.onChange();
    try{this.state=await this.api('/api/admin/providers');this.accept();this.error=false;this.render();}
    catch(e){this.error=true;$('#api-status').textContent='读取失败';$('#api-help').textContent=e.message;if(document.body.classList.contains('api-view'))this.alertBox(e.message,'error');}
    finally{this.loading=false;this.onChange();}
  }
  field(parent,label,value,update,{options=null,password=false,hint='',max=1000}={}){
    const box=node('div','','field'),title=node('label',label),input=node(options?'select':'input');
    if(options){for(const [v,t]of Object.entries(options)){const o=node('option',t);o.value=v;input.append(o);}}
    else{input.type=password?'password':'text';input.maxLength=max;input.autocomplete=password?'new-password':'off';input.spellcheck=false;}
    input.value=value;input.disabled=!this.state.canSave||this.saving;title.append(input);box.append(title);if(hint)box.append(node('small',hint));
    input.addEventListener(options?'change':'input',()=>{update(input.value);this.onChange();});parent.append(box);return input;
  }
  add(){
    if(!this.draft||this.saving||Object.keys(this.draft.profiles).length>=16)return;let i=1;while(this.draft.profiles['connection'+i])i++;
    this.draft.profiles['connection'+i]={label:'新连接 '+i,protocol:'chat-completions',baseUrl:'',auth:'bearer',key:'',tokenField:'max_tokens',thinking:'omit',streamUsage:false};this.render();this.onChange();
  }
  render(){
    if(!this.draft)return;const s=this.state;
    $('#api-status').textContent=s.restartRequired?'待重启生效':s.revision==='legacy'?'现有百炼连接':s.runningRevision?'当前配置已载入':'已保存';
    $('#api-status').classList.toggle('pending',s.restartRequired);
    $('#api-help').textContent='保存只检查配置与本机加密，不调用 API。文字任务可使用 Chat Completions、Anthropic Messages、OpenAI Responses 或 Gemini Generate Content；声音仍按 Speech / Transcriptions 或百炼 Token Plan 配置。保存后重启后台服务生效。';
    const root=$('#api-profiles');root.replaceChildren();
    for(const[id,p]of Object.entries(this.draft.profiles)){
      const card=node('article','','api-service api-profile'),head=node('div','','api-profile-head'),title=node('h4',p.label||'新连接'),remove=node('button','移除','button secondary');
      remove.type='button';const used=Object.values(this.draft.assignments).some(a=>a.profile===id);remove.disabled=used||this.saving;remove.title=used?'请先将任务分配给其他连接':'移除此连接';
      remove.addEventListener('click',()=>{if(this.saving||this.loading)return;delete this.draft.profiles[id];this.render();this.onChange();});head.append(title,remove);card.append(head);
      this.field(card,'连接名称',p.label,v=>{p.label=v;title.textContent=v||'新连接';},{max:80});
      this.field(card,'接口协议',p.protocol,v=>{p.protocol=v;p.auth=v==='anthropic-messages'?'x-api-key':v==='gemini-content'?'x-goog-api-key':'bearer';p.key='';this.render();},{options:names});
      const baseHint=p.protocol==='token-plan'?'填写服务根地址，例如 https://token-plan.cn-beijing.maas.aliyuncs.com':p.protocol==='gemini-content'?'填写 Google API 根地址，例如 https://generativelanguage.googleapis.com/v1beta':'填写 API 前缀，通常以 /v1 结尾。本地接口可用 http://127.0.0.1:端口/v1';
      this.field(card,'Base URL',p.baseUrl,v=>p.baseUrl=v,{hint:baseHint});
      this.field(card,'身份验证',p.auth,v=>{p.auth=v;p.key='';this.render();},{options:authOptions[p.protocol]||authOptions.bearer});
      if(p.auth!=='none')this.field(card,'API Key',p.key,v=>p.key=v,{password:true,max:2048,hint:s.config.profiles[id]?.hasKey?'已配置；地址和协议不变时，留空保留。更换地址或协议后请重新填写。':'填写此连接专用密钥，不会回显或存入浏览器。'});
      if(p.protocol!=='openai-audio'){const d=node('details');d.append(node('summary','文字协议选项'));if(['chat-completions','token-plan'].includes(p.protocol)){
        this.field(d,'输出长度字段',p.tokenField,v=>p.tokenField=v,{options:{max_tokens:'max_tokens',max_completion_tokens:'max_completion_tokens'}});
        this.field(d,'思考参数',p.thinking,v=>p.thinking=v,{options:{omit:'不发送额外思考参数',off:'发送 enable_thinking: false（仅支持此参数的接口）'}});
        this.field(d,'流式用量',String(p.streamUsage),v=>p.streamUsage=v==='true',{options:{false:'不发送 stream_options',true:'请求 include_usage'}});
      }else d.append(node('small','此协议使用适配器规定的请求字段，模型 ID 和服务商权限仍需单独确认。'));card.append(d);}
      root.append(card);
    }
    const tasks=$('#api-services');tasks.replaceChildren();
    for(const[task,name]of Object.entries(s.tasks)){
      const a=this.draft.assignments[task],card=node('article','','api-service');card.append(node('h4',name));
      const available=Object.fromEntries(Object.entries(this.draft.profiles).filter(([,p])=>s.protocols[p.protocol].includes(task)).map(([id,p])=>[id,p.label||id]));
      if(!(a.profile in available))available[a.profile]='⚠ 连接协议不适用于此任务';
      this.field(card,'使用连接',a.profile,v=>{a.profile=v;this.render();},{options:available});
      this.field(card,'模型 ID',a.model,v=>a.model=v,{max:200,hint:'按服务商的模型 ID 填写，名称不代表权限已验证。'});
      if(task==='tts')this.field(card,'默认音色 ID',a.voice,v=>a.voice=v,{max:100,hint:'须属于当前合成服务；切换连接后以此音色为起点。'});tasks.append(card);
    }
  }
  paint(){const d=this.dirty();$('#save').textContent=this.saving?'正在加密保存…':'保存 API 配置 →';$('#save').disabled=this.saving||this.loading||this.error||!this.state?.canSave||!d;
    $('#discard').textContent='撤销本页修改';$('#discard').disabled=this.saving||!d;$('#refresh-api').disabled=this.loading||this.saving;$('#add-api-profile').disabled=this.loading||this.saving||!this.state?.canSave||Object.keys(this.draft?.profiles||{}).length>=16;
    $('#api').querySelectorAll('input,select').forEach(e=>e.disabled=this.loading||this.saving||!this.state?.canSave);
    $('#api-profiles').querySelectorAll('.api-profile-head button').forEach((e,i)=>{const id=Object.keys(this.draft.profiles)[i];e.disabled=this.loading||this.saving||!this.state?.canSave||Object.values(this.draft.assignments).some(a=>a.profile===id);});
    $('#save-dot').className='save-dot'+(this.error?' error':d||this.state?.restartRequired?' dirty':'');
    $('#save-label').textContent=this.loading?'正在读取 API 配置…':this.error?'读取失败，请重试':d?'API 配置尚未保存':this.state?.restartRequired?'已保存 · 下次启动生效':'配置已读取 · 尚未验证接口';}
  discard(){if(!this.saving&&this.state){this.accept();this.render();this.onChange();}}
  async save(){if(this.saving||this.loading||this.error||!this.state?.canSave||!this.dirty())return;this.saving=true;this.onChange();
    try{this.state=await this.api('/api/admin/providers',{method:'POST',body:JSON.stringify({revision:this.state.revision,config:this.draft})});this.accept();this.render();this.alertBox('连接与任务分配已加密保存，重新启动后台服务后生效。未发送测试请求。','success');}
    catch(e){this.alertBox(e.name==='TimeoutError'?'保存结果尚未确认，请重新读取配置后检查。输入已保留。':e.message,'error');}
    finally{this.saving=false;this.onChange();}}
}

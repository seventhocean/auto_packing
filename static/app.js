/* DeepFlow 升级包构建 — frontend logic */
/* ================================================================
   STATE
   ================================================================ */
let versionData=[],packageNameTemplate='',eventSource=null,currentSeries='v6',isBuilding=false;
let channelList=[],currentChannel='';
let existingFilesData=[],logExpanded=false,lastLogSeq=0,currentTaskId='',sseRetries=0;
let overviewTimer=null,overviewLoading=false;
let lastFocusBeforeDrawer=null,lastFocusBeforeModal=null;
const OVERVIEW_POLL_MS=5000;
const TASK_QUERY_KEY='task_id';
const ICON_DOWNLOAD='<svg class="icon icon-sm" viewBox="0 0 16 16" aria-hidden="true"><path d="M7.25 1h1.5v7.1l2.6-2.6 1.05 1.05L8 11.5 3.6 7.1 4.65 6.05l2.6 2.6V1ZM2 13h12v2H2v-2Z"/></svg>';
const ICON_COPY='<svg class="icon icon-sm" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h8v2H4V2zm10 4v9H2V6h3V4h6v2h3zm-2 2H4v5h8V8z"/></svg>';
const BUILD_BTN_IDLE='<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6L13 8 5 3.2Z"/></svg> 开始构建';

/* ================================================================
   UTILS
   ================================================================ */
function beforeUnloadHandler(e){e.preventDefault();e.returnValue='构建任务正在进行中，离开页面将丢失进度。';return e.returnValue}
function debounce(fn,d){let t=null;return function(...a){clearTimeout(t);t=setTimeout(()=>fn.apply(this,a),d)}}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function setTaskInUrl(taskId){
    const url=new URL(location.href);
    if(taskId)url.searchParams.set(TASK_QUERY_KEY,taskId);
    else url.searchParams.delete(TASK_QUERY_KEY);
    history.replaceState(null,'',url.pathname+url.search+url.hash);
}

function showToast(message,type){
    const region=document.getElementById('toastRegion');
    if(!region||!message)return;
    const el=document.createElement('div');
    el.className='toast'+(type==='error'?' error':'');
    el.textContent=message;
    region.appendChild(el);
    requestAnimationFrame(()=>el.classList.add('show'));
    setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),200)},5000);
}

/* ================================================================
   INIT
   ================================================================ */
document.addEventListener('DOMContentLoaded',async()=>{
    const params=new URLSearchParams(location.search);
    const modeParam=params.get('mode')||params.get('series');
    if(modeParam==='v7'||modeParam==='custom')currentSeries='v7';
    else if(modeParam==='v6'||modeParam==='patch')currentSeries='v6';
    initVersionPickers();initEventListeners();setMode(currentSeries);
    await loadChannels(params.get('channel'));
    fetchExistingFiles();startTaskOverviewPolling();
    // 刷新后凭 URL 中的 task_id 恢复正在进行的构建
    const taskId=params.get(TASK_QUERY_KEY);
    if(taskId)resumeBuildTask(taskId);
});

/* ================================================================
   MODE & CHANNEL
   ================================================================ */
function setMode(m){
    currentSeries=m;document.body.dataset.mode=m;
    document.querySelectorAll('#modeToggle input[name="buildMode"]').forEach(r=>{r.checked=r.value===m});
    document.getElementById('v6Workspace').classList.toggle('active',m==='v6');
    document.getElementById('v7Workspace').classList.toggle('active',m==='v7');
    syncUrlParams();
    if(!isBuilding){checkBuildBtnStatus();updateStepperLabels(m);}
    updateV7Preview();
}

// 把「模式 + 版本线」写进 URL，刷新后能还原到同一屏
function syncUrlParams(){
    const url=new URL(location.href);
    const mode=currentSeries==='v7'?'custom':'patch';
    if(url.searchParams.get('mode')!==mode)url.searchParams.set('mode',mode);
    url.searchParams.delete('series');
    if(currentSeries==='v6'&&currentChannel){
        if(url.searchParams.get('channel')!==currentChannel)url.searchParams.set('channel',currentChannel);
    }else if(url.searchParams.has('channel')){
        url.searchParams.delete('channel');
    }
    history.replaceState(null,'',url.pathname+url.search+url.hash);
}

async function loadChannels(preferred){
    const sel=document.getElementById('channelSelect');
    try{
        const r=await fetch('/channels',{cache:'no-store'});
        const d=await r.json();
        if(d.success&&Array.isArray(d.channels))channelList=d.channels;
    }catch(e){channelList=[]}
    if(!channelList.length){
        if(currentPicker)currentPicker.setOptions([],'版本线加载失败');
        return;
    }
    const want=(preferred&&channelList.some(c=>c.key===preferred))
        ? preferred
        : (channelList.find(c=>c.is_default)||channelList[0]).key;
    currentChannel=want;
    if(sel){
        sel.innerHTML=channelList.map(c=>`<option value="${escapeHtml(c.key)}">${escapeHtml(c.label)}</option>`).join('');
        sel.value=want;
    }
    syncUrlParams();
    fetchVersions(want);
}

function handleChannelChange(e){
    const v=e.target.value;
    if(!v||v===currentChannel)return;
    currentChannel=v;
    versionData=[];packageNameTemplate='';
    if(targetPicker){targetPicker.setOptions([],'请先选择当前版本');targetPicker.setDisabled(true)}
    updateV6Preview();checkBuildBtnStatus();syncUrlParams();
    fetchVersions(v);
}

function updateStepperLabels(m){
    const step1=document.querySelector('.step-item[data-step="1"] .step-title');
    if(step1)step1.textContent=m==='v7'?'1. 清单准备':'1. 差集计算';
}
function setModeLocked(locked){
    document.body.classList.toggle('is-building',!!locked);
    document.querySelectorAll('#modeToggle input[name="buildMode"]').forEach(i=>{i.disabled=!!locked});
}

function handleModeChange(e){
    const m=e.target&&e.target.value;
    if(e.target.name!=='buildMode'||!m||m===currentSeries)return;
    setMode(m);
    if(m==='v6'&&versionData.length===0)fetchVersions(currentChannel);
}

/* ================================================================
   VERSIONS
   ================================================================ */
let currentPicker=null,targetPicker=null;

function initVersionPickers(){
    currentPicker=createVersionPicker({
        inputId:'currentVersionInput',listId:'currentVersionList',comboId:'currentVersionCombo',
        placeholder:'请选择当前版本',onChange:handleCurrentVersionChange
    });
    targetPicker=createVersionPicker({
        inputId:'targetVersionInput',listId:'targetVersionList',comboId:'targetVersionCombo',
        placeholder:'请选择目标版本',onChange:handleTargetVersionChange
    });
    targetPicker.setDisabled(true);
}

function createVersionPicker(cfg){
    const input=document.getElementById(cfg.inputId);
    const list=document.getElementById(cfg.listId);
    const combo=document.getElementById(cfg.comboId);
    let options=[],selected=null,visible=[],activeIndex=-1,disabled=false;

    function setExpanded(v){list.hidden=!v;input.setAttribute('aria-expanded',v?'true':'false');if(!v)activeIndex=-1}
    function close(){setExpanded(false)}
    function choose(o){selected=o;input.value=o?o.display:'';close();if(cfg.onChange)cfg.onChange(o||null)}
    function updateActive(){
        const children=Array.from(list.children);
        children.forEach((el,i)=>el.classList.toggle('active',i===activeIndex));
        const el=children[activeIndex];
        if(el&&el.scrollIntoView)el.scrollIntoView({block:'nearest'});
    }
    function render(){
        if(disabled){close();return}
        const q=input.value.trim().toLowerCase();
        const filtering=!(selected&&input.value===selected.display);
        visible=(filtering&&q)?options.filter(o=>o.display.toLowerCase().includes(q)||o.value.toLowerCase().includes(q)):options.slice();
        list.innerHTML='';
        if(visible.length===0){
            const e=document.createElement('div');e.className='combo-empty';
            e.textContent=options.length?'无匹配版本':'暂无可选版本';list.appendChild(e);
        }else{
            visible.forEach(o=>{
                const el=document.createElement('div');
                el.className='combo-option'+((selected&&o.value===selected.value)?' selected':'');
                el.setAttribute('role','option');
                el.setAttribute('aria-selected',(selected&&o.value===selected.value)?'true':'false');
                el.textContent=o.display;
                // mousedown + preventDefault：选择时 input 不触发 blur
                el.addEventListener('mousedown',ev=>{ev.preventDefault();choose(o)});
                list.appendChild(el);
            });
        }
        activeIndex=-1;setExpanded(true);
    }

    input.addEventListener('focus',()=>{if(!disabled)render()});
    input.addEventListener('click',()=>{if(!disabled&&list.hidden)render()});
    input.addEventListener('input',()=>{
        if(disabled)return;
        selected=null;
        if(cfg.onChange)cfg.onChange(null);
        render();
    });
    input.addEventListener('keydown',ev=>{
        if(disabled)return;
        if(ev.key==='ArrowDown'||ev.key==='ArrowUp'){
            ev.preventDefault();
            if(list.hidden)render();
            if(visible.length===0)return;
            const delta=ev.key==='ArrowDown'?1:-1;
            activeIndex=(activeIndex+delta+visible.length)%visible.length;
            updateActive();
        }else if(ev.key==='Enter'){
            if(!list.hidden&&visible[activeIndex]){ev.preventDefault();choose(visible[activeIndex])}
        }else if(ev.key==='Escape'){
            close();
        }
    });
    input.addEventListener('blur',()=>{
        setTimeout(()=>{
            if(combo.contains(document.activeElement))return;
            if(selected){input.value=selected.display}
            else if(input.value.trim()){input.value='';if(cfg.onChange)cfg.onChange(null)}
            close();
        },0);
    });
    document.addEventListener('click',ev=>{if(!combo.contains(ev.target))close()});

    return {
        getValue(){return selected?selected.value:''},
        getSelected(){return selected},
        chooseByValue(value){
            const hit=options.find(o=>o.value===value);
            if(!hit)return false;
            choose(hit);
            return true;
        },
        setOptions(opts,placeholder){
            options=Array.isArray(opts)?opts:[];
            selected=null;input.value='';
            if(placeholder)input.placeholder=placeholder;
            close();
        },
        setDisabled(v){
            disabled=!!v;input.disabled=disabled;combo.classList.toggle('disabled',disabled);
            if(disabled){selected=null;input.value='';close()}
        }
    };
}

async function fetchVersions(channel){
    if(!channel)return;
    if(currentPicker)currentPicker.setOptions([], '加载中...');
    try{
        const r=await fetch(`/versions?channel=${encodeURIComponent(channel)}`);
        if(currentChannel!==channel)return;
        const d=await r.json();
        if(d.success&&d.versions){versionData=d.versions;packageNameTemplate=d.package_name_template||'';renderVersionOptions()}
        else{versionData=[];renderVersionOptions(true)}
    }catch(e){if(currentChannel!==channel)return;versionData=[];renderVersionOptions(true)}
}
function renderVersionOptions(isError){
    if(!currentPicker)return;
    currentPicker.setOptions(isError?[]:versionData,isError?'版本加载失败':'请选择当前版本');
    handleCurrentVersionChange(null);
}
function handleCurrentVersionChange(option){
    if(!option){
        targetPicker.setOptions([], '请先选择当前版本');
        targetPicker.setDisabled(true);
    }else{
        const currentSeq=Number(option.seq_num);
        const newer=versionData.filter(v=>Number(v.seq_num)>currentSeq);
        targetPicker.setOptions(newer,newer.length?'请选择目标版本':'暂无更新版本');
        targetPicker.setDisabled(newer.length===0);
    }
    updateV6Preview();
    checkBuildBtnStatus();
}
function handleTargetVersionChange(){
    updateV6Preview();
    checkBuildBtnStatus();
}
function updateV6Preview(){
    const el=document.getElementById('v6Preview');
    if(!el)return;
    el.innerHTML='';
    const cur=currentPicker?currentPicker.getSelected():null;
    const tgt=targetPicker?targetPicker.getSelected():null;
    if(!cur||!tgt){
        el.textContent='选择已发布的起始版本与目标版本，系统将自动对比并计算差集镜像清单';
        return;
    }

    const span=Number(tgt.seq_num)-Number(cur.seq_num);
    const from=String(cur.value).split('-')[0];
    const to=String(tgt.value).split('-')[0];
    const expected=packageNameTemplate
        ? packageNameTemplate.replace('{current}',from).replace('{target}',to)
        : '';
    const existing=expected?(existingFilesData||[]).find(f=>f.name===expected):null;

    const info=document.createElement('span');
    info.textContent=`✓ 已选定跨越 ${span} 个 patch 版本（${cur.display||cur.value} → ${tgt.display||tgt.value}）`;
    el.appendChild(info);
    if(existing){
        const link=document.createElement('a');
        link.href=`/download-existing/${encodeURIComponent(existing.name)}`;
        link.textContent='（已有同名升级包，可直接下载）';
        el.appendChild(link);
    }
}

/* ================================================================
   BUILD BUTTON
   ================================================================ */
function checkBuildBtnStatus(){
    const b6=document.getElementById('v6BuildBtn'),b7=document.getElementById('v7BuildBtn');
    if(isBuilding){b6.disabled=true;b7.disabled=true;return}
    if(currentSeries==='v7'){
        b6.disabled=true;
        const parsed=parseV7Images(document.getElementById('v7ImageList').value);
        b7.disabled=parsed.valid.length===0||parsed.invalid.length>0;
    }
    else{
        b7.disabled=true;
        const cv=currentPicker?currentPicker.getValue():'';
        const tv=targetPicker?targetPicker.getValue():'';
        b6.disabled=!(cv&&tv);
    }
}

/* ================================================================
   BUILD PROCESS
   ================================================================ */
function showCancelButton(show,enabled){
    const btn=document.getElementById('btnCancelBuild');
    btn.style.display=show?'inline-block':'none';
    btn.disabled=!enabled;
    if(!show)btn.textContent='取消构建';
}
function handleCancelBuild(){
    if(!currentTaskId)return;
    const btn=document.getElementById('btnCancelBuild');
    btn.disabled=true;btn.textContent='取消中...';
    fetch(`/build/${encodeURIComponent(currentTaskId)}/cancel`,{method:'POST'})
        .then(r=>r.json())
        .then(d=>{
            if(!d.success){
                addLog(d.message||'取消请求失败','error');
                btn.disabled=false;btn.textContent='取消构建';
                showToast(d.message||'取消请求失败','error');
            }
        })
        .catch(()=>{
            addLog('取消请求失败，请检查网络','error');
            btn.disabled=false;btn.textContent='取消构建';
            showToast('取消请求失败，请检查网络','error');
        });
}

function setBuildButtonsBusy(){
    const busy='<span class="spinner"></span> 构建中...';
    const b6=document.getElementById('v6BuildBtn'),b7=document.getElementById('v7BuildBtn');
    b6.innerHTML=busy;b7.innerHTML=busy;b6.disabled=true;b7.disabled=true;
}
function resetBuildButtons(){
    const b6=document.getElementById('v6BuildBtn'),b7=document.getElementById('v7BuildBtn');
    b6.innerHTML=BUILD_BTN_IDLE;b7.innerHTML=BUILD_BTN_IDLE;
}
function setLogExpanded(v){
    logExpanded=!!v;
    const el=document.getElementById('buildLog');
    const container=document.getElementById('terminalContainer');
    if(container)container.classList.toggle('expanded',logExpanded);
    if(el)el.classList.toggle('expanded',logExpanded);
    document.getElementById('btnToggleLog').textContent=logExpanded?'收起日志':'展开日志';
    if(logExpanded&&el)requestAnimationFrame(()=>{el.scrollTop=el.scrollHeight});
}

function fallbackCopy(text,successMessage){
    const ta=document.createElement('textarea');
    ta.value=text;ta.setAttribute('readonly','');
    ta.style.position='fixed';ta.style.top='-1000px';ta.style.opacity='0';
    document.body.appendChild(ta);
    ta.select();ta.setSelectionRange(0,ta.value.length);
    let ok=false;
    try{ok=document.execCommand('copy')}catch(e){}
    ta.remove();
    if(ok)showToast(successMessage);
    else showToast('复制失败，请手动选择文本','error');
}
function copyTextToClipboard(text,successMessage){
    if(window.isSecureContext&&navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(text)
            .then(()=>showToast(successMessage))
            .catch(()=>fallbackCopy(text,successMessage));
    }else{
        fallbackCopy(text,successMessage);
    }
}

function copyLogsToClipboard(){
    const el=document.getElementById('buildLog');
    if(!el||!el.textContent.trim()){
        showToast('暂无日志可复制','error');
        return;
    }
    const entries=Array.from(el.querySelectorAll('.log-entry')).map(e=>e.textContent).join('\n');
    copyTextToClipboard(entries||el.textContent,'控制台日志已复制到剪贴板');
}

function clearLogScreen(){
    const el=document.getElementById('buildLog');
    if(el)el.innerHTML='';
    showToast('控制台日志已清屏');
}

function notifyBuildComplete(title,body){
    if(!window.isSecureContext||!('Notification' in window))return;
    if(Notification.permission==='granted'){
        try{new Notification(title,{body,icon:'static/favicon.ico'})}catch(e){}
    }
}

function requestNotificationPermission(){
    if(!window.isSecureContext||!('Notification' in window))return;
    if(Notification.permission==='default'){
        Notification.requestPermission();
    }
}

function copyCurlCommand(fileName){
    const url=`${window.location.origin}/download-existing/${encodeURIComponent(fileName)}`;
    const cmd=`curl -O "${url}"`;
    copyTextToClipboard(cmd,'已复制 curl 下载命令');
}
function currentProgressPercent(){
    const w=parseFloat(document.getElementById('progressBar').style.width);
    return Number.isFinite(w)?w:0;
}
function finishBuild(){
    isBuilding=false;setModeLocked(false);
    window.removeEventListener('beforeunload',beforeUnloadHandler);
    if(eventSource){eventSource.close();eventSource=null}
    resetBuildButtons();
    setTaskInUrl('');
    currentTaskId='';
    checkBuildBtnStatus();
    refreshTaskOverview();
}

function startBuildProcess(e){
    if(e)e.preventDefault();if(isBuilding)return;
    const cv=currentPicker?currentPicker.getValue():'',tv=targetPicker?targetPicker.getValue():'';
    const vi=document.getElementById('v7ImageList').value;
    if(currentSeries==='v6'&&(!cv||!tv))return;

    let payload;
    if(currentSeries==='v7'){
        const parsed=parseV7Images(vi);
        if(parsed.invalid.length){
            updateV7Preview();
            showToast(`有 ${parsed.invalid.length} 行镜像格式无效，请修正后再构建`,'error');
            return;
        }
        if(parsed.valid.length===0){showToast('请输入至少一个镜像名称','error');return}
        payload={mode:'custom',images:parsed.valid};
    }else{
        payload={mode:'patch',channel:currentChannel,current:cv,target:tv};
    }

    isBuilding=true;sseRetries=0;updateStepperLabels(currentSeries);setModeLocked(true);window.addEventListener('beforeunload',beforeUnloadHandler);
    requestNotificationPermission();
    currentTaskId='';showCancelButton(true,false);setBuildButtonsBusy();hideCompletionActions();
    const bc=document.getElementById('buildCard'),bl=document.getElementById('buildLog');
    bc.classList.add('active');bl.innerHTML='';setLogExpanded(true);lastLogSeq=0;
    const termTitle=document.getElementById('terminalTitle');
    if(termTitle)termTitle.textContent='console · worker-output [queued]';
    document.getElementById('taskStateText').textContent='排队中';
    document.getElementById('taskMessageText').textContent='';
    updateProgressBar(0,'');updatePipelineStepper('queued',0,'排队中，等待 Worker 分配');checkBuildBtnStatus();
    if(eventSource){eventSource.close();eventSource=null}

    // V6/V7 统一：先 POST 建任务拿 task_id，再用 GET 订阅 SSE。
    // 这样 SSE 重连只会重放日志，不会重复入队。
    fetch('/build',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
        .then(async r=>{
            let d={};
            try{d=await r.json()}catch(err){}
            if(!r.ok||!d.success||!d.task_id){
                throw new Error(d.message||`提交构建任务失败（HTTP ${r.status}）`);
            }
            return d;
        })
        .then(d=>{
            if(!isBuilding)return;
            currentTaskId=d.task_id;
            setTaskInUrl(d.task_id);
            if(termTitle)termTitle.textContent=`console · build-task-${d.task_id}`;
            showCancelButton(true,true);
            connectBuildStream(`/build?task_id=${encodeURIComponent(d.task_id)}`);
        })
        .catch(err=>{
            const message=err&&err.message?err.message:'提交构建任务失败，请检查网络';
            resetBuildState();
            showToast(message,'error');
        });
}

function resumeBuildTask(taskId){
    currentTaskId=taskId;isBuilding=true;sseRetries=0;setModeLocked(true);
    window.addEventListener('beforeunload',beforeUnloadHandler);
    requestNotificationPermission();
    setBuildButtonsBusy();showCancelButton(true,false);hideCompletionActions();
    const bc=document.getElementById('buildCard'),bl=document.getElementById('buildLog');
    bc.classList.add('active');bl.innerHTML='';setLogExpanded(true);lastLogSeq=0;
    const termTitle=document.getElementById('terminalTitle');
    if(termTitle)termTitle.textContent=`console · build-task-${taskId}`;
    document.getElementById('taskStateText').textContent='恢复任务中';
    document.getElementById('taskMessageText').textContent='';
    updateProgressBar(0,'');updatePipelineStepper('queued',0,'正在恢复任务状态...');checkBuildBtnStatus();
    connectBuildStream(`/build?task_id=${encodeURIComponent(taskId)}`);
}

function connectBuildStream(url){
    if(eventSource){eventSource.close();eventSource=null}
    const sep=url.includes('?')?'&':'?';
    eventSource=new EventSource(url+sep+'since='+encodeURIComponent(lastLogSeq));
    eventSource.onmessage=function(ev){
        sseRetries=0;
        try{handleBuildStatus(JSON.parse(ev.data))}catch(e){}
    };
    eventSource.onerror=function(){
        if(eventSource){eventSource.close();eventSource=null}
        if(!isBuilding)return;
        if(sseRetries<3){
            sseRetries++;
            setTimeout(()=>{if(isBuilding)connectBuildStream(url)},Math.min(1000*Math.pow(2,sseRetries),8000));
        }else{
            handleStreamLost();
        }
    };
}

function handleStreamLost(){
    isBuilding=false;setModeLocked(false);
    window.removeEventListener('beforeunload',beforeUnloadHandler);
    resetBuildButtons();checkBuildBtnStatus();
    document.getElementById('taskStateText').textContent='连接中断';
    document.getElementById('taskMessageText').textContent='任务可能仍在运行，可返回任务中心查看或刷新页面恢复';
    setCompletionActions('');
    showToast('与构建服务的连接中断，刷新页面可恢复任务状态','error');
}

function resetBuildState(){
    resetBuildButtons();
    isBuilding=false;setModeLocked(false);window.removeEventListener('beforeunload',beforeUnloadHandler);
    lastLogSeq=0;sseRetries=0;currentTaskId='';showCancelButton(false);
    if(eventSource){eventSource.close();eventSource=null}
    document.getElementById('buildCard').classList.remove('active');
    setLogExpanded(false);
    setTaskInUrl('');
    hideCompletionActions();
    checkBuildBtnStatus();
    refreshTaskOverview();
}

let newPackageName='';
function handleBuildStatus(d){
    const st=document.getElementById('taskStateText');
    const mt=document.getElementById('taskMessageText'),logs=Array.isArray(d.logs)?d.logs:[];
    // 刷新恢复任务时，用任务元数据校准当前模式与版本线
    const taskMode=(d.mode==='custom'||(!d.mode&&d.task_type==='v7'))?'v7':'v6';
    if(taskMode==='v6'&&d.channel&&d.channel!==currentChannel){
        currentChannel=d.channel;
        const sel=document.getElementById('channelSelect');
        if(sel)sel.value=d.channel;
        fetchVersions(d.channel);
    }
    updateStepperLabels(taskMode);
    if(taskMode!==currentSeries)setMode(taskMode);
    if(d.task_id){
        currentTaskId=d.task_id;
        if(isBuilding){
            setTaskInUrl(d.task_id);
            if(d.status!=='complete'&&d.status!=='error')showCancelButton(true,true);
        }
    }
    // 服务端只推送游标之后的新日志，直接追加即可
    logs.forEach(l=>{
        if(l&&l.content)addLog(l.content,mapLogLevel(l.level),l.timestamp);
        if(l&&Number.isInteger(l.seq)&&l.seq>lastLogSeq)lastLogSeq=l.seq;
    });
    if(Number.isInteger(d.log_seq)&&d.log_seq>lastLogSeq)lastLogSeq=d.log_seq;
    updatePipelineStepper(d.status, d.percent, d.message);
    switch(d.status){
        case'queued':st.textContent='排队中';updateProgressBar(d.percent||0,'');mt.textContent=d.message||'';break;
        case'progress':st.textContent='构建中';updateProgressBar(d.percent,'');mt.textContent=d.message||'';break;
        case'complete':
            st.textContent='已完成';updateProgressBar(100,'success');mt.textContent='构建完成，升级包已生成';
            newPackageName=d.package_name||'';
            showCancelButton(false);
            finishBuild();
            setCompletionActions(d.package_name||'');
            fetchExistingFiles();
            notifyBuildComplete('🎉 DeepFlow 升级包构建完成', d.package_name ? `升级包 ${d.package_name} 已就绪` : '升级包构建成功，已可下载');
            break;
        case'error':{
            st.textContent=d.cancelled?'已取消':'失败';
            updateProgressBar(currentProgressPercent(),'error');
            mt.textContent=d.message||'';
            // 服务端日志里通常已经包含错误信息，避免重复追加
            const logEl=document.getElementById('buildLog'),last=logEl.lastElementChild;
            if(d.message&&(!last||!last.textContent.includes(d.message)))addLog(d.message,'error');
            showCancelButton(false);
            finishBuild();
            setCompletionActions('');
            if(logEl.childElementCount>0)setLogExpanded(true);
            showToast(d.cancelled?'构建已取消':(d.message||'构建失败'),'error');
            notifyBuildComplete('⚠️ DeepFlow 升级包构建失败', d.cancelled ? '构建任务已取消' : (d.message || '构建失败'));
            break;
        }
    }
}

/* ================================================================
   PROGRESS & LOG
   ================================================================ */
function updatePipelineStepper(status, percent, message){
    const stepper = document.getElementById('pipelineStepper');
    if(!stepper) return;
    const p = Number(percent) || 0;

    // 映射阶段：1(准备与差集计算 0~29%), 2(镜像拉取与保存 30~69%), 3(组装打包 70~99%), 4(交付就绪 100%)
    let activeStep = 1;
    if(status === 'complete' || p >= 100){
        activeStep = 4;
    } else if(p >= 70){
        activeStep = 3;
    } else if(p >= 30){
        activeStep = 2;
    } else {
        activeStep = 1;
    }

    const isComplete = (status === 'complete' || p >= 100);
    const isError = (status === 'error');

    for(let i = 1; i <= 4; i++){
        const item = stepper.querySelector(`[data-step="${i}"]`);
        if(!item) continue;
        const sub = item.querySelector('.step-sub');
        const defaultText = sub ? sub.dataset.default : '';

        item.classList.remove('waiting', 'active', 'completed', 'error');

        if(isComplete){
            item.classList.add('completed');
            if(sub) sub.textContent = (i === 4 && message) ? message : (defaultText || '');
        } else if(i < activeStep){
            item.classList.add('completed');
            if(sub && defaultText) sub.textContent = defaultText;
        } else if(i === activeStep){
            if(isError){
                item.classList.add('error');
                if(sub) sub.textContent = message || '执行失败';
            } else {
                item.classList.add('active');
                if(sub) sub.textContent = message || defaultText || '进行中...';
            }
        } else {
            item.classList.add('waiting');
            if(sub && defaultText) sub.textContent = defaultText;
        }
    }

    const connectors = stepper.querySelectorAll('.step-connector');
    connectors.forEach((conn, idx) => {
        conn.classList.remove('completed', 'error');
        if(isComplete){
            conn.classList.add('completed');
        } else if(isError && activeStep === idx + 1){
            conn.classList.add('error');
        } else if(activeStep > idx + 1){
            conn.classList.add('completed');
        }
    });
}

function updateProgressBar(pct,modifier){
    const b=document.getElementById('progressBar');const p=Math.max(0,Math.min(100,pct||0));
    b.style.width=p+'%';b.classList.remove('error','success');
    if(modifier==='error')b.classList.add('error');if(modifier==='success')b.classList.add('success');
}
function addLog(msg,type,timestamp){
    const c=String(msg).trim();if(!c)return;
    const el=document.getElementById('buildLog');
    // 用户向上翻日志时不抢滚动位置
    const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<32;
    const d=document.createElement('div');d.className='log-entry';
    const matched=String(timestamp||'').match(/(\d{2}:\d{2}:\d{2})/);
    const ts=matched?matched[1]:new Date().toTimeString().slice(0,8);
    const cls=type==='error'?'log-error':type==='success'?'log-success':type==='warning'?'log-warning':'log-info';
    const time=document.createElement('span');time.className='log-time';time.textContent=`[${ts}]`;
    const text=document.createElement('span');text.className=cls;text.textContent=c;
    d.append(time,' ',text);el.appendChild(d);
    if(nearBottom)el.scrollTop=el.scrollHeight;
}
function mapLogLevel(l){return l==='ERROR'?'error':(l==='WARNING'||l==='WARN')?'warning':'info'}

/* ================================================================
   TASK CENTER
   ================================================================ */
function startTaskOverviewPolling(){
    refreshTaskOverview();
    if(overviewTimer)clearInterval(overviewTimer);
    overviewTimer=setInterval(()=>{if(!document.hidden)refreshTaskOverview()},OVERVIEW_POLL_MS);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshTaskOverview()});
}

async function refreshTaskOverview(){
    if(overviewLoading)return;
    overviewLoading=true;
    try{
        const r=await fetch('/tasks/overview',{cache:'no-store'});
        let d={};
        try{d=await r.json()}catch(e){}
        if(!r.ok||!d.success)throw new Error(d.message||'获取任务状态失败');
        renderTaskOverview(d);
    }catch(e){
        document.getElementById('taskOverviewError').hidden=false;
    }finally{
        overviewLoading=false;
    }
}

function renderTaskOverview(d){
    const active=Array.isArray(d.active)?d.active:[];
    const recent=Array.isArray(d.recent)?d.recent:[];
    const sys=d.system||{};
    const serverTime=Number(sys.server_time)||Math.floor(Date.now()/1000);

    document.getElementById('taskOverviewError').hidden=true;
    document.getElementById('activeTaskCount').textContent=active.length;
    document.getElementById('activeTaskEmpty').hidden=active.length>0;
    document.getElementById('activeTaskList').innerHTML=active.map(t=>taskRowHtml(t,true,serverTime)).join('');
    document.getElementById('recentTaskEmpty').hidden=recent.length>0;
    document.getElementById('recentTaskList').innerHTML=recent.map(t=>taskRowHtml(t,false,serverTime)).join('');

    const online=!!sys.worker_online;
    const dot=document.getElementById('workerDot');
    dot.classList.toggle('online',online);
    dot.classList.toggle('offline',!online);
    document.getElementById('workerText').textContent=online?'Worker 在线':'Worker 离线';

    const queue=Number(sys.queue_length);
    document.getElementById('queueLength').textContent=Number.isFinite(queue)?queue:'-';
    const slots=Number(sys.active_slots);
    document.getElementById('activeSlots').textContent=Number.isFinite(slots)?slots:'-';
    const maxSlots=Number(sys.max_concurrent_tasks);
    document.getElementById('maxSlots').textContent=Number.isFinite(maxSlots)?maxSlots:'-';

    const free=Number(sys.disk_free);
    const total=Number(sys.disk_total);
    const diskEl=document.getElementById('diskFree');
    diskEl.textContent=free>0?formatGB(free):'-';
    diskEl.parentElement.title=free>0&&total>0?`剩余 ${formatGB(free)} / 共 ${formatGB(total)}`:'';
}

function taskRowHtml(t,isActive,serverTime){
    const id=escapeHtml(t.task_id||'');
    const label=escapeHtml(taskLabel(t));
    const status=taskStatusInfo(t);
    const percent=Math.max(0,Math.min(100,Number(t.percent)||0));
    const created=Number(t.created_at)||0;
    const updated=Number(t.updated_at)||0;
    const duration=created?formatDuration((isActive?serverTime:updated)-created):'';
    const plainMeta=isActive
        ?`${duration?duration+' · ':''}${percent}%${t.message?' · '+t.message:''}`
        :`${formatClock(updated)}${duration?' · '+duration:''}${t.message?' · '+t.message:''}`;
    const meta=escapeHtml(plainMeta);

    // 已结束的任务也要能回看日志：失败时这是唯一的排查入口
    let actions='';
    if(isActive){
        actions=`<button type="button" class="btn-sm" data-action="view" data-task-id="${id}">查看</button>`
               +`<button type="button" class="btn-sm danger" data-action="cancel" data-task-id="${id}">取消</button>`;
    }else{
        const parts=[];
        if(t.package_name)parts.push(`<a class="btn-sm" href="/download-existing/${encodeURIComponent(t.package_name)}" data-download="1">下载</a>`);
        parts.push(`<button type="button" class="btn-sm" data-action="view" data-task-id="${id}" title="查看本次构建日志">日志</button>`);
        if(!t.cancelled)parts.push(`<button type="button" class="btn-sm" data-action="retry" data-task-id="${id}" title="用相同参数再构建一次">重试</button>`);
        actions=parts.join('');
    }

    const progress=isActive
        ?`<div class="task-progress" role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100" aria-label="构建进度"><i style="width:${percent}%"></i></div>`
        :'';

    const chipText=t.channel||((t.mode==='custom'||t.task_type==='v7')?'自定义':'');
    const chip=chipText?`<span class="task-channel">${escapeHtml(chipText)}</span>`:'';
    return `<div class="task-item">`
        +`<div class="task-main">`
        +`<div class="task-line">${chip}<span class="task-label" title="${label}">${label}</span>`
        +`<span class="task-status ${status.cls}">${status.text}</span></div>`
        +`<div class="task-message" title="${meta}">${meta}</div>`
        +progress
        +`</div><div class="task-actions">${actions}</div></div>`;
}

function taskLabel(t){
    if(t.task_type==='v6'){
        const current=t.current_version?String(t.current_version).split('-')[0]:'';
        const target=t.target_version?String(t.target_version).split('-')[0]:'';
        if(current||target)return `${current||'?'} → ${target||'?'}`;
    }
    if(t.task_type==='v7')return `${Number(t.image_count)||0} 个镜像`;
    return t.task_id||'构建任务';
}

function taskStatusInfo(t){
    if(t.status==='queued')return{text:'排队中',cls:''};
    if(t.status==='progress')return{text:'构建中',cls:'progress'};
    if(t.cancelled)return{text:'已取消',cls:'error'};
    if(t.error)return{text:'失败',cls:'error'};
    if(t.complete)return{text:'已完成',cls:'complete'};
    return{text:t.status||'未知',cls:''};
}

function formatDuration(seconds){
    let sec=Math.max(0,Math.floor(Number(seconds)||0));
    const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=Math.floor(sec%60);
    if(h)return `${h}h ${String(m).padStart(2,'0')}m`;
    return `${m}:${String(s).padStart(2,'0')}`;
}

function formatClock(ts){
    const n=Number(ts);
    if(!n)return '-';
    const d=new Date(n*1000),pad=x=>String(x).padStart(2,'0');
    return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatGB(bytes){
    const value=Number(bytes);
    if(!Number.isFinite(value)||value<=0)return '-';
    const gb=value/1073741824;
    return gb>=10?`${gb.toFixed(1)} GB`:`${gb.toFixed(2)} GB`;
}

function handleActiveTaskClick(e){
    const btn=e.target.closest('[data-action]');
    if(!btn)return;
    if(btn.dataset.action==='view')viewTaskFromOverview(btn.dataset.taskId);
    else if(btn.dataset.action==='cancel')cancelTaskFromOverview(btn.dataset.taskId,btn);
}

function handleRecentTaskClick(e){
    const link=e.target.closest('[data-download]');
    if(link){checkExistingFileDownload(e,link);return}
    const btn=e.target.closest('[data-action]');
    if(!btn)return;
    if(btn.dataset.action==='view')viewTaskFromOverview(btn.dataset.taskId);
    else if(btn.dataset.action==='retry')retryTaskFromOverview(btn.dataset.taskId);
}

// 用上一次的参数重新填好表单（不直接提交，让人确认一眼再点开始构建）
async function retryTaskFromOverview(taskId){
    if(!taskId)return;
    try{
        const r=await fetch(`/task-status/${encodeURIComponent(taskId)}`,{cache:'no-store'});
        let d={};
        try{d=await r.json()}catch(e){}
        if(!r.ok||!d.success||!d.status)throw new Error(d.message||'任务信息已过期，无法重试');
        const s=d.status;
        const isCustom=(s.mode==='custom'||(!s.mode&&s.task_type==='v7'));
        if(isCustom){
            const images=Array.isArray(s.images)?s.images:[];
            if(!images.length)throw new Error('该任务没有记录镜像清单，无法自动重试');
            setMode('v7');
            applyV7Images(images);
        }else{
            setMode('v6');
            if(s.channel&&s.channel!==currentChannel){
                currentChannel=s.channel;
                const sel=document.getElementById('channelSelect');
                if(sel)sel.value=s.channel;
                await fetchVersions(s.channel);
            }
            if(!selectVersionsByValue(s.current_version,s.target_version)){
                throw new Error('版本列表已变化，找不到原版本，请手动选择');
            }
        }
        showTaskCenter();
        showToast('已回填上次的构建参数，确认后点“开始构建”','success');
    }catch(err){
        showToast(err.message||'重试失败','error');
    }
}

function applyV7Images(images){
    v7Tags=[];
    (images||[]).forEach(t=>{
        const v=String(t||'').trim();
        if(v&&!v7Tags.includes(v))v7Tags.push(v);
    });
    renderV7Tags();
}

function selectVersionsByValue(currentValue,targetValue){
    if(!currentPicker||!currentValue)return false;
    if(!currentPicker.chooseByValue(currentValue))return false;
    if(targetValue&&targetPicker)targetPicker.chooseByValue(targetValue);
    return true;
}

function viewTaskFromOverview(taskId){
    if(!taskId)return;
    setTaskInUrl(taskId);
    resumeBuildTask(taskId);
}

async function cancelTaskFromOverview(taskId,btn){
    if(!taskId)return;
    const original=btn.textContent;
    btn.disabled=true;btn.textContent='取消中...';
    try{
        const r=await fetch(`/build/${encodeURIComponent(taskId)}/cancel`,{method:'POST'});
        let d={};
        try{d=await r.json()}catch(e){}
        if(!r.ok||!d.success)throw new Error(d.message||'取消请求失败');
        showToast('取消请求已提交');
        refreshTaskOverview();
    }catch(err){
        showToast(err.message||'取消请求失败','error');
        btn.disabled=false;btn.textContent=original;
    }
}

function setCompletionActions(packageName){
    const download=document.getElementById('btnDownloadPackage');
    const back=document.getElementById('btnBackToTasks');
    if(packageName){
        download.href=`/download-existing/${encodeURIComponent(packageName)}`;
        download.style.display='inline-block';
    }else{
        download.style.display='none';
        download.removeAttribute('href');
    }
    back.style.display='inline-block';
}

function hideCompletionActions(){
    const download=document.getElementById('btnDownloadPackage');
    const back=document.getElementById('btnBackToTasks');
    download.style.display='none';
    download.removeAttribute('href');
    back.style.display='none';
}

function showTaskCenter(){
    if(isBuilding)return;
    document.getElementById('buildCard').classList.remove('active');
    setLogExpanded(false);
    hideCompletionActions();
    refreshTaskOverview();
}

/* ================================================================
   PACKAGES
   ================================================================ */
async function fetchExistingFiles(){
    const fl=document.getElementById('filesLoading'),nf=document.getElementById('noFilesMessage'),fs=document.getElementById('filesList');
    fl.style.display='flex';nf.style.display='none';fs.innerHTML='';
    try{
        const r=await fetch('/existing-packages'),d=await r.json();fl.style.display='none';
        if(d.success&&d.files&&d.files.length>0){existingFilesData=d.files;renderExistingFiles(d.files);updateFilesSummary(d.files.length);newPackageName=''}
        else{existingFilesData=[];nf.style.display='flex';updateFilesSummary(0);newPackageName=''}
        updateV6Preview();
    }catch(e){fl.style.display='none';existingFilesData=[];nf.style.display='flex';updateFilesSummary(0);updateV6Preview()}
}
function renderExistingFiles(files){
    const fl=document.getElementById('filesList'),nf=document.getElementById('noFilesMessage');
    if(!files||files.length===0){nf.style.display='flex';fl.innerHTML='';return}
    nf.style.display='none';
    fl.innerHTML=files.map(f=>{
        const m=f.name.match(/deepflow_patch_v(\d+)_(.+)_(.+)\.tar\.gz/);
        const custom=f.name.match(/deepflow_patch_custom_(.+)\.tar\.gz/);
        const d=m?m[2]+' → '+m[3].replace(/_/g,'-'):(custom?'自定义 · '+custom[1]:f.name.replace('deepflow_patch_','').replace('.tar.gz',''));
        const code=m?'v'+m[1]:(custom?'custom':'');
        const codeChip=code?`<span class="pkg-code">${code}</span>`:'';
        const mb=(f.size/1048576).toFixed(1);
        const badge=(f.name===newPackageName)?'<span class="pkg-new">NEW</span>':'';
        return`<div class="pkg-item"><div class="pkg-info"><div class="pkg-name" title="${escapeHtml(f.name)}">${codeChip}${escapeHtml(d)}${badge}</div><div class="pkg-meta">${mb} MB</div></div><div class="pkg-actions"><button type="button" class="pkg-action-btn secondary" title="复制跳板机 curl 下载命令" data-copy-cmd="${escapeHtml(f.name)}">${ICON_COPY}</button><a class="pkg-action-btn" href="/download-existing/${encodeURIComponent(f.name)}" onclick="return checkExistingFileDownload(event,this)">${ICON_DOWNLOAD} 下载</a></div></div>`;
    }).join('');
}
function updateFilesSummary(n){
    const s=document.getElementById('filesSummary');
    if(s)s.textContent=n+' 个包';
    const hb=document.getElementById('headerPackagesCount');
    if(hb)hb.textContent=n;
    const trigger=document.getElementById('btnToggleDrawer');
    if(trigger)trigger.setAttribute('aria-label',`查看已有升级包（${n} 个）`);
}

function openDrawer(){
    const d=document.getElementById('packagesDrawer');
    const o=document.getElementById('drawerOverlay');
    const trigger=document.getElementById('btnToggleDrawer');
    if(!d)return;
    lastFocusBeforeDrawer=document.activeElement;
    const shell=document.querySelector('.app-shell');
    if(shell)shell.inert=true;
    d.inert=false;d.removeAttribute('inert');
    d.classList.add('open');d.setAttribute('aria-hidden','false');
    if(trigger)trigger.setAttribute('aria-expanded','true');
    if(o)o.classList.add('open');
    setTimeout(()=>{const close=document.getElementById('btnCloseDrawer');if(close)close.focus()},50);
}

function closeDrawer(){
    const d=document.getElementById('packagesDrawer');
    const o=document.getElementById('drawerOverlay');
    const trigger=document.getElementById('btnToggleDrawer');
    const wasOpen=!!(d&&d.classList.contains('open'));
    if(d){
        d.classList.remove('open');d.setAttribute('aria-hidden','true');d.inert=true;
    }
    if(trigger)trigger.setAttribute('aria-expanded','false');
    if(o)o.classList.remove('open');
    const shell=document.querySelector('.app-shell');
    if(shell)shell.inert=false;
    if(wasOpen&&lastFocusBeforeDrawer&&lastFocusBeforeDrawer.focus){
        lastFocusBeforeDrawer.focus();
    }
    lastFocusBeforeDrawer=null;
}
function filterExistingFiles(){
    const q=document.getElementById('fileSearchInput').value.toLowerCase();
    renderExistingFiles(q?existingFilesData.filter(f=>f.name.toLowerCase().includes(q)):existingFilesData);
}
async function checkExistingFileDownload(e,el){
    e.preventDefault();
    try{
        const r=await fetch(el.href,{method:'HEAD'});
        if(r.ok){window.location.href=el.href;return false}
        fetchExistingFiles();
        showToast('文件已不存在，列表已刷新','error');
    }catch(err){
        showToast('下载失败，请检查网络','error');
    }
    return false;
}

const V7_IMAGE_RE=/^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$/;

/* ================================================================
   V7 TAGS & BATCH MODAL
   ================================================================ */
let v7Tags=[];

function renderV7Tags(){
    const listEl=document.getElementById('v7TagList');
    const badgeEl=document.getElementById('v7TagCountBadge');
    const clearBtn=document.getElementById('btnClearAllTags');
    const hiddenInput=document.getElementById('v7ImageList');
    if(!listEl)return;

    listEl.innerHTML=v7Tags.map((tag,idx)=>{
        const isValid=V7_IMAGE_RE.test(tag);
        if(isValid){
            const colonIdx=tag.lastIndexOf(':');
            const name=tag.slice(0,colonIdx);
            const version=tag.slice(colonIdx+1);
            return `<div class="tag-pill" data-index="${idx}" title="${escapeHtml(tag)}">`+
                `<span class="tag-pill-name">${escapeHtml(name)}</span>`+
                `<span class="tag-pill-colon">:</span>`+
                `<span class="tag-pill-version">${escapeHtml(version)}</span>`+
                `<button type="button" class="tag-pill-remove" data-action="remove-tag" data-index="${idx}" aria-label="移除镜像 ${escapeHtml(tag)}" title="移除">`+
                    `<svg viewBox="0 0 16 16"><path d="M3.7 3 3 3.7 7.3 8 3 12.3l.7.7L8 8.7l4.3 4.3.7-.7L8.7 8 13 3.7 12.3 3 8 7.3 3.7 3Z"/></svg>`+
                `</button>`+
            `</div>`;
        }else{
            return `<div class="tag-pill invalid" data-index="${idx}" title="格式错误：必须为 name:tag">`+
                `<span class="tag-pill-name">${escapeHtml(tag)}</span>`+
                `<span class="tag-pill-err-tag">格式错误</span>`+
                `<button type="button" class="tag-pill-remove" data-action="remove-tag" data-index="${idx}" aria-label="移除错误项" title="移除">`+
                    `<svg viewBox="0 0 16 16"><path d="M3.7 3 3 3.7 7.3 8 3 12.3l.7.7L8 8.7l4.3 4.3.7-.7L8.7 8 13 3.7 12.3 3 8 7.3 3.7 3Z"/></svg>`+
                `</button>`+
            `</div>`;
        }
    }).join('');

    const count=v7Tags.length;
    if(badgeEl)badgeEl.textContent=`${count} 个镜像`;
    if(clearBtn)clearBtn.style.display=count>0?'inline-flex':'none';

    if(hiddenInput){
        hiddenInput.value=v7Tags.join('\n');
    }
    updateV7Preview();
    checkBuildBtnStatus();
}

function addV7Tokens(text){
    if(!text)return 0;
    const tokens=String(text).split(/[,，;\n\r\s]+/).map(s=>s.trim()).filter(Boolean);
    if(!tokens.length)return 0;

    let addedCount=0;
    const existingSet=new Set(v7Tags);
    tokens.forEach(tok=>{
        if(!existingSet.has(tok)){
            existingSet.add(tok);
            v7Tags.push(tok);
            addedCount++;
        }
    });

    renderV7Tags();
    return addedCount;
}

function removeV7TagByIndex(idx){
    if(idx>=0&&idx<v7Tags.length){
        v7Tags.splice(idx,1);
        renderV7Tags();
    }
}

function clearAllV7Tags(){
    v7Tags=[];
    renderV7Tags();
}

function openBatchModal(){
    const modal=document.getElementById('batchModalOverlay');
    const textarea=document.getElementById('batchModalTextarea');
    if(!modal||!textarea)return;

    lastFocusBeforeModal=document.activeElement;
    const shell=document.querySelector('.app-shell');
    if(shell)shell.inert=true;
    textarea.value=v7Tags.join('\n');
    updateBatchModalCounter();
    modal.inert=false;modal.removeAttribute('inert');
    modal.classList.add('open');
    modal.setAttribute('aria-hidden','false');
    setTimeout(()=>textarea.focus(),60);
}

function closeBatchModal(){
    const modal=document.getElementById('batchModalOverlay');
    if(!modal)return;
    const wasOpen=modal.classList.contains('open');
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden','true');
    modal.inert=true;
    const shell=document.querySelector('.app-shell');
    if(shell)shell.inert=false;
    if(wasOpen&&lastFocusBeforeModal&&lastFocusBeforeModal.focus){
        lastFocusBeforeModal.focus();
    }
    lastFocusBeforeModal=null;
}

function batchModalValidation(){
    const textarea=document.getElementById('batchModalTextarea');
    const raw=textarea?textarea.value:'';
    const tokens=String(raw).split(/[,，;\n\r\s]+/).map(s=>s.trim()).filter(Boolean);
    const valid=[],invalid=[],seen=new Set();
    let duplicates=0;
    tokens.forEach(t=>{
        if(!V7_IMAGE_RE.test(t)){invalid.push(t);return}
        if(seen.has(t)){duplicates++;return}
        seen.add(t);valid.push(t);
    });
    return {valid,invalid,duplicates};
}

function updateBatchModalCounter(){
    const counter=document.getElementById('batchModalCounter');
    const hint=document.getElementById('batchModalInvalid');
    const confirmBtn=document.getElementById('btnConfirmBatchModal');
    if(!counter)return;

    const {valid,invalid,duplicates}=batchModalValidation();
    counter.classList.toggle('has-error',invalid.length>0);
    if(!valid.length&&!invalid.length){
        counter.textContent='共 0 个镜像';
    }else if(invalid.length){
        counter.textContent=`${valid.length} 有效 / ${invalid.length} 无效`;
    }else{
        counter.textContent=`共 ${valid.length} 个镜像${duplicates?`（重复 ${duplicates} 项已忽略）`:''}`;
    }

    if(hint){
        if(invalid.length){
            const shown=invalid.slice(0,4).map(x=>`「${escapeHtml(x)}」`).join(' ');
            hint.innerHTML=`<span class="modal-invalid-title">格式无效，需为 name:tag：</span>${shown}`
                +(invalid.length>4?` 等 ${invalid.length} 项`:'');
            hint.hidden=false;
        }else{
            hint.hidden=true;hint.innerHTML='';
        }
    }

    // 有非法行时直接挡住确认，避免"导入了才发现错"
    if(confirmBtn)confirmBtn.disabled=invalid.length>0||valid.length===0;
}

function confirmBatchModal(){
    const textarea=document.getElementById('batchModalTextarea');
    if(!textarea)return;
    const {valid,invalid}=batchModalValidation();
    if(invalid.length){
        updateBatchModalCounter();
        showToast(`有 ${invalid.length} 行格式无效，请修正后再导入`,'error');
        return;
    }
    if(!valid.length){showToast('请至少输入一个镜像','error');return}
    v7Tags=[];
    addV7Tokens(valid.join('\n'));
    closeBatchModal();
    showToast(`已同步 ${v7Tags.length} 个镜像胶囊`,'success');
}

function parseV7Images(raw){
    const tokens=String(raw||'').split(/[,，;\n\r]+/).map(s=>s.trim()).filter(Boolean);
    const valid=[],invalid=[],seen=new Set();let duplicates=0;
    tokens.forEach(t=>{
        if(!V7_IMAGE_RE.test(t)){invalid.push(t);return}
        if(seen.has(t)){duplicates++;return}
        seen.add(t);valid.push(t);
    });
    return {valid,invalid,duplicates};
}

function updateV7Preview(){
    const el=document.getElementById('v7Preview');
    if(!el)return;
    const {valid,invalid,duplicates}=parseV7Images(document.getElementById('v7ImageList').value);
    el.classList.toggle('has-error',invalid.length>0);
    if(!valid.length&&!invalid.length){el.textContent='格式：name:tag，每行一个或逗号分隔';return}
    if(invalid.length){
        const shown=invalid.slice(0,2).join('、')+(invalid.length>2?' 等':'');
        el.textContent=`${valid.length?`已识别 ${valid.length} 个，`:''}${invalid.length} 个格式无效：${shown}（需包含 :tag）`;
        return;
    }
    el.textContent=`✓ 已识别 ${valid.length} 个有效镜像${duplicates?`（已自动去重 ${duplicates} 个）`:''}`;
}

function initV7InputListeners(){
    const container=document.getElementById('v7TagContainer');
    const inlineInput=document.getElementById('v7TagInlineInput');
    const tagList=document.getElementById('v7TagList');

    if(container&&inlineInput){
        container.addEventListener('click',e=>{
            if(!e.target.closest('.tag-pill-remove')){
                inlineInput.focus();
            }
        });

        inlineInput.addEventListener('keydown',e=>{
            if(e.key==='Enter'||e.key===','||e.key==='，'||e.key===';'||e.key==='；'){
                e.preventDefault();
                const val=inlineInput.value.trim();
                if(val){
                    addV7Tokens(val);
                    inlineInput.value='';
                }
            }else if(e.key==='Backspace'&&!inlineInput.value&&v7Tags.length>0){
                v7Tags.pop();
                renderV7Tags();
            }
        });

        inlineInput.addEventListener('paste',e=>{
            const pasted=(e.clipboardData||window.clipboardData).getData('text');
            if(pasted){
                e.preventDefault();
                addV7Tokens(pasted);
                inlineInput.value='';
            }
        });

        inlineInput.addEventListener('blur',()=>{
            const val=inlineInput.value.trim();
            if(val){
                addV7Tokens(val);
                inlineInput.value='';
            }
        });
    }

    if(tagList){
        tagList.addEventListener('click',e=>{
            const btn=e.target.closest('[data-action="remove-tag"]');
            if(btn){
                e.stopPropagation();
                const idx=parseInt(btn.dataset.index,10);
                removeV7TagByIndex(idx);
            }
        });
    }

    const btnClearAll=document.getElementById('btnClearAllTags');
    if(btnClearAll)btnClearAll.addEventListener('click',clearAllV7Tags);

    const btnFillSample=document.getElementById('btnFillSampleV7');
    if(btnFillSample){
        btnFillSample.addEventListener('click',()=>{
            addV7Tokens('deepflow-server:v6.6.5550\ndeepflow-agent:v6.6.5617\npcap:v6.6.220');
        });
    }

    const btnOpenModal=document.getElementById('btnOpenBatchModal');
    if(btnOpenModal)btnOpenModal.addEventListener('click',openBatchModal);

    const btnCloseModal=document.getElementById('btnCloseBatchModal');
    if(btnCloseModal)btnCloseModal.addEventListener('click',closeBatchModal);

    const btnCancelModal=document.getElementById('btnCancelBatchModal');
    if(btnCancelModal)btnCancelModal.addEventListener('click',closeBatchModal);

    const modalOverlay=document.getElementById('batchModalOverlay');
    if(modalOverlay){
        modalOverlay.addEventListener('click',e=>{
            if(e.target===modalOverlay)closeBatchModal();
        });
    }

    const modalTextarea=document.getElementById('batchModalTextarea');
    if(modalTextarea){
        modalTextarea.addEventListener('input',updateBatchModalCounter);
    }

    const btnModalClear=document.getElementById('btnModalClear');
    if(btnModalClear&&modalTextarea){
        btnModalClear.addEventListener('click',()=>{
            modalTextarea.value='';
            updateBatchModalCounter();
            modalTextarea.focus();
        });
    }

    const btnConfirmModal=document.getElementById('btnConfirmBatchModal');
    if(btnConfirmModal)btnConfirmModal.addEventListener('click',confirmBatchModal);
}

function trapFocus(container,e){
    if(!container)return;
    const selectors='a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';
    const items=Array.from(container.querySelectorAll(selectors)).filter(el=>el.getClientRects().length>0);
    if(!items.length){e.preventDefault();return}
    const first=items[0],last=items[items.length-1],active=document.activeElement;
    if(e.shiftKey&&(active===first||!container.contains(active))){
        e.preventDefault();last.focus();
    }else if(!e.shiftKey&&(active===last||!container.contains(active))){
        e.preventDefault();first.focus();
    }
}

/* ================================================================
   LISTENERS
   ================================================================ */
function initEventListeners(){
    document.getElementById('modeToggle').addEventListener('change',handleModeChange);
    const channelSelect=document.getElementById('channelSelect');
    if(channelSelect)channelSelect.addEventListener('change',handleChannelChange);
    document.getElementById('v7ImageList').addEventListener('input',()=>{updateV7Preview();checkBuildBtnStatus()});
    document.getElementById('buildForm').addEventListener('submit',startBuildProcess);
    document.getElementById('v7BuildBtn').addEventListener('click',startBuildProcess);
    document.getElementById('fileSearchInput').addEventListener('input',debounce(filterExistingFiles,200));
    document.getElementById('btnToggleLog').addEventListener('click',()=>setLogExpanded(!logExpanded));
    document.getElementById('btnCancelBuild').addEventListener('click',handleCancelBuild);
    document.getElementById('btnRefreshOverview').addEventListener('click',refreshTaskOverview);
    document.getElementById('activeTaskList').addEventListener('click',handleActiveTaskClick);
    document.getElementById('recentTaskList').addEventListener('click',handleRecentTaskClick);
    document.getElementById('btnBackToTasks').addEventListener('click',showTaskCenter);
    document.getElementById('btnDownloadPackage').addEventListener('click',e=>checkExistingFileDownload(e,e.currentTarget));
    const copyLogBtn=document.getElementById('btnCopyLog');
    if(copyLogBtn)copyLogBtn.addEventListener('click',copyLogsToClipboard);
    const clearLogBtn=document.getElementById('btnClearLog');
    if(clearLogBtn)clearLogBtn.addEventListener('click',clearLogScreen);
    const filesList=document.getElementById('filesList');
    if(filesList){
        filesList.addEventListener('click',e=>{
            const btn=e.target.closest('[data-copy-cmd]');
            if(btn){
                e.preventDefault();
                copyCurlCommand(btn.dataset.copyCmd);
            }
        });
    }
    const btnToggleDrawer=document.getElementById('btnToggleDrawer');
    if(btnToggleDrawer)btnToggleDrawer.addEventListener('click',openDrawer);
    const btnCloseDrawer=document.getElementById('btnCloseDrawer');
    if(btnCloseDrawer)btnCloseDrawer.addEventListener('click',closeDrawer);
    const drawerOverlay=document.getElementById('drawerOverlay');
    if(drawerOverlay)drawerOverlay.addEventListener('click',closeDrawer);
    document.addEventListener('keydown',e=>{
        const modal=document.getElementById('batchModalOverlay');
        if(modal&&modal.classList.contains('open')){
            if(e.key==='Escape'){closeBatchModal();return}
            if(e.key==='Tab'){trapFocus(modal.querySelector('.modal-dialog'),e);return}
        }
        const drawer=document.getElementById('packagesDrawer');
        if(drawer&&drawer.classList.contains('open')){
            if(e.key==='Escape'){closeDrawer();return}
            if(e.key==='Tab'){trapFocus(drawer,e);return}
        }
    });

    initV7InputListeners();
    renderV7Tags();
}

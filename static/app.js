/* DeepFlow 升级包构建 — frontend logic */
/* ================================================================
   STATE
   ================================================================ */
let versionData=[],packageNameTemplate='',eventSource=null,currentSeries='v6',isBuilding=false;
let existingFilesData=[],logExpanded=false,lastLogSeq=0,currentTaskId='',sseRetries=0;
const TASK_QUERY_KEY='task_id';
const ICON_DOWNLOAD='<svg class="icon icon-sm" viewBox="0 0 16 16" aria-hidden="true"><path d="M7.25 1h1.5v7.1l2.6-2.6 1.05 1.05L8 11.5 3.6 7.1 4.65 6.05l2.6 2.6V1ZM2 13h12v2H2v-2Z"/></svg>';
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
document.addEventListener('DOMContentLoaded',()=>{
    initVersionPickers();initEventListeners();setMode(currentSeries);
    fetchVersions(currentSeries);fetchExistingFiles();
    // 刷新后凭 URL 中的 task_id 恢复正在进行的构建
    const taskId=new URLSearchParams(location.search).get(TASK_QUERY_KEY);
    if(taskId)resumeBuildTask(taskId);
});

/* ================================================================
   MODE
   ================================================================ */
function setMode(m){
    currentSeries=m;document.body.dataset.mode=m;
    document.querySelectorAll('#modeToggle input[name="buildMode"]').forEach(r=>{r.checked=r.value===m});
    document.getElementById('v6Workspace').classList.toggle('active',m==='v6');
    document.getElementById('v7Workspace').classList.toggle('active',m==='v7');
    if(!isBuilding)checkBuildBtnStatus();
    updateV7Preview();
}
function handleModeChange(e){
    const m=e.target&&e.target.value;
    if(e.target.name!=='buildMode'||!m||m===currentSeries)return;
    setMode(m);
    if(m==='v6'&&versionData.length===0)fetchVersions('v6');
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

async function fetchVersions(series){
    if(series!=='v6'){if(versionData.length>0)return;renderVersionOptions(true);return}
    if(currentPicker)currentPicker.setOptions([], '加载中...');
    try{
        const r=await fetch(`/versions?series=${encodeURIComponent(series)}`);
        if(currentSeries!==series)return;
        const d=await r.json();
        if(d.success&&d.versions){versionData=d.versions;packageNameTemplate=d.package_name_template||'';renderVersionOptions()}
        else{versionData=[];renderVersionOptions(true)}
    }catch(e){if(currentSeries!==series)return;versionData=[];renderVersionOptions(true)}
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
    el.textContent='';
    const cur=currentPicker?currentPicker.getSelected():null;
    const tgt=targetPicker?targetPicker.getSelected():null;
    if(!cur||!tgt)return;

    const span=Number(tgt.seq_num)-Number(cur.seq_num);
    const from=String(cur.value).split('-')[0];
    const to=String(tgt.value).split('-')[0];
    const expected=packageNameTemplate
        ? packageNameTemplate.replace('{current}',from).replace('{target}',to)
        : '';
    const existing=expected?(existingFilesData||[]).find(f=>f.name===expected):null;

    const info=document.createElement('span');
    info.textContent=`将跨越 ${span} 个 patch`;
    el.appendChild(info);
    if(existing){
        const link=document.createElement('a');
        link.href=`/download-existing/${encodeURIComponent(existing.name)}`;
        link.textContent='已有同名升级包，可直接下载';
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
    el.classList.toggle('expanded',logExpanded);
    document.getElementById('btnToggleLog').textContent=logExpanded?'收起日志':'展开日志';
    if(logExpanded)requestAnimationFrame(()=>{el.scrollTop=el.scrollHeight});
}
function currentProgressPercent(){
    const w=parseFloat(document.getElementById('progressBar').style.width);
    return Number.isFinite(w)?w:0;
}
function finishBuild(){
    isBuilding=false;
    window.removeEventListener('beforeunload',beforeUnloadHandler);
    if(eventSource){eventSource.close();eventSource=null}
    resetBuildButtons();
    setTaskInUrl('');
    currentTaskId='';
    checkBuildBtnStatus();
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
        payload={series:'v7',images:parsed.valid};
    }else{
        payload={series:'v6',current:cv,target:tv};
    }

    isBuilding=true;sseRetries=0;window.addEventListener('beforeunload',beforeUnloadHandler);
    currentTaskId='';showCancelButton(true,false);setBuildButtonsBusy();
    const bc=document.getElementById('buildCard'),bl=document.getElementById('buildLog');
    bc.classList.add('active');bl.innerHTML='';setLogExpanded(true);lastLogSeq=0;
    document.getElementById('taskStateText').textContent='排队中';
    document.getElementById('taskMessageText').textContent='';
    updateProgressBar(0,'');checkBuildBtnStatus();
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
    currentTaskId=taskId;isBuilding=true;sseRetries=0;
    window.addEventListener('beforeunload',beforeUnloadHandler);
    setBuildButtonsBusy();showCancelButton(true,false);
    const bc=document.getElementById('buildCard'),bl=document.getElementById('buildLog');
    bc.classList.add('active');bl.innerHTML='';setLogExpanded(true);lastLogSeq=0;
    document.getElementById('taskStateText').textContent='恢复任务中';
    document.getElementById('taskMessageText').textContent='';
    updateProgressBar(0,'');checkBuildBtnStatus();
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
    isBuilding=false;
    window.removeEventListener('beforeunload',beforeUnloadHandler);
    resetBuildButtons();checkBuildBtnStatus();
    document.getElementById('taskStateText').textContent='连接中断';
    document.getElementById('taskMessageText').textContent='任务可能仍在运行，刷新页面可恢复查看';
    showToast('与构建服务的连接中断，刷新页面可恢复任务状态','error');
}

function resetBuildState(){
    resetBuildButtons();
    isBuilding=false;window.removeEventListener('beforeunload',beforeUnloadHandler);
    lastLogSeq=0;sseRetries=0;currentTaskId='';showCancelButton(false);
    if(eventSource){eventSource.close();eventSource=null}
    document.getElementById('buildCard').classList.remove('active');
    setLogExpanded(false);
    setTaskInUrl('');
    checkBuildBtnStatus();
}

let newPackageName='';
function handleBuildStatus(d){
    const st=document.getElementById('taskStateText');
    const mt=document.getElementById('taskMessageText'),logs=Array.isArray(d.logs)?d.logs:[];
    // 刷新恢复任务时，用任务类型校准当前面板
    if((d.task_type==='v6'||d.task_type==='v7')&&d.task_type!==currentSeries)setMode(d.task_type);
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
    switch(d.status){
        case'queued':st.textContent='排队中';updateProgressBar(d.percent||0,'');mt.textContent=d.message||'';break;
        case'progress':st.textContent='构建中';updateProgressBar(d.percent,'');mt.textContent=d.message||'';break;
        case'complete':
            st.textContent='已完成';updateProgressBar(100,'success');mt.textContent='升级包已加入右侧列表';
            newPackageName=d.package_name||'';
            showCancelButton(false);
            finishBuild();
            fetchExistingFiles();
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
            if(logEl.childElementCount>0)setLogExpanded(true);
            showToast(d.cancelled?'构建已取消':(d.message||'构建失败'),'error');
            break;
        }
    }
}

/* ================================================================
   PROGRESS & LOG
   ================================================================ */
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
        const d=m?m[2]+' → '+m[3].replace(/_/g,'-'):f.name.replace('deepflow_patch_','').replace('.tar.gz','');
        const mb=(f.size/1048576).toFixed(1);
        const badge=(f.name===newPackageName)?'<span class="pkg-new">NEW</span>':'';
        return`<div class="pkg-item"><div class="pkg-info"><div class="pkg-name" title="${escapeHtml(f.name)}">${escapeHtml(d)}${badge}</div><div class="pkg-meta">${mb} MB</div></div><a class="pkg-download" href="/download-existing/${encodeURIComponent(f.name)}" onclick="return checkExistingFileDownload(event,this)">${ICON_DOWNLOAD} 下载</a></div>`;
    }).join('');
}
function updateFilesSummary(n){document.getElementById('filesSummary').textContent=n+' 个包'}
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
        el.textContent=`${valid.length?`已识别 ${valid.length} 个，`:''}${invalid.length} 行无效：${shown}（需为 name:tag）`;
        return;
    }
    el.textContent=`已识别 ${valid.length} 个镜像${duplicates?`，忽略 ${duplicates} 个重复项`:''}`;
}

/* ================================================================
   LISTENERS
   ================================================================ */
function initEventListeners(){
    document.getElementById('modeToggle').addEventListener('change',handleModeChange);
    document.getElementById('v7ImageList').addEventListener('input',()=>{updateV7Preview();checkBuildBtnStatus()});
    document.getElementById('buildForm').addEventListener('submit',startBuildProcess);
    document.getElementById('v7BuildBtn').addEventListener('click',startBuildProcess);
    document.getElementById('fileSearchInput').addEventListener('input',debounce(filterExistingFiles,200));
    document.getElementById('btnToggleLog').addEventListener('click',()=>setLogExpanded(!logExpanded));
    document.getElementById('btnCancelBuild').addEventListener('click',handleCancelBuild);
}

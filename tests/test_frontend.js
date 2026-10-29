/* Run with: node --test tests/test_frontend.js (no npm packages required). */
const assert=require('node:assert/strict');
const {test}=require('node:test');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const os=require('node:os');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execFileAsync=promisify(execFile);
const source=fs.readFileSync(path.join(__dirname,'..','static','app.js'),'utf8');

function element(){
    const classes=new Set(),attributes=new Map();
    return {
        value:'',textContent:'',innerHTML:'',style:{width:'0%'},dataset:{},disabled:false,hidden:false,
        children:[],scrollHeight:0,scrollTop:0,clientHeight:0,
        classList:{
            add(...names){names.forEach(n=>classes.add(n))},
            remove(...names){names.forEach(n=>classes.delete(n))},
            contains(name){return classes.has(name)},
            toggle(name,force){const set=force===undefined?!classes.has(name):!!force;set?classes.add(name):classes.delete(name);return set}
        },
        setAttribute(name,value){attributes.set(name,value)},removeAttribute(name){attributes.delete(name)},
        appendChild(child){this.children.push(child);return child},append(...children){this.children.push(...children)},
        get lastElementChild(){return this.children.at(-1)||null},get childElementCount(){return this.children.length},
        querySelector(){return null},querySelectorAll(){return []},addEventListener(){},focus(){},remove(){}
    };
}

function harness(){
    const elements=new Map(),timers=new Map(),streams=[],toasts=[],modeInputs=[element(),element()];
    modeInputs[0].value='v6';modeInputs[1].value='v7';
    let nextTimer=1;
    const location={href:'http://localhost/?task_id=task-a',origin:'http://localhost',search:'?task_id=task-a'};
    const document={
        body:element(),hidden:false,addEventListener(){},
        getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},
        createElement(){return element()},querySelector(){return null},
        querySelectorAll(selector){return selector.includes('buildMode')?modeInputs:[]}
    };
    const context=vm.createContext({
        console,document,location,URL,URLSearchParams,AbortController,
        window:{location,isSecureContext:false,addEventListener(){},removeEventListener(){}},
        history:{replaceState(_state,_title,url){location.href=new URL(url,location.href).href}},
        setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,delay});return id},
        clearTimeout(id){timers.delete(id)},setInterval(){return nextTimer++},clearInterval(){},
        requestAnimationFrame(fn){fn()},
        fetch(){throw new Error('Unexpected network request')},
        EventSource:class{constructor(url){this.url=url;this.closed=false;streams.push(this)}close(){this.closed=true}}
    });
    const run=code=>vm.runInContext(code,context);
    run(source);
    context.recordToast=message=>toasts.push(message);
    run('showToast=recordToast');
    return {
        context,run,elements,document,timers,streams,toasts,modeInputs,location,
        fireTimer(id){const timer=timers.get(id);assert.ok(timer,`Missing timer ${id}`);timers.delete(id);return timer.fn()},
        deadline(){return [...timers].find(([,t])=>t.delay===10000)?.[0]},
        stop(){run('stopBuildObservation()')}
    };
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const jsonResponse=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
const activeStatus={task_id:'task-a',task_type:'v6',status:'progress',complete:false,percent:35,message:'pulling',logs:[],log_seq:0};

test('download follows normal navigation without a cross-origin HEAD request',()=>{
    const h=harness();let prevented=false;
    const result=h.context.checkExistingFileDownload({preventDefault(){prevented=true}}, {href:'http://localhost/download-existing/probe.tar.gz'});
    assert.equal(result,false);assert.equal(prevented,true);
    assert.equal(h.location.href,'http://localhost/download-existing/probe.tar.gz');
    assert.equal(h.context.checkExistingFileDownload({ctrlKey:true,preventDefault(){throw new Error('Modified click prevented')}},{href:'/other'}),true);
});

test('copied curl command downloads the redirected archive and rejects HTTP errors',async()=>{
    const h=harness();let command;
    h.context.copyTextToClipboard=text=>{command=text};
    const archive=Buffer.from('archive-content'),requests=[];
    const server=http.createServer((req,res)=>{
        requests.push(req.url);
        if(req.url==='/download-existing/probe.tar.gz'){res.writeHead(302,{Location:'/object'});res.end('redirect html')}
        else if(req.url==='/object'){res.writeHead(200);res.end(archive)}
        else{res.writeHead(404);res.end('missing')}
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    h.context.window.location.origin=`http://127.0.0.1:${server.address().port}`;
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'packing-frontend-'));
    try{
        h.context.copyCurlCommand('probe.tar.gz');
        const match=command.match(/^curl -fLO "([^"]+)"$/);assert.ok(match);
        await execFileAsync('curl',['-fLO',match[1]],{cwd:directory});
        assert.deepEqual(fs.readFileSync(path.join(directory,'probe.tar.gz')),archive);
        assert.deepEqual(requests,['/download-existing/probe.tar.gz','/object']);
        h.context.copyCurlCommand('missing.tar.gz');
        const missing=command.match(/^curl -fLO "([^"]+)"$/)[1];
        await assert.rejects(execFileAsync('curl',['-fLO',missing],{cwd:directory}),error=>error.code===22);
    }finally{await new Promise(resolve=>server.close(resolve));fs.rmSync(directory,{recursive:true,force:true})}
});

test('building locks both mode and channel and rejects a channel change',()=>{
    const h=harness();h.run("isBuilding=true;currentChannel='6.6.9';setModeLocked(true)");
    assert.equal(h.document.getElementById('channelSelect').disabled,true);
    assert.ok(h.modeInputs.every(input=>input.disabled));
    const target={value:'7.1.9'};h.context.handleChannelChange({target});
    assert.equal(target.value,'6.6.9');assert.equal(h.run('currentChannel'),'6.6.9');
    h.run('isBuilding=false;setModeLocked(false)');
    assert.equal(h.document.getElementById('channelSelect').disabled,false);
});

test('lost SSE keeps the task and build controls locked until status is known',()=>{
    const h=harness(),requests=[];
    h.context.fetch=(url,options)=>{requests.push({url,options});return new Promise(()=>{})};
    h.run("isBuilding=true;currentTaskId='task-a';lastLogSeq=7;handleStreamLost()");
    assert.equal(h.run('isBuilding'),true);assert.equal(h.run('currentTaskId'),'task-a');
    assert.equal(h.document.getElementById('v6BuildBtn').disabled,true);
    assert.equal(h.document.getElementById('channelSelect').disabled,true);
    assert.ok(h.location.href.includes('task_id=task-a'));
    assert.equal(requests[0].url,'/task-status/task-a?since=7');
    h.stop();assert.equal(requests[0].options.signal.aborted,true);
});

test('fallback status polling handles progress and completion using the normal terminal flow',async()=>{
    const h=harness(),requests=[];
    const complete={...activeStatus,status:'complete',complete:true,percent:100,package_name:'result.tar.gz',oss_object_name:'result_abcdef123456.tar.gz'};
    h.context.fetch=async url=>{requests.push(url);return jsonResponse({success:true,status:requests.length===1?activeStatus:complete})};
    h.run('refreshTaskOverview=()=>{};fetchExistingFiles=()=>{}');
    h.run("isBuilding=true;currentTaskId='task-a';handleStreamLost()");await flush();
    assert.equal(h.run('isBuilding'),true);assert.equal(h.document.getElementById('taskStateText').textContent,'构建中');
    h.fireTimer(h.run('taskRecoveryTimer'));await flush();
    assert.equal(requests.length,2);assert.equal(h.run('isBuilding'),false);assert.equal(h.run('currentTaskId'),'');
    assert.equal(h.document.getElementById('taskStateText').textContent,'已完成');
    assert.equal(h.document.getElementById('btnDownloadPackage').href,'/download-existing/result_abcdef123456.tar.gz');
    assert.equal(h.document.getElementById('channelSelect').disabled,false);
    assert.equal(h.run('taskRecoveryTimer'),null);assert.ok(!h.location.href.includes('task_id='));
});

test('status request timeouts retain the lock and schedule another bounded attempt',async()=>{
    const h=harness();let signal;
    h.context.fetch=(_url,options)=>new Promise((_resolve,reject)=>{
        signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});
    });
    h.run("isBuilding=true;currentTaskId='task-a';handleStreamLost()");
    h.fireTimer(h.deadline());await flush();
    assert.equal(signal.aborted,true);assert.equal(h.run('isBuilding'),true);
    assert.equal(h.run('currentTaskId'),'task-a');assert.ok(h.run('taskRecoveryTimer'));
    h.stop();
});

test('stale polling and SSE events cannot finish a newly selected task',async()=>{
    const h=harness();let resolveOld;
    h.context.fetch=()=>new Promise(resolve=>{resolveOld=resolve});
    h.run("isBuilding=true;currentTaskId='task-a';handleStreamLost()");
    h.context.resumeBuildTask('task-b');
    const sourceB=h.streams.at(-1);
    resolveOld(jsonResponse({success:true,status:{...activeStatus,status:'complete',complete:true}}));await flush();
    assert.equal(h.run('currentTaskId'),'task-b');assert.equal(h.run('isBuilding'),true);
    h.context.resumeBuildTask('task-c');
    const sourceC=h.streams.at(-1);
    sourceB.onmessage({data:JSON.stringify({...activeStatus,task_id:'task-b',status:'complete',complete:true})});
    sourceB.onerror();
    assert.equal(h.run('currentTaskId'),'task-c');assert.equal(sourceC.closed,false);
    h.stop();
});

test('stream transport error messages trigger recovery instead of build failure',()=>{
    const h=harness();h.context.fetch=()=>new Promise(()=>{});
    h.run("isBuilding=true;currentTaskId='task-a';connectBuildStream('/build?task_id=task-a')");
    h.streams[0].onmessage({data:JSON.stringify({status:'error',message:'SSE transport failed',logs:[]})});
    assert.equal(h.run('isBuilding'),true);assert.equal(h.run('currentTaskId'),'task-a');
    assert.equal(h.document.getElementById('taskStateText').textContent,'恢复任务中');h.stop();
});

test('confirmed missing task ends observation and allows another build',async()=>{
    const h=harness();h.context.fetch=async()=>jsonResponse({success:false,message:'任务已过期'},404);
    h.run('refreshTaskOverview=()=>{};fetchExistingFiles=()=>{}');
    h.run("isBuilding=true;currentTaskId='task-a';handleStreamLost()");await flush();
    assert.equal(h.run('isBuilding'),false);assert.equal(h.run('currentTaskId'),'');
    assert.equal(h.document.getElementById('taskMessageText').textContent,'任务已过期');
});

test('overview response body timeout releases the loading guard for the next refresh',async()=>{
    const h=harness();let signal;
    h.context.fetch=async(_url,options)=>{
        signal=options.signal;
        return {ok:true,json:()=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}))};
    };
    const request=h.context.refreshTaskOverview();await flush();
    assert.equal(h.run('overviewLoading'),true);assert.ok(h.deadline());
    h.fireTimer(h.deadline());await request;
    assert.equal(h.run('overviewLoading'),false);assert.equal(h.document.getElementById('taskOverviewError').hidden,false);
    h.context.fetch=async()=>jsonResponse({success:true,active:[],recent:[],system:{}});
    h.document.getElementById('diskFree').parentElement={};
    await h.context.refreshTaskOverview();
    assert.equal(h.run('overviewLoading'),false);assert.equal(h.document.getElementById('taskOverviewError').hidden,true);
});

test('custom image parser accepts registry ports and rejects malformed references',()=>{
    const h=harness();
    const valid=['agent:v1','team-a/agent:v7.1.1','registry.example.com/dev/agent:Tag_1','localhost:5000/team/agent:v1','localhost:65535/agent:v1','repo__name:tag','repo--name:_tag'];
    const invalid=['registry:5000/image','registry:5000/image:','registry:0/image:v1','registry:65536/image:v1','agent:tag:extra','/agent:v1','a//b:v1','a/../b:v1','Agent:v1','agent:-tag','agent:'+ 'x'.repeat(129),'a'.repeat(230)+':tag'];
    for(const reference of valid)assert.equal(h.context.isValidImageReference(reference),true,reference);
    for(const reference of invalid)assert.equal(h.context.isValidImageReference(reference),false,reference);
    const parsed=h.context.parseV7Images(valid[0]+'\n'+valid[0]+'\n'+invalid[0]);
    assert.equal(parsed.valid.length,1);assert.equal(parsed.duplicates,1);assert.equal(parsed.invalid.length,1);
});

test('upload failure retry submits only the upload endpoint and observes the original task',async()=>{
    const h=harness(),requests=[],resumed=[];
    h.context.fetch=async(url,options={})=>{
        requests.push({url,method:options.method||'GET'});
        if(requests.length===1)return jsonResponse({success:true,status:{upload_failed:true,upload_retry_available:true}});
        return jsonResponse({success:true,task_id:'failed-upload'});
    };
    h.context.resumeBuildTask=taskId=>resumed.push(taskId);
    await h.context.retryTaskFromOverview('failed-upload');
    assert.deepEqual(requests,[
        {url:'/task-status/failed-upload',method:'GET'},
        {url:'/build/failed-upload/retry-upload',method:'POST'}
    ]);
    assert.deepEqual(resumed,['failed-upload']);
    const row=h.context.taskRowHtml({task_id:'failed-upload',upload_retry_available:true,complete:true,error:true},false,0);
    assert.ok(row.includes('重试上传'));
});

test('task downloads use their unique OSS object and failed uploads expose no download',()=>{
    const h=harness();
    const successful=h.context.taskRowHtml({task_id:'done',package_name:'result.tar.gz',oss_object_name:'result_abcdef123456.tar.gz',complete:true},false,0);
    assert.ok(successful.includes('/download-existing/result_abcdef123456.tar.gz'));
    const failed=h.context.taskRowHtml({task_id:'failed',package_name:'result.tar.gz',upload_failed:true,error:true,complete:true},false,0);
    assert.ok(!failed.includes('data-download'));
    h.context.renderExistingFiles([{name:'deepflow_patch_v669_01_03_abcdef123456.tar.gz',size:1024}]);
    assert.ok(h.document.getElementById('filesList').innerHTML.includes('01 → 03'));
});

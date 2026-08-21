const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let JOB=null, REPORTS=[], ACTION=null, CAPS={}, POLL=null, RUN=null;
let POLL_FAILS=0, POLL_STARTED=0;
const POLL_MAX_FAILS=8, POLL_MAX_MS=30*60*1000;

const ACTIONS=[
  {id:'clean',   title:'Remove metadata only',  desc:'Strip EXIF, XMP, C2PA and the rest. Nothing else about the file changes.', needs:'any'},
  {id:'split',   title:'Split 2x2 grid',        desc:'Cut a four-panel grid into separate Instagram-ready images.', needs:'image'},
  {id:'video',   title:'Clean and resize video', desc:'Strip metadata and re-encode to Instagram dimensions.', needs:'video'},
  {id:'detect',  title:'Find watermark',        desc:'Locate a burned-in mark and show you where it is.', needs:'video'},
  {id:'cropwm',  title:'Crop watermark out',    desc:'Safer than paint: crop the frame so the mark is gone.', needs:'video'},
  {id:'removelogo',title:'Remove watermark',    desc:'Paint out a mark once you know its position.', needs:'video'},
];

function loadCaps(){
  fetch('/api/capabilities').then(r=>r.json()).then(c=>{
    CAPS=c;
    $('#caps').innerHTML=[
      cap('Images', true),
      cap('Video (ffmpeg)', c.ffmpeg),
      cap('Best watermark removal (ProPainter)', c.propainter),
      cap('Sharper upscaling (Real-ESRGAN)', !!c.realesrgan),
    ].join('');
    if(c.realesrgan_status==='pending') setTimeout(loadCaps, 4000);
  }).catch(()=>{ $('#caps').innerHTML=cap('Could not reach the local server', false); });
}
loadCaps();
const cap=(n,on)=>`<span class="cap"><span class="dot${on?'':' off'}"></span>${n}${on?'':' — not installed'}</span>`;

function apiError(d, fallback){ return (d && d.error) ? d.error : (fallback || 'Something went wrong.'); }
function failMessage(err){
  const s=String(err||'');
  if(/Failed to fetch|NetworkError|Load failed/i.test(s))
    return 'Lost contact with the local server. Is Framewipe still running?';
  return s || 'Something went wrong.';
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

const drop=$('#drop'), picker=$('#picker');
drop.addEventListener('click', ()=>picker.click());
drop.addEventListener('keydown', e=>{
  if(e.key==='Enter' || e.key===' '){ e.preventDefault(); picker.click(); }
});
drop.addEventListener('dragover', e=>{e.preventDefault();drop.classList.add('over')});
drop.addEventListener('dragleave', ()=>drop.classList.remove('over'));
drop.addEventListener('drop', e=>{e.preventDefault();drop.classList.remove('over');upload(e.dataTransfer.files)});
picker.addEventListener('change', e=>upload(e.target.files));

function upload(list){
  if(!list||!list.length) return;
  const fd=new FormData(); let n=0;
  for(const f of list){ fd.append('files',f); n++; }
  $('#upmsg').innerHTML='<div class="msg info">Reading '+n+' file'+(n>1?'s':'')+'…</div>';
  fetch('/api/inspect',{method:'POST',body:fd}).then(async r=>{
    const d=await r.json().catch(()=>({}));
    if(!r.ok || d.error){
      const msg=apiError(d, r.status===413
        ? 'That file is too large. The limit is 4 GB per upload.'
        : 'Upload failed.');
      $('#upmsg').innerHTML='<div class="msg bad">'+esc(msg)+'</div>';
      return;
    }
    $('#upmsg').innerHTML='';
    JOB=d.job; REPORTS=d.reports;
    renderFiles(); renderActions();
    $('#step2').classList.remove('hidden');
    $('#step3').classList.remove('hidden');
    $('#step4').classList.add('hidden');
  }).catch(e=>$('#upmsg').innerHTML='<div class="msg bad">'+esc(failMessage(e))+'</div>');
}

function renderFiles(){
  const imgs=REPORTS.filter(r=>r.kind==='image').length;
  const vids=REPORTS.filter(r=>r.kind==='video').length;
  const dirty=REPORTS.filter(r=>(r.findings||[]).length).length;
  $('#sum2').textContent=[imgs?imgs+' image'+(imgs>1?'s':''):null,
                          vids?vids+' video'+(vids>1?'s':''):null].filter(Boolean).join(', ');
  $('#results').innerHTML=REPORTS.map((r,i)=>{
    const n=(r.findings||[]).length;
    const pill=r.error?'<span class="pill bad">unreadable</span>'
      :n?'<span class="pill warn">'+n+' item'+(n>1?'s':'')+' found</span>'
        :'<span class="pill ok">already clean</span>';
    const opill=originPill(r);
    const meta=[r.dimensions,r.codec,r.size_h,
      r.duration?r.duration.toFixed(1)+'s':null,
      r.audio?'audio: '+r.audio:null].filter(Boolean).join(' · ');
    let body=originBlock(r);
    if(r.error) body+='<div class="msg bad">'+esc(r.error)+'</div>';
    else if(n){
      body+='<table><thead><tr><th>What was found</th><th>Contents</th><th>Size</th></tr></thead><tbody>'+
        r.findings.map(f=>'<tr><td>'+esc(f.label)+'</td><td class="detail">'+
          esc(f.detail||'')+'</td><td class="size">'+(f.bytes?f.bytes+' B':'')+
          '</td></tr>').join('')+'</tbody></table>';
    } else body+='<div class="msg ok">No metadata of any kind. Nothing to remove.</div>';
    return '<div class="file'+(n||r.error?' open':'')+'" data-i="'+i+'">'+
      '<button type="button" class="fhead" aria-expanded="'+(n||r.error?'true':'false')+'"><div class="fleft"><div class="ficon">'+
        (r.kind==='video'?'▶':'▣')+'</div><div><div class="fname">'+esc(r.name)+
        '</div><div class="fmeta">'+esc(meta)+'</div></div></div>'+
      '<div class="fright">'+opill+pill+'<span class="chev" aria-hidden="true">▶</span></div></button>'+
      '<div class="fbody">'+body+'</div></div>';
  }).join('');
  $$('.fhead').forEach(h=>h.onclick=()=>{
    const el=h.parentElement;
    el.classList.toggle('open');
    h.setAttribute('aria-expanded', el.classList.contains('open')?'true':'false');
  });
  let adv='';
  if(dirty) adv='<div class="msg info"><b>'+dirty+' file'+(dirty>1?'s carry':' carries')+
    ' metadata.</b> If all you want is to remove it, choose <b>Remove metadata only</b> '+
    'below — it is lossless and takes a moment.</div>';
  else if(REPORTS.length) adv='<div class="msg ok"><b>Everything is already clean.</b> '+
    'You can still split grids or resize video below.</div>';
  $('#advice').innerHTML=adv;
}

const VERDICT={
  'ai-declared':{cls:'ai',   icon:'\u25C6', pill:'warn', short:'AI-generated'},
  'ai-likely':  {cls:'maybe',icon:'\u25C7', pill:'warn', short:'Possibly AI'},
  'camera-like':{cls:'cam',  icon:'\u25CF', pill:'ok',   short:'Camera photo'},
  'unknown':    {cls:'none', icon:'\u25CB', pill:'',     short:'Origin unknown'}
};
function originPill(r){
  if(!r.origin) return '';
  const v=VERDICT[r.origin.verdict]||VERDICT.unknown;
  const cls=v.pill?('pill '+v.pill):'pill';
  const st=v.pill?'':'background:var(--code);color:var(--muted)';
  return '<span class="'+cls+'" style="'+st+'">'+v.short+'</span>';
}
function originBlock(r){
  if(!r.origin) return '';
  const o=r.origin, v=VERDICT[o.verdict]||VERDICT.unknown;
  let h='<div class="verdict '+v.cls+'"><h4><span class="vicon">'+v.icon+'</span>'+
        esc(o.headline)+'</h4><p>'+esc(o.explain)+'</p>';
  if(o.evidence&&o.evidence.length){
    h+='<div class="ev">'+o.evidence.map(e=>
      '<div class="evrow"><span class="evtag '+e.weight+'">'+
      (e.weight==='declared'?'proof':e.weight==='camera'?'camera':'weak')+
      '</span><span><b>'+esc(e.label)+'</b>'+(e.detail?' — '+esc(e.detail):'')+
      '</span></div>').join('')+'</div>';
  }
  h+='<div class="synth"><b>SynthID:</b><span>'+
     (o.synthid==='declared'?'<b>declared in this file.</b> ':'not declared. ')+
     esc(o.synthid_note)+'</span></div>';
  return h+'</div>';
}

function renderActions(){
  const hasImg=REPORTS.some(r=>r.kind==='image'), hasVid=REPORTS.some(r=>r.kind==='video');
  const dirty=REPORTS.some(r=>(r.findings||[]).length);
  $('#acts').innerHTML=ACTIONS.map(a=>{
    let dis=false, why='';
    if(a.needs==='image'&&!hasImg){dis=true;why='No images loaded'}
    if(a.needs==='video'&&!hasVid){dis=true;why='No videos loaded'}
    if(a.needs==='video'&&hasVid&&!CAPS.ffmpeg){dis=true;why='Needs ffmpeg'}
    const rec=(a.id==='clean'&&dirty)?'<span class="rec">SUGGESTED</span>':'';
    return '<button type="button" role="radio" aria-checked="false" class="act'+(dis?' dis':'')+'" data-a="'+a.id+'"'+(dis?' disabled title="'+why+'"':'')+'>'+
      rec+'<b>'+a.title+'</b><span>'+(dis?why:a.desc)+'</span></button>';
  }).join('');
  $$('.act').forEach(el=>el.onclick=()=>{
    if(el.classList.contains('dis')||el.disabled) return;
    ACTION=el.dataset.a;
    $$('.act').forEach(x=>{x.classList.remove('on'); x.setAttribute('aria-checked','false');});
    el.classList.add('on'); el.setAttribute('aria-checked','true');
    $('#opts').classList.remove('hidden');
    ['clean','split','video','detect','cropwm','removelogo'].forEach(a=>
      $('#opt-'+a).classList.toggle('hidden',a!==ACTION));
    $('#run').textContent = ACTION==='detect' ? 'Find watermark'
      : ACTION==='cropwm' ? 'Suggest crop' : 'Run';
    updateHint();
  });
  ACTION=null; $('#opts').classList.add('hidden');
}

$('#method').onchange=updateHint;
function updateHint(){
  let h='';
  if(ACTION==='removelogo'&&$('#method').value==='propainter')
    h=CAPS.propainter?'ProPainter takes roughly 3–5 minutes for a 10-second clip.'
                     :'ProPainter is not installed — this will fall back to the fast method.';
  if(ACTION==='clean') h='Lossless — pixels stay bit-identical.';
  if(ACTION==='cropwm') h='Does not paint. Uses --suggest-crop from the engine.';
  $('#runhint').textContent=h;
}

function resetUi(){
  JOB=null;REPORTS=[];ACTION=null;RUN=null;
  clearTimeout(POLL);
  ['#step2','#step3','#step4'].forEach(s=>$(s).classList.add('hidden'));
  $('#upmsg').innerHTML=''; picker.value='';
  window.scrollTo({top:0,behavior:'smooth'});
}
$('#reset').onclick=()=>{
  const job=JOB;
  resetUi();
  if(job){
    const fd=new FormData(); fd.append('job', job);
    fetch('/api/reset',{method:'POST',body:fd}).catch(()=>{});
  }
};
$('#cancel').onclick=()=>{
  if(!RUN) return;
  $('#cancel').disabled=true;
  fetch('/api/cancel/'+RUN,{method:'POST'}).catch(()=>{});
};

$('#run').onclick=()=>{
  if(!JOB){alert('Add some files first.');return}
  if(!ACTION){alert('Choose what to do first.');return}
  if(ACTION==='detect'){ runDetect(); return; }
  if(ACTION==='cropwm'){ runSuggestCrop(); return; }
  if(ACTION==='removelogo'&&!$('#box').value.trim()){
    $('#boxwarn').className='msg bad';
    $('#boxwarn').innerHTML='<b>Enter the watermark box first.</b> Use <b>Find watermark</b> to get it — a wrong box erases the wrong part of your video. Or use <b>Crop watermark out</b>.';
    return;
  }
  const fd=new FormData(); fd.append('job',JOB); fd.append('action',ACTION);
  if(ACTION==='split'){fd.append('format',$('#fmt').value);fd.append('layout',$('#layout').value)}
  if(ACTION==='video'){fd.append('fit',$('#fit').value);fd.append('crop',$('#crop').value.trim())}
  if(ACTION==='removelogo'){fd.append('box',$('#box').value.trim());fd.append('method',$('#method').value)}
  startRun(fd);
};

function startRun(fd){
  $('#run').disabled=true;
  $('#cancel').disabled=false;
  RUN=null;
  $('#step4').classList.remove('hidden');
  $('#outcome').innerHTML=''; $('#thumbs').innerHTML=''; $('#candsWrap').innerHTML='';
  $('#log').textContent=''; $('#logwrap').classList.add('hidden');
  $('#progress').classList.remove('hidden'); $('#sum4').textContent='';
  $('#step4').scrollIntoView({behavior:'smooth',block:'start'});
  fetch('/api/run',{method:'POST',body:fd}).then(async r=>{
    const d=await r.json().catch(()=>({}));
    if(!r.ok || d.error){finishError(apiError(d,'Could not start.'));return}
    RUN=d.run;
    $('#plabel').textContent=d.label+'…';
    $('#pnote').textContent = ACTION==='removelogo'&&$('#method').value==='propainter'
      ? 'Rebuilding every frame with ProPainter. This is the slow one — a few minutes is normal.'
      : 'You can leave this page open; it updates as it goes.';
    POLL_FAILS=0; POLL_STARTED=Date.now();
    poll(d.run,0);
  }).catch(e=>finishError(failMessage(e)));
}

function poll(run,since){
  clearTimeout(POLL);
  if(Date.now()-POLL_STARTED>POLL_MAX_MS){
    finishError('This run timed out. Try a smaller file, or check the log.');
    return;
  }
  fetch('/api/status/'+run+'?since='+since).then(async r=>{
    const d=await r.json().catch(()=>({}));
    if(!r.ok || d.error){finishError(apiError(d,'Unknown run.'));return}
    POLL_FAILS=0;
    if(d.lines&&d.lines.length){
      $('#log').textContent+=(($('#log').textContent?'\n':'')+d.lines.join('\n'));
      $('#logwrap').classList.remove('hidden');
    }
    $('#ptime').textContent=d.elapsed+'s'+(d.total?'  ·  file '+Math.min(d.done_count+1,d.total)+' of '+d.total:'');
    if(d.current) $('#plabel').textContent=d.label+' — '+d.current;
    if(d.status==='running'||d.status==='cancelling'){
      if(d.status==='cancelling') $('#plabel').textContent='Cancelling…';
      POLL=setTimeout(()=>poll(run,d.next),700); return;
    }
    $('#progress').classList.add('hidden'); $('#run').disabled=false;
    if(d.status==='cancelled'){finishError('Cancelled.');return}
    if(d.status==='error'){finishError(d.error||'Processing failed.');return}
    finishOk(d.result);
  }).catch(()=>{
    POLL_FAILS++;
    if(POLL_FAILS>=POLL_MAX_FAILS){
      finishError('Lost contact with the local server. Is Framewipe still running?');
      return;
    }
    POLL=setTimeout(()=>poll(run,since),1500);
  });
}

function finishOk(res){
  const n=(res.files||[]).length;
  const fails=(res.failures||[]).length;
  let html='';
  if(n){
    html+='<div class="msg ok"><b>Done — '+n+' file'+(n>1?'s':'')+' ready.</b> Finished in '+res.elapsed+'s.</div>';
  }else{
    html+='<div class="msg warn"><b>No files were produced.</b> Check the log below.</div>';
  }
  if(fails) html+='<div class="msg bad"><b>'+fails+' file'+(fails>1?'s':'')+' failed:</b> '+
    res.failures.map(f=>esc(f[0])+' — '+esc(f[1])).join('; ')+'</div>';
  if((res.synthid||[]).length) html+='<div class="msg warn"><b>SynthID was not removed.</b> '+
    'Metadata is gone and verified, but SynthID is embedded in the pixels and survives re-encoding, cropping and watermark removal.</div>';
  if(n) html+='<div class="row" style="margin-top:14px"><button type="button" id="dl">Download '+
    (n>1?'all ('+n+' files)':'result')+'</button></div>';
  $('#outcome').innerHTML=html;
  $('#sum4').textContent=n?n+' file'+(n>1?'s':''):'';
  if(n){ $('#dl').onclick=()=>location.href='/api/download/'+JOB; }
  (res.files||[]).slice(0,12).forEach(f=>{
    const url='/api/file/'+JOB+'/'+encodeURIComponent(f.name);
    const d=document.createElement('div'); d.className='thumb';
    if(/\.(png|jpe?g|webp)$/i.test(f.name)){
      d.innerHTML='<img src="'+url+'" alt="Result '+esc(f.name)+'">'+
                  '<div title="'+esc(f.name)+'">'+esc(f.name)+' · '+esc(f.size_h)+'</div>';
      $('#thumbs').appendChild(d);
    }else if(/\.(mp4|mov|m4v|webm)$/i.test(f.name)){
      d.innerHTML='<video src="'+url+'" controls preload="metadata" aria-label="Result video '+esc(f.name)+'"></video>'+
                  '<div title="'+esc(f.name)+'">'+esc(f.name)+' · '+esc(f.size_h)+'</div>';
      $('#thumbs').appendChild(d);
    }
  });
}
function finishError(m){
  $('#progress').classList.add('hidden'); $('#run').disabled=false;
  $('#outcome').innerHTML='<div class="msg bad"><b>That didn\'t work.</b> '+esc(m)+'</div>';
}

function runDetect(){
  $('#run').disabled=true;
  $('#step4').classList.remove('hidden');
  $('#outcome').innerHTML=''; $('#thumbs').innerHTML=''; $('#candsWrap').innerHTML='';
  $('#progress').classList.remove('hidden');
  $('#plabel').textContent='Looking for a watermark…';
  $('#pnote').textContent='Sampling frames and comparing them.';
  $('#ptime').textContent='';
  $('#cancel').disabled=true;
  $('#step4').scrollIntoView({behavior:'smooth',block:'start'});
  const fd=new FormData(); fd.append('job',JOB);
  fetch('/api/detect',{method:'POST',body:fd}).then(async r=>{
    const d=await r.json().catch(()=>({}));
    $('#progress').classList.add('hidden'); $('#run').disabled=false;
    if(!r.ok || d.error){finishError(apiError(d,'Detection failed.'));return}
    let html='';
    for(const r0 of d.results){
      html+='<div class="card" style="margin-top:12px"><b>'+esc(r0.name)+'</b>';
      if(!r0.candidates.length){
        html+='<div class="msg warn">Nothing that looks like a watermark was found. If you can see one, read its position off a frame and type the box in manually.</div>';
      }else{
        html+='<div class="msg info">These are <b>guesses</b>. Look at the picture below and pick the box that is actually the watermark.</div><div class="cands">';
        r0.candidates.forEach((c,i)=>{
          html+='<div class="cand"><span><b>'+(i+1)+'.</b> '+esc(c.corner)+
            ' &nbsp;<code>'+esc(c.box)+'</code> &nbsp;<span class="tag">'+c.w+'×'+c.h+' px</span></span>'+
            '<button type="button" class="sm use" data-box="'+esc(c.box)+'">Use this one</button></div>';
        });
        html+='</div>';
      }
      if(r0.preview) html+='<div class="preview"><img src="/api/file/'+JOB+'/'+
        encodeURIComponent(r0.preview)+'" alt="Detected watermark boxes on '+esc(r0.name)+'"></div>';
      html+='</div>';
    }
    $('#candsWrap').innerHTML=html;
    $$('.use').forEach(b=>b.onclick=()=>{
      $('#box').value=b.dataset.box;
      $('#cropbox').value=b.dataset.box;
      selectAction('removelogo');
      $('#boxwarn').className='msg ok';
      $('#boxwarn').innerHTML='Using box <b>'+esc(b.dataset.box)+'</b>. Pick a method and press Run, or switch to <b>Crop watermark out</b>.';
      $('#step3').scrollIntoView({behavior:'smooth',block:'start'});
    });
  }).catch(e=>finishError(failMessage(e)));
}

function selectAction(id){
  ACTION=id;
  $$('.act').forEach(x=>{
    const on=x.dataset.a===id;
    x.classList.toggle('on', on);
    x.setAttribute('aria-checked', on?'true':'false');
  });
  $('#opts').classList.remove('hidden');
  ['clean','split','video','detect','cropwm','removelogo'].forEach(a=>
    $('#opt-'+a).classList.toggle('hidden',a!==id));
  $('#run').textContent = id==='detect' ? 'Find watermark'
    : id==='cropwm' ? 'Suggest crop' : 'Run';
  updateHint();
}

function runSuggestCrop(){
  const box=($('#cropbox').value.trim()||$('#box').value.trim());
  if(!box){
    $('#outcome').innerHTML='';
    $('#step4').classList.remove('hidden');
    finishError('Enter the watermark box first. Use Find watermark to get it.');
    return;
  }
  $('#box').value=box; $('#cropbox').value=box;
  $('#run').disabled=true;
  $('#step4').classList.remove('hidden');
  $('#outcome').innerHTML=''; $('#thumbs').innerHTML=''; $('#candsWrap').innerHTML='';
  $('#progress').classList.remove('hidden');
  $('#plabel').textContent='Finding a crop that excludes the mark…';
  $('#pnote').textContent='Uses the engine --suggest-crop geometry. Nothing is written yet.';
  $('#ptime').textContent='';
  $('#cancel').disabled=true;
  $('#step4').scrollIntoView({behavior:'smooth',block:'start'});
  const fd=new FormData(); fd.append('job',JOB); fd.append('box',box);
  fetch('/api/suggest-crop',{method:'POST',body:fd}).then(async r=>{
    const d=await r.json().catch(()=>({}));
    $('#progress').classList.add('hidden'); $('#run').disabled=false;
    if(!r.ok || d.error){finishError(apiError(d,'Could not suggest a crop.'));return}
    let html='<div class="msg info"><b>Crop is safer than paint.</b> Pick a ratio. Framewipe will re-encode the video with that crop so the mark is out of frame.</div>';
    for(const res of d.results){
      html+='<div class="card" style="margin-top:12px"><b>'+esc(res.name)+'</b>';
      if(res.error){ html+='<div class="msg bad">'+esc(res.error)+'</div></div>'; continue; }
      html+='<div class="hint">Source '+esc(res.source)+' · box <code>'+esc(res.box)+'</code></div><div class="cands">';
      (res.suggestions||[]).forEach(s=>{
        if(!s.ok){
          html+='<div class="cand"><span><b>'+esc(s.label)+'</b> — '+esc(s.detail)+'</span></div>';
        }else{
          html+='<div class="cand"><span><b>'+esc(s.label)+'</b> &nbsp;<code>'+esc(s.crop)+'</code> &nbsp;<span class="tag">keeps '+s.keep_w+'% of width · '+esc(s.size)+'</span></span>'+
            '<button type="button" class="sm applycrop" data-crop="'+esc(s.crop)+'">Apply this crop</button></div>';
        }
      });
      html+='</div></div>';
    }
    $('#candsWrap').innerHTML=html;
    $$('.applycrop').forEach(b=>b.onclick=()=>{
      $('#crop').value=b.dataset.crop;
      const fd2=new FormData();
      fd2.append('job',JOB); fd2.append('action','cropwm');
      fd2.append('box',box); fd2.append('crop',b.dataset.crop);
      startRun(fd2);
    });
  }).catch(e=>finishError(failMessage(e)));
}

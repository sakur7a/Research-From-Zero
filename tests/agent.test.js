import test from 'node:test';
import assert from 'node:assert/strict';
import {MODEL_OPTION_LIMIT,REUSE_PICK_LIMIT,SHIPPED_DEFAULTS,activeRun,budgetSummary,canFollowUp,canRetry,canResume,consentText,deltaSummary,eventText,followupPayload,followupProblem,idempotencyKeyFor,ledgerLine,modelOptionIds,normalizeDefaults,paperCard,reuseChoices,shortUrl,turnLabel} from '../web/agent-core.js';
import {e,link} from '../web/core.js';
test('task lifecycle and resumability are explicit',()=>{
 assert(activeRun({status:'running'})); assert(!activeRun({status:'completed'}));
 assert(canResume({status:'interrupted'})); assert(canResume({status:'failed'}));
 assert(!canResume({status:'cancelled'})); assert(!canResume({status:'budget_exhausted'}));
});
test('tool failures are not displayed as successful checks',()=>{
 assert.match(eventText({kind:'tool_finished',data:{tool:'search_papers',ok:false,error:'限流'}}),/限流/);
 assert.match(eventText({kind:'tool_finished',data:{tool:'search_papers',ok:true,evidence_ids:['ev_1']}}),/1 条证据/);
 assert.match(eventText({kind:'tool_repeat_warning',data:{message:'本任务已经完成相同参数；仍会执行'}}),/仍会执行/);
});
test('model text and evidence locators cannot inject HTML or script URLs',()=>{
 assert.equal(e('<script>alert(1)</script>'),'&lt;script&gt;alert(1)&lt;/script&gt;');
 assert.equal(link('javascript:alert(1)'),'#');
 assert.equal(link('https://token:secret@example.org'),'#');
});
test('missing or malformed task defaults fall back to the shipped values',()=>{
 assert.deepEqual(normalizeDefaults(undefined),SHIPPED_DEFAULTS);
 assert.deepEqual(normalizeDefaults(null),SHIPPED_DEFAULTS);
 assert.deepEqual(normalizeDefaults({}),SHIPPED_DEFAULTS);
 assert.deepEqual(normalizeDefaults({max_model_calls:null,max_tool_calls:'x',attempt_seconds:NaN}),SHIPPED_DEFAULTS);
 assert.deepEqual(normalizeDefaults('12'),SHIPPED_DEFAULTS);
});
test('task defaults are clamped to the server schema bounds',()=>{
 assert.equal(normalizeDefaults({max_model_calls:99}).max_model_calls,24);
 assert.equal(normalizeDefaults({max_model_calls:0}).max_model_calls,2);
 assert.equal(normalizeDefaults({max_tool_calls:-5}).max_tool_calls,1);
 assert.equal(normalizeDefaults({attempt_seconds:5000}).attempt_seconds,900);
 assert.equal(normalizeDefaults({max_tool_calls:'18'}).max_tool_calls,18);
 assert.equal(normalizeDefaults({attempt_seconds:120.9}).attempt_seconds,120);
});
test('the composer summary states the library permission explicitly',()=>{
 assert.equal(budgetSummary({max_model_calls:8,max_tool_calls:10,max_upstream_requests:15,attempt_seconds:120}),'模型 8 次 · 工具 10 次 · 请求 15 次 · 小范围起步 · 单次 120 秒 · 文献库未授权');
 assert.match(budgetSummary({use_library:true}),/文献库已授权$/);
});
test('the per-task consent names library material only when it will be sent',()=>{
 assert.ok(!consentText({use_library:false}).includes('文献库'));
 assert.ok(consentText({use_library:true}).includes('本地文献库的书目与摘要'));
});
test('model candidates are trimmed and deduplicated before reaching the DOM',()=>{
 assert.deepEqual(modelOptionIds({models:['b',' a ','b','','   ']}),['b','a']);
 assert.deepEqual(modelOptionIds({models:['x',7,null,{id:'y'}]}),['x']);
 assert.deepEqual(modelOptionIds({}),[]);
 assert.deepEqual(modelOptionIds(null),[]);
 assert.deepEqual(modelOptionIds('fixture-model'),[]);
 assert.equal(modelOptionIds({models:Array.from({length:MODEL_OPTION_LIMIT+50},(_,i)=>`m${i}`)}).length,MODEL_OPTION_LIMIT);
});
test('evidence read-back and context compaction are visible in the trace',()=>{
 assert.match(eventText({kind:'tool_started',data:{tool:'read_evidence'}}),/取回证据正文/);
 assert.match(eventText({kind:'context_compacted',data:{message:'已收起 3 条较早的工具摘录；证据保留'}}),/已收起 3 条较早的工具摘录/);
});

test('a paper card bounds authors, abstract, institutions and artifacts',()=>{
 const card = paperCard({id:'ev_1',kind:'paper',tool:'search_papers',locator:'metadata from openalex',
  content:'body',retrieved_at:'2026-09-17T00:00:00Z',
  paper:{title:'RevealLayer: Disentangling',authors:Array.from({length:9},(_,i)=>`A${i}`),year:2026,
   doi:'10.1/x',arxiv_id:'2605.11818',abstract:'a'.repeat(500),paper_url:'https://openalex.org/W1'},
  publication:{state:'preprint',label:'仅见预印本版本',venue:'arXiv (Cornell University)',source:'openalex'},
  preprint_also:true,institutions:['清华大学','某机构','另一个','第四个'],
  artifact_candidates:[{url:'https://github.com/1/2',origin:'标识名与项目名一致'},
   {url:'https://huggingface.co/a'},{url:'https://huggingface.co/b'},{url:'https://huggingface.co/c'},
   {url:'https://huggingface.co/d'}],
  artifact_search:'searched'});
 assert.equal(card.authors.length,6); assert.equal(card.moreAuthors,3);
 assert.equal(card.abstract.length,300); assert.equal(card.abstractTruncated,true);
 assert.equal(card.institutions.length,3); assert.equal(card.moreInstitutions,1);
 assert.equal(card.artifacts.length,4);
 assert.deepEqual(card.links.map(l=>l.label),['arXiv','DOI','来源记录']);
 assert.equal(card.stateLabel,'仅见预印本版本'); assert.equal(card.alsoPreprint,true);
 assert.equal(card.artifactCoverage,'');
});

test('a card never invents an abstract, a link, a year or a coverage claim',()=>{
 const bare = paperCard({id:'ev_2',kind:'paper',tool:'search_papers',paper:{title:'仅标题'},artifact_search:'not-run'});
 assert.deepEqual(bare.authors,[]); assert.deepEqual(bare.links,[]); assert.deepEqual(bare.artifacts,[]);
 assert.equal(bare.abstract,''); assert.equal(bare.year,null);
 assert.equal(bare.stateLabel,'unknown');
 assert.match(bare.artifactCoverage,/未做开源检索/);
 assert.match(paperCard({artifact_search:'skipped',paper:{}}).artifactCoverage,/没有可检索的项目名/);
 assert.equal(paperCard({}).title,'未命名论文');
 assert.equal(paperCard(undefined).title,'未命名论文');
});

test('a search that did not finish is not labelled as one that was never run',()=>{
 // Both are gaps, but only one is a rate limit the reader can fix by re-running. Calling either
 // "未做开源检索" would report a failure as a choice.
 assert.match(paperCard({artifact_search:'partial',paper:{}}).artifactCoverage,/只完成了一部分/);
 assert.match(paperCard({artifact_search:'failed',paper:{}}).artifactCoverage,/不代表没有开源/);
 assert.doesNotMatch(paperCard({artifact_search:'partial',paper:{}}).artifactCoverage,/未做开源检索/);
 assert.doesNotMatch(paperCard({artifact_search:'failed',paper:{}}).artifactCoverage,/未做开源检索/);
 // A completed search still says nothing, and an unknown state falls back rather than inventing one.
 assert.equal(paperCard({artifact_search:'searched',paper:{}}).artifactCoverage,'');
 assert.match(paperCard({artifact_search:'some_future_state',paper:{}}).artifactCoverage,/未做开源检索/);
});

test('artifact links are shortened for display but still validated before rendering',()=>{
 assert.equal(shortUrl('https://github.com/360CVGroup/RevealLayer'),'github.com/360CVGroup/RevealLayer');
 assert.equal(shortUrl('https://huggingface.co/datasets/qihoo360/RevealLayer-100K/'),'huggingface.co/datasets/qihoo360/RevealLayer-100K');
 assert.doesNotThrow(()=>shortUrl('javascript:alert(1)'));
 assert.equal(shortUrl(undefined),'');
 assert.equal(link('javascript:alert(1)'),'#');
});

test('the publication label comes from the server rather than a second copy here',()=>{
 const fromServer = paperCard({paper:{},publication:{state:'venue',label:'有会议或期刊版本'}});
 assert.equal(fromServer.stateLabel,'有会议或期刊版本');
 // Older stored evidence has no label; showing the raw state is honest, inventing one is not.
 assert.equal(paperCard({paper:{},publication:{state:'under_review'}}).stateLabel,'under_review');
});

test('a continuing turn is offered only once the previous one has stopped',()=>{
 assert(canFollowUp({status:'completed',conversation_id:'cv_1',turn:1}));
 assert(canFollowUp({status:'budget_exhausted',conversation_id:'cv_1',turn:1}));
 assert(!canFollowUp({status:'running',conversation_id:'cv_1',turn:1}));
 // No conversation means nothing to continue: a run predating the migration must not offer one.
 assert(!canFollowUp({status:'completed',turn:1}));
 assert(canRetry({status:'failed'})); assert(canRetry({status:'budget_exhausted'}));
 assert(!canRetry({status:'completed'})); assert(!canRetry({status:'running'}));
});
test('turn and ledger labels name the kind and never hide the cumulative total',()=>{
 assert.equal(turnLabel({turn:2,kind:'followup'}),'第 2 轮 · 追问');
 assert.equal(turnLabel({turn:1,kind:'new'}),'第 1 轮 · 新研究');
 assert.equal(turnLabel({}),'第 1 轮 · 新研究');
 const line=ledgerLine({ledger:{model_calls:6,tool_calls:9,turns:2},caps:{max_session_model_calls:36,max_session_tool_calls:80}});
 assert.match(line,/模型 6\/36/); assert.match(line,/工具 9\/80/); assert.match(line,/2 轮/);
 // A missing conversation still renders rather than throwing: the panel shows it before the fetch lands.
 assert.match(ledgerLine(null),/模型 0\/\?/);
});
test('the reuse picker is bounded and says how much it withheld',()=>{
 const evidence=Array.from({length:20},(_,i)=>({id:`ev_${i}`}));
 const {choices,hidden}=reuseChoices(evidence,12);
 assert.equal(choices.length,12); assert.equal(hidden,8);
 assert.deepEqual(reuseChoices(undefined),{choices:[],hidden:0});
});
test('a follow-up payload carries every authorization and no invented one',()=>{
 const payload=followupPayload({goal:' 只保留有训练代码的两篇 ',reuse:['ev_1','',null],useLibrary:true,researchScope:'expanded',
   workspaceId:'ws_0123456789abcdef',workspaceReuse:['src_0123456789abcdef','']},
   {id:'run-1'},{max_model_calls:8,max_tool_calls:12,max_upstream_requests:32,attempt_seconds:300,use_library:false});
 assert.equal(payload.parent_run,'run-1');
 assert.equal(payload.goal,'只保留有训练代码的两篇');
 assert.deepEqual(payload.reuse_evidence,['ev_1']);
 assert.equal(payload.workspace_id,'ws_0123456789abcdef');
 assert.deepEqual(payload.reuse_sources,['src_0123456789abcdef']);
 assert.equal(payload.authorize_spend,true); assert.equal(payload.consent_to_send,true);
 assert.equal(payload.use_library,true);
 // Not inherited from the run's own defaults: the checkbox is the only source.
 assert.equal(payload.trust_new_destination,false);
 assert.equal(payload.max_model_calls,8);
 assert.equal(payload.max_upstream_requests,32);
 assert.equal(payload.research_scope,'expanded');
 assert.equal(followupProblem(payload),'');
 assert.match(followupProblem(followupPayload({goal:'短'},{id:'r'},undefined)),/至少 5 字/);
 assert.match(followupProblem(followupPayload({goal:'x'.repeat(6001)},{id:'r'},undefined)),/过长/);
 assert.match(followupProblem({goal:'足够长的目标了',parent_run:''}),/缺少父任务/);
 assert.match(followupProblem({goal:'足够长的目标了',parent_run:'r',reuse_sources:['src_1']}),/工作区/);
});
test('the idempotency key is stable for an unchanged form and differs for a changed one',()=>{
 const run={id:'run-1'};
 const base=followupPayload({goal:'只保留两篇',reuse:['ev_1','ev_2']},run,undefined);
 const same=followupPayload({goal:'只保留两篇',reuse:['ev_2','ev_1']},run,undefined);
 assert.equal(idempotencyKeyFor(run,base),idempotencyKeyFor(run,same));
 const other=followupPayload({goal:'改查数据划分',reuse:['ev_1']},run,undefined);
 assert.notEqual(idempotencyKeyFor(run,base),idempotencyKeyFor(run,other));
 const otherWorkspace=followupPayload({goal:'只保留两篇',workspaceId:'ws_1',workspaceReuse:['src_1']},run,undefined);
 assert.notEqual(idempotencyKeyFor(run,base),idempotencyKeyFor(run,otherWorkspace));
 assert.notEqual(idempotencyKeyFor({id:'run-2'},base),idempotencyKeyFor(run,base));
 assert.match(idempotencyKeyFor(run,base),/^web-[0-9a-z]+$/);
});
test('a report delta is summarized only when there is a previous turn to compare with',()=>{
 assert.equal(deltaSummary(null),null);
 assert.equal(deltaSummary({against_run:'',added:[{claim:'x'}]}),null);
 const summary=deltaSummary({against_run:'r1',against_turn:1,added:[1,2],changed:[3],dropped:[],
   still_uncertain:[4],resolved_from_uncertain:[],outcome:{previous:'findings',current:'insufficient_evidence'},
   note:'dropped 只表示本轮没有再提'});
 assert.equal(summary.againstTurn,1); assert.equal(summary.added,2); assert.equal(summary.changed,1);
 assert.equal(summary.dropped,0); assert.equal(summary.stillUncertain,1);
 assert.equal(summary.outcomeTo,'insufficient_evidence');
 assert.match(summary.note,/本轮没有再提/);
});

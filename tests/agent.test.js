import test from 'node:test';
import assert from 'node:assert/strict';
import {SHIPPED_DEFAULTS,activeRun,budgetSummary,canResume,consentText,eventText,normalizeDefaults} from '../web/agent-core.js';
import {e,link} from '../web/core.js';
test('task lifecycle and resumability are explicit',()=>{
 assert(activeRun({status:'running'})); assert(!activeRun({status:'completed'}));
 assert(canResume({status:'interrupted'})); assert(canResume({status:'failed'}));
 assert(!canResume({status:'cancelled'})); assert(!canResume({status:'budget_exhausted'}));
});
test('tool failures are not displayed as successful checks',()=>{
 assert.match(eventText({kind:'tool_finished',data:{tool:'search_papers',ok:false,error:'限流'}}),/限流/);
 assert.match(eventText({kind:'tool_finished',data:{tool:'search_papers',ok:true,evidence_ids:['ev_1']}}),/1 条证据/);
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
 assert.equal(budgetSummary({max_model_calls:8,max_tool_calls:10,attempt_seconds:120}),'模型 8 次 · 工具 10 次 · 单次 120 秒 · 文献库未授权');
 assert.match(budgetSummary({use_library:true}),/文献库已授权$/);
});
test('the per-task consent names library material only when it will be sent',()=>{
 assert.ok(!consentText({use_library:false}).includes('文献库'));
 assert.ok(consentText({use_library:true}).includes('本地文献库的书目与摘要'));
});

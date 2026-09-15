import test from 'node:test';
import assert from 'node:assert/strict';
import {activeRun,canResume,eventText} from '../web/agent-core.js';
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

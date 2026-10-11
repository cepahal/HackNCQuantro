import type {ReportV2} from '../lib/api-types.ts';
import assert from 'node:assert/strict';
import test from 'node:test';
import {parseProgress,motionStep,progressFromReport,personalizedProgressAvailable} from '../lib/analysis-progress.ts';
import {reportFixture} from './fixtures/report_v2.ts';
test('progress rejects another job and unknown stage',()=>{
 assert.throws(()=>parseProgress({job_id:'other',stage:'search'},'job'));
 assert.throws(()=>parseProgress({job_id:'job',stage:'imagined'},'job'));
});
test('missing fields do not manufacture personalized data',()=>{
 const p=parseProgress({job_id:'job',stage:'search'},'job');
 assert.equal(p.excerpt,'');assert.equal(p.video_ready,false);assert.deepEqual(p.candidates,[]);assert.equal(motionStep(p),1);
});
test('invalid scores and times are excluded',()=>{
 const p=parseProgress({job_id:'job',stage:'candidates',source_start:-1,candidates:[{title:'x',score:Infinity}]},'job');
 assert.equal(p.source_start,null);assert.equal(p.candidates[0].score,null);
});
test('saved report provides personalized replay',()=>{
 const p=progressFromReport(reportFixture);assert.equal(p.job_id,reportFixture.job_id);assert.ok(p.excerpt.length>0);
});

test('progress bounds personalized content and ignores unexpected private fields',()=>{
 const p=parseProgress({job_id:'job',stage:'transcript',excerpt:'x'.repeat(2000),before:['a','b','c','d'],candidates:[{title:'x',score:0},{title:'y',score:101}],local_path:'/tmp/private',api_key:'private'},'job');
 assert.equal(p.excerpt.length,240);assert.equal(p.before.length,3);assert.equal(p.candidates[0].score,0);assert.equal(p.candidates[1].score,null);
 assert.ok(!('local_path' in p));assert.ok(!('api_key' in p));
});

test('saved stale low-confidence source cannot replay a successful match',()=>{
 const report:ReportV2=structuredClone(reportFixture);report.alignment!.match_quality.confidence.level='low';
 const p=progressFromReport(report);assert.equal(p.stage,'no_match');assert.equal(p.video_ready,false);assert.equal(p.source_start,null);
});


test('orb remains through retrieval even if metadata already contains text',()=>{
 assert.equal(personalizedProgressAvailable(null),false);
 assert.equal(personalizedProgressAvailable(parseProgress({job_id:'job',stage:'retrieve',excerpt:'Known title or cached text'},'job')),false);
 for(const stage of ['transcript','search','candidates','align','context','export','complete','no_match','failed'])
  assert.equal(personalizedProgressAvailable(parseProgress({job_id:'job',stage,excerpt:'Actual recorded speech'},'job')),true);
});


test('empty transcript and stage labels alone do not trigger personalized animation',()=>{
 for(const stage of ['transcript','search','candidates','align','context','export','complete'])
  assert.equal(personalizedProgressAvailable(parseProgress({job_id:'job',stage,title:'Video metadata',excerpt:'   '},'job')),false);
 assert.equal(personalizedProgressAvailable(parseProgress({job_id:'job',stage:'candidates',candidates:[{title:'Real returned candidate'}]},'job')),true);
});

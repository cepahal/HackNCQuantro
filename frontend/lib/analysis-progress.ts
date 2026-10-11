import type {ReportV2} from './api-types.ts';
export const motionSteps = ['Transcript','Search clues','Candidates','Alignment','Context','Extract'] as const;
export type ProgressStage = 'retrieve'|'transcript'|'search'|'candidates'|'align'|'context'|'export'|'complete'|'no_match'|'failed';
export type AnalysisProgress = {job_id:string;stage:ProgressStage;status:string;title:string;excerpt:string;speaker_clue:string;anchor:string;candidates:{title:string;score:number|null}[];source_title:string;source_start:number|null;source_end:number|null;before:string[];after:string[];context_method:string;video_ready:boolean;message:string};
const stages:ProgressStage[]=['retrieve','transcript','search','candidates','align','context','export','complete','no_match','failed'];
export function parseProgress(raw:unknown,jobId:string):AnalysisProgress {
 if(!raw||typeof raw!=='object')throw new Error('Invalid progress');
 const r=raw as Record<string,unknown>;
 if(r.job_id!==jobId||!stages.includes(r.stage as ProgressStage))throw new Error('Invalid progress');
 const text=(k:string,max=240)=>typeof r[k]==='string'?(r[k] as string).slice(0,max):'';
 const time=(k:string)=>typeof r[k]==='number'&&Number.isFinite(r[k])&&(r[k] as number)>=0?r[k] as number:null;
 const words=(k:string)=>Array.isArray(r[k])?(r[k] as unknown[]).filter((v):v is string=>typeof v==='string').slice(0,3).map(v=>v.slice(0,240)):[];
 return {job_id:jobId,stage:r.stage as ProgressStage,status:text('status',32),title:text('title',120),excerpt:text('excerpt'),speaker_clue:text('speaker_clue',120),anchor:text('anchor',160),source_title:text('source_title',120),source_start:time('source_start'),source_end:time('source_end'),before:words('before'),after:words('after'),context_method:text('context_method',60),video_ready:r.video_ready===true,message:text('message'),candidates:Array.isArray(r.candidates)?r.candidates.slice(0,3).filter(v=>v&&typeof v==='object').map(v=>({title:typeof v.title==='string'?v.title.slice(0,120):'',score:typeof v.score==='number'&&Number.isFinite(v.score)&&v.score>=0&&v.score<=100?v.score:null})):[]};
}
export function motionStep(p:AnalysisProgress):number {return ({retrieve:0,transcript:0,search:1,candidates:2,align:3,context:4,export:5,complete:5,no_match:2,failed:0})[p.stage];}
export function progressFromReport(report:ReportV2):AnalysisProgress {
 const selection=report.context_selection as Record<string,unknown>|null|undefined;
 const accepted=report.outcome==='matched'&&report.source!==null&&report.alignment?.status==='verified'&&report.alignment.matches.length>0&&['high','moderate'].includes(report.alignment.match_quality.confidence.level)&&report.alignment.source_media_id===report.source.post.media.media_id&&report.alignment.clip_media_id===report.submitted.post.media.media_id;
 const matches=accepted?report.alignment!.matches:[];
 return parseProgress({job_id:report.job_id,stage:accepted?'complete':'no_match',status:'completed',title:report.submitted.post.title??'',excerpt:report.submitted.transcript.sentences.map(s=>s.text).join(' '),source_title:report.source?.post.title??'',source_start:matches[0]?.source_start_seconds??null,source_end:matches.at(-1)?.source_end_seconds??null,before:Array.isArray(selection?.before)?selection.before.filter((s):s is {text:string}=>Boolean(s)&&typeof s==='object'&&typeof s.text==='string').map(s=>s.text):[],after:Array.isArray(selection?.after)?selection.after.filter((s):s is {text:string}=>Boolean(s)&&typeof s==='object'&&typeof s.text==='string').map(s=>s.text):[],context_method:selection?.strategy??'',video_ready:accepted&&report.context_video?.mime_type==='video/mp4',message:accepted&&!report.context_video?'Report ready. Context-video export is unavailable.':''},report.job_id);
}

/** Retrieval keeps the orb; only confirmed later pipeline stages personalize it. */
export function personalizedProgressAvailable(progress:AnalysisProgress|null|undefined):boolean {
 if(!progress||progress.stage==='retrieve')return false;
 const meaningful=(text:string)=>Boolean(text.trim());
 return meaningful(progress.excerpt)||meaningful(progress.anchor)||meaningful(progress.speaker_clue)
  ||progress.candidates.some(candidate=>meaningful(candidate.title))||meaningful(progress.source_title)
  ||progress.before.some(meaningful)||progress.after.some(meaningful)
  ||(['failed','no_match'].includes(progress.stage)&&meaningful(progress.message));
}

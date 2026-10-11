"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import Navbar from "./Navbar";
import AnalysisLoadingScreen from "./AnalysisLoadingScreen";
import ReportPlayer from "./ReportPlayer";
import ReportSummary, { ReportVerdict } from "./ReportSummary";
import SourceLeads from "./SourceLeads";
import { analysisInFlight, getReport, getDisplayModel, publicUrl, startAnalysis, waitForReport } from "../lib/api";
import type { ReportV2 as Report, DisplayModel } from "../lib/api-types";
import { errorDisplay, type ErrorDisplay } from "../lib/display";
import { compact, date, percent, time, unique } from "../lib/format";
import { summaryText } from "../lib/report-summary";
import styles from "./ReportPage.module.css";

export default function ReportPage({jobId,reportId}:{jobId:string|null;reportId:string|null}){
 const router=useRouter();const[report,setReport]=useState<Report|null>(null);const[display,setDisplay]=useState<DisplayModel|null>(null);const[error,setError]=useState<ErrorDisplay|null>(null);const[attempt,setAttempt]=useState(0);const[copyStatus,setCopyStatus]=useState("");const[loadingDismissed,setLoadingDismissed]=useState(false);const[routeAfterReveal,setRouteAfterReveal]=useState<string|null>(null);const skipNextLoading=useRef(false);const requestKey=useRef(`${jobId}:${reportId}`);
 useEffect(()=>{
  const nextKey=`${jobId}:${reportId}`;
  if(requestKey.current!==nextKey){requestKey.current=nextKey;if(skipNextLoading.current){skipNextLoading.current=false}else{setLoadingDismissed(false);setReport(null);setDisplay(null);setError(null)}}
  const controller=new AbortController();
  async function load(){try{
   if(reportId){const [loaded,model]=await Promise.all([getReport(reportId),getDisplayModel(reportId)]);if(!controller.signal.aborted){setReport(loaded);setDisplay(model)}return}
   if(jobId){
    const run=analysisInFlight(jobId);
    const id=run?(await run.result).report_id:await waitForReport(jobId,controller.signal);
     if(controller.signal.aborted)return;
     const [loaded,model]=await Promise.all([getReport(id),getDisplayModel(id)]);
     if(!controller.signal.aborted){setReport(loaded);setDisplay(model);setRouteAfterReveal(id)}
    return;
   }
   throw errorDisplay({error:{code:"MISSING_REPORT",stage:"load_report",message:"Choose a report to view.",retryable:false}});
  }catch(reason){if(!controller.signal.aborted)setError(errorDisplay(reason))}}
  void load();return()=>controller.abort();
 },[reportId,jobId,router,attempt]);
 const retry=()=>{setReport(null);setDisplay(null);setError(null);setLoadingDismissed(false);setRouteAfterReveal(null);const run=!reportId&&jobId?analysisInFlight(jobId):undefined;if(run){router.replace(`/results?job=${encodeURIComponent(startAnalysis(run.url))}`);return}setAttempt(value=>value+1)};
 const copyLink=async()=>{try{if(!navigator.clipboard?.writeText)throw new Error("Clipboard unavailable");await navigator.clipboard.writeText(window.location.href);setCopyStatus("Link copied.")}catch{setCopyStatus("Couldn’t copy. Copy the address from your browser.")}window.setTimeout(()=>setCopyStatus(""),2200)};
 const ready=Boolean(error||(report&&display));
 const finishLoading=()=>{setLoadingDismissed(true);if(routeAfterReveal){skipNextLoading.current=true;router.replace(`/results?id=${encodeURIComponent(routeAfterReveal)}`)}};
 return <><Navbar/><div className={styles.page}><main className={styles.main}>
  <header className={styles.header}>
   <Link className={styles.back} href="/">← New analysis</Link>
   <div className={styles.heading}><div><p className={styles.eyebrow}>Zoomout / Results</p><h1>Context report</h1></div>
    <div className={styles.actions}><button className={styles.copyButton} onClick={copyLink}>{copyStatus.startsWith("Link copied")?"Copied":copyStatus.startsWith("Couldn’t")?"Copy failed":"Copy link"}<span aria-hidden="true">↗</span></button><span role="status" className="sr-only">{copyStatus}</span></div>
   </div>
  </header>
  {error?<div className={styles.error} role="alert"><h2>We couldn’t load this report.</h2><p>{error.error.message}</p><p className={styles.note}>{error.error.code} · {error.error.stage}</p>{error.error.retryable&&<button className={styles.copyButton} onClick={retry}>Retry</button>}</div>:report&&display?<ReportContent report={report} display={display}/>:null}
 </main><footer className={styles.footer}><Link href="/" aria-label="Zoomout home"><Image src="/logo-light.png" alt="Zoomout" width={129} height={25} unoptimized /></Link><p>The full record, in context.</p><span>Built at HackNC · © 2026 Zoomout</span></footer></div>{!loadingDismissed&&<AnalysisLoadingScreen complete={ready} onDone={finishLoading}/>}</>;
}
export function ReportLoading(){return <AnalysisLoadingScreen/>}

export function ReportContent({ report, display }: { report: Report; display: DisplayModel }) {
 const { source, submitted, alignment, comparison } = report;
 const limitations = unique([
  ...report.limitations, ...(alignment?.limitations ?? []), ...(comparison?.limitations ?? []),
  ...(report.context?.limitations ?? []), ...submitted.post.retrieval.warnings, ...(source?.post.retrieval.warnings ?? []),
  ...(alignment?.match_quality.confidence.limitations ?? []), ...(comparison?.assessment_confidence.limitations ?? []),
 ]);
 const findings = comparison?.findings.filter(finding => finding.evidence.length > 0) ?? [];
 return <article className={styles.report}>
  <ReportVerdict report={report}/>
  <ReportSummary report={report}/>
  <section className={styles.section} aria-label="Recordings">
   <div className={styles.sectionHead}><h2>{source ? "Compare the recordings" : "Submitted recording"}</h2><span>{source ? "Excerpt + matched recording" : "Source comparison unavailable"}</span></div>
   <div className={`${styles.recordings} ${!source ? styles.singleRecording : ""}`}>
    <ReportPlayer key={submitted.post.media.media_id} media={submitted} label="Submitted excerpt"/>
    {source&&<ReportPlayer key={source.post.media.media_id} media={source} label="Matched recording"/>}
   </div>
   {source&&<p className={styles.note}>A matching recording may be a reupload. The earliest original upload has not been established.</p>}
  </section>
  {findings.length>0&&<section className={styles.section} aria-label="Cited findings"><h2>Evidence behind the result</h2>
   <div className={styles.findings}>{findings.slice(0,3).map(finding=><article className={styles.finding} key={finding.finding_id}>
    <p>{summaryText(finding.explanation)}</p>{finding.evidence.slice(0,2).map((evidence,index)=><figure key={`${evidence.media_id}-${index}`}>
     <blockquote>“{summaryText(evidence.quote)}”</blockquote><figcaption>{evidence.media_id===source?.post.media.media_id?"Matched recording":"Submitted excerpt"} · {time(evidence.start_seconds,true)}–{time(evidence.end_seconds,true)}</figcaption>
    </figure>)}
   </article>)}</div>
  </section>}
  <div className={styles.sourceLeads}><SourceLeads report={report}/></div>
  <section className={styles.section} aria-label="Evidence strength">
   <h2>Evidence strength</h2>
   <dl className={styles.metrics}>
    <div><dt>Source match</dt><dd>{display.source_match?.value??"Not assessed"}</dd><p>{display.source_match?.basis[0]??"No verified source alignment is available."}</p></div>
    <div><dt>Excerpt aligned</dt><dd>{alignment?.match_quality.clip_coverage_ratio==null?"Unavailable":percent(alignment.match_quality.clip_coverage_ratio)}</dd><p>{display.clip_coverage?.description??"Coverage of the submitted excerpt is unavailable."}</p></div>
    <div><dt>Context assessment</dt><dd>{display.context_assessment?.value??"Not assessed"}</dd><p>{display.context_assessment?.basis[0]??"Source context could not be assessed."}</p></div>
   </dl><p className={styles.note}>{display.confidence_note}</p>
  </section>
  <section className={styles.disclosures} aria-label="Report details">
   <details className={styles.disclosure}><summary><span>Transcripts</span><span className={styles.disclosureHint}>Read the available speech</span></summary>
    <p className={styles.note}>Speech recognition and timestamps may contain errors. Partial transcripts leave unobserved speech unknown.</p>
    <div className={styles.transcripts}><Transcript media={submitted} label="Submitted excerpt"/>{source&&<Transcript media={source} label="Matched recording"/>}</div>
   </details>
   {report.context&&<details className={styles.disclosure}><summary><span>Surrounding source context</span><span className={styles.disclosureHint}>{report.context.windows.length} {report.context.windows.length===1?"window":"windows"}</span></summary>
    {report.context.windows.map(window=><article className={styles.contextWindow} key={window.context_id}>
     <h3>{time(window.requested_start_seconds)}–{time(window.requested_end_seconds)} <span>{window.coverage} coverage</span></h3>
     {window.sentences.length?window.sentences.map(sentence=><p key={sentence.sentence_id}><time>{time(sentence.start_seconds,true)}</time> {summaryText(sentence.text)}</p>):<p className={styles.note}>Transcript unavailable for this window.</p>}
    </article>)}
   </details>}
   <details className={styles.disclosure}><summary><span>Technical evidence</span><span className={styles.disclosureHint}>Alignment, methods, and provenance</span></summary>
    {alignment?<>
     <p className={styles.note}>Transcript alignment is approximate. Source gaps do not establish deletions or speed changes.</p>
     <div className={styles.tableWrap}><table><caption>Aligned speech passages</caption><thead><tr><th>Passage</th><th>Submitted excerpt</th><th>Matched recording</th></tr></thead><tbody>{alignment.matches.map((match,index)=><tr key={match.match_id}><td>{index+1}</td><td>{time(match.clip_start_seconds,true)}–{time(match.clip_end_seconds,true)}</td><td>{time(match.source_start_seconds,true)}–{time(match.source_end_seconds,true)}</td></tr>)}</tbody></table></div>
     <dl className={styles.technicalCounts}>{Object.entries(alignment.edit_analysis.counts).map(([label,value])=><div key={label}><dt>{label.replaceAll("_"," ")}</dt><dd>{value??"Unavailable"}</dd></div>)}</dl>
     <h3>Alignment evidence</h3>{alignment.matches.map(match=><div key={match.match_id}>{match.evidence.map((evidence,index)=><p className={styles.detailText} key={index}><strong>{evidence.method.replaceAll("_"," ")}:</strong> {summaryText(evidence.description)}</p>)}</div>)}
     {alignment.edit_analysis.source_gaps.length>0&&<><h3>Approximate source gaps</h3><ul>{alignment.edit_analysis.source_gaps.map((gap,index)=><li key={index}>{time(gap.start_seconds,true)}–{time(gap.end_seconds,true)}</li>)}</ul></>}
     {alignment.edit_analysis.unmatched_clip_ranges.length>0&&<><h3>Unmatched excerpt ranges</h3><ul>{alignment.edit_analysis.unmatched_clip_ranges.map((range,index)=><li key={index}>{time(range.start_seconds,true)}–{time(range.end_seconds,true)}</li>)}</ul></>}
     <p className={styles.detailText}>Methods: {alignment.match_quality.methods_used.join(", ")||"Unavailable"}</p>
     <p className={styles.detailText}>Scope: {alignment.edit_analysis.scope||"Unavailable"}</p>
    </>:<p className={styles.note}>No verified source alignment is available.</p>}
    {findings.length>3&&<><h3>Additional findings</h3>{findings.slice(3).map(finding=><div key={finding.finding_id}><p>{summaryText(finding.explanation)}</p>{finding.evidence.map((evidence,index)=><p key={index}>“{summaryText(evidence.quote)}” · {time(evidence.start_seconds,true)}–{time(evidence.end_seconds,true)}</p>)}</div>)}</>}
    <p className={styles.note}>Report {report.job_id} · Schema {report.schema_version}</p>
   </details>
   <details className={styles.disclosure}><summary><span>Post details</span><span className={styles.disclosureHint}>Metadata supplied with each post</span></summary>
    <p className={styles.note}>Popularity, account identity, and verification badges are not reliability signals.</p>
    <div className={styles.postGrid}><PostDetails media={submitted} label="Submitted post"/>{source&&<PostDetails media={source} label="Matched post"/>}</div>
   </details>
   <details className={styles.disclosure}><summary><span>Limitations and retrieval notes</span><span className={styles.disclosureHint}>{limitations.length} {limitations.length===1?"note":"notes"}</span></summary>
    {limitations.length?<ul className={styles.limitations}>{limitations.map(item=><li key={item}>{summaryText(item)}</li>)}</ul>:<p className={styles.note}>No additional limitations were recorded. This does not establish accuracy or authenticity.</p>}
   </details>
  </section>
 </article>;
}

function Transcript({ media, label }: { media: Report["submitted"]; label: string }) {
 return <section className={styles.transcript} aria-label={`${label} transcript`}><h3>{label}<span>{media.transcript.status.replaceAll("_"," ")}</span></h3>
  {media.transcript.sentences.length?media.transcript.sentences.map(sentence=><p key={sentence.sentence_id}><time>{time(sentence.start_seconds,true)}–{time(sentence.end_seconds,true)}{sentence.speaker_id&&` · ${sentence.speaker_id}`}</time>{summaryText(sentence.text)}</p>):<p className={styles.note}>Transcript unavailable.</p>}
 </section>;
}

function PostDetails({ media, label }: { media: Report["submitted"]; label: string }) {
 const { post }=media;const original=publicUrl(post.post_url);
 const rows: Array<[string,string|number]> = [
  ["Platform",post.platform], ["Title",post.title||"Unavailable"], ["Caption",post.caption||"Unavailable"],
  ["Handle",post.author.handle||"Unavailable"], ["Name",post.author.display_name||"Unavailable"], ["Published",date(post.published_at)],
  ["Duration",post.media.duration_seconds==null?"Unavailable":time(post.media.duration_seconds)],
  ["Resolution",post.media.width&&post.media.height?`${post.media.width} × ${post.media.height}`:"Unavailable"],
  ["FPS",post.media.fps??"Unavailable"], ["Audio",post.media.has_audio==null?"Unavailable":post.media.has_audio?"Yes":"No"],
  ["Views",compact(post.engagement.views_count)], ["Likes",compact(post.engagement.likes_count)], ["Comments",compact(post.engagement.comments_count)],
  ["Shares",compact(post.engagement.shares_count)], ["Followers",compact(post.author.followers_count)], ["Following",compact(post.author.following_count)],
  ["Posts",compact(post.author.posts_count)], ["Verified badge",post.author.verified==null?"Unavailable":post.author.verified?"Yes":"No"],
  ["Metadata provider",post.retrieval.provider], ["Retrieved",date(post.retrieval.retrieved_at)],
 ];
 return <article className={styles.post}><h3>{label}</h3><dl>{rows.map(([key,value])=><div key={key}><dt>{key}</dt><dd>{typeof value==="string"?summaryText(value):value}</dd></div>)}</dl>{original&&<a href={original} target="_blank" rel="noopener noreferrer">View post ↗</a>}</article>;
}

"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import Navbar from "./Navbar";
import AnalysisLoadingScreen from "./AnalysisLoadingScreen";
import ReportPlayer from "./ReportPlayer";
import ReportSummary, { reportAssessment, ReportVerdict } from "./ReportSummary";
import SourceLeads from "./SourceLeads";
import ReportAnalytics, { SourceOptions } from "./ReportAnalytics";
import { analysisInFlight, getReport, getDisplayModel, getReportDiagnostics, getReportInterpretation, recheckReportSource, publicUrl, startAnalysis, waitForReport } from "../lib/api";
import type { ReportV2 as Report, ReportInterpretation, DisplayModel } from "../lib/api-types";
import { errorDisplay, type ErrorDisplay } from "../lib/display";
import { compact, date, percent, time, unique } from "../lib/format";
import { summaryText } from "../lib/report-summary";
import { sourceLeads, type SourceLead } from "../lib/source-leads";
import { reportAnalytics } from "../lib/report-analytics";
import { analysisName, diagnosticFilename, diagnosticOutput, type ReportDiagnostics } from "../lib/report-diagnostics";
import { reportDataMarkers, reportTimelineMarkers } from "../lib/view-model";
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
   <div className={styles.heading}><div><p className={styles.eyebrow}>Zoomout / Analysis</p><h1>{analysisName(report)}</h1></div>
    <div className={styles.actions}><button className={styles.copyButton} onClick={copyLink}>{copyStatus.startsWith("Link copied")?"Copied":copyStatus.startsWith("Couldn’t")?"Copy failed":"Copy link"}<span aria-hidden="true">↗</span></button><span role="status" className="sr-only">{copyStatus}</span></div>
   </div>
  </header>
  {error?<><div className={styles.error} role="alert"><h2>We couldn’t load this report.</h2><p>{error.error.message}</p><p className={styles.note}>{error.error.code} · {error.error.stage}</p>{error.error.retryable&&<button className={styles.copyButton} onClick={retry}>Retry</button>}</div>{(jobId||reportId)&&<DiagnosticPanel key={jobId||reportId} jobId={jobId??reportId!.replace(/^report_/,"")}/>}</>:report&&display?<ReportContent key={report.job_id} report={report} display={display} onRecheckCompleted={id=>router.push(`/results?id=${encodeURIComponent(id)}`)}/>:null}
 </main><footer className={styles.footer}><Link href="/" aria-label="Zoomout home"><Image src="/logo-light.png" alt="Zoomout" width={129} height={25} unoptimized /></Link><p>The full record, in context.</p><span>Built at HackNC · © 2026 Zoomout</span></footer></div>{!loadingDismissed&&<AnalysisLoadingScreen complete={ready} onDone={finishLoading}/>}</>;
}
export function ReportLoading(){return <AnalysisLoadingScreen/>}

export function ReportContent({ report, display, diagnostics, onRecheckCompleted }: { report: Report; display: DisplayModel; diagnostics?: ReportDiagnostics; onRecheckCompleted?: (reportId: string) => void }) {
 const [captured,setCaptured]=useState<ReportDiagnostics|null>(diagnostics??null);
 const [interpretation,setInterpretation]=useState<ReportInterpretation|null>(null);
 const [interpretationState,setInterpretationState]=useState<"loading"|"ready"|"unavailable">("loading");
 const [pendingUrl,setPendingUrl]=useState<string|null>(null);
 const [sourceError,setSourceError]=useState<string|null>(null);
 const [checkedReportId,setCheckedReportId]=useState<string|null>(null);
 const rechecking=useRef(false);
 const analytics=reportAnalytics(report,captured);
 useEffect(()=>{
  if(!analytics.isVideoDetected)return;
  let active=true;
  getReportInterpretation(`report_${report.job_id}`).then(value=>{
   if(active){setInterpretation(value);setInterpretationState(value.status);}
  }).catch(()=>{if(active)setInterpretationState("unavailable")});
  return()=>{active=false};
 },[report.job_id,analytics.isVideoDetected]);
 const recheck=async(option:SourceLead)=>{
  if(rechecking.current)return;
  rechecking.current=true;setPendingUrl(option.url);setSourceError(null);
  try{const result=await recheckReportSource(`report_${report.job_id}`,option.url);
   if(onRecheckCompleted)onRecheckCompleted(result.report_id);
   else setCheckedReportId(result.report_id);
  }catch(reason){setSourceError(errorDisplay(reason).error.message)}
  finally{rechecking.current=false;setPendingUrl(null)}
 };
 const { source, submitted, alignment, comparison } = report;
 const limitations = unique([
  ...report.limitations, ...(alignment?.limitations ?? []), ...(comparison?.limitations ?? []),
  ...(report.context?.limitations ?? []), ...submitted.post.retrieval.warnings, ...(source?.post.retrieval.warnings ?? []),
  ...(alignment?.match_quality.confidence.limitations ?? []), ...(comparison?.assessment_confidence.limitations ?? []),
 ]);
 const findings = comparison?.findings.filter(finding => finding.evidence.length > 0) ?? [];
 const verifiedSource = reportAssessment(report).items[0].tone === "supported"
  && ["high", "moderate"].includes(alignment?.match_quality.confidence.level ?? "") ? source : null;
 const leads = sourceLeads(report);
 const primaryLead = verifiedSource ? undefined : leads[0];
 const candidateMedia = !verifiedSource && !primaryLead ? source : null;
 const additionalLeadCount = leads.length - (primaryLead ? 1 : 0);
 return <article className={styles.report}>
  <ReportAnalytics data={analytics} interpretation={interpretation} interpretationState={interpretationState}/>
  {checkedReportId&&<Link href={`/results?id=${encodeURIComponent(checkedReportId)}`}>Open rechecked report ↗</Link>}
  {analytics.isVideoDetected&&<div className={styles.videoGrid}>
  <section className={styles.primarySource} aria-label="Submitted video">
   <div className={styles.primaryHead}><h2>Submitted video</h2></div>
   <div className={styles.primaryVideo}><ReportPlayer media={submitted} label="Submitted excerpt"/></div>
  </section>
  <section className={styles.primarySource} aria-label="Source video result">
   <div className={styles.primaryHead}>
    <div><p className={styles.eyebrow}>{verifiedSource ? "Matched source recording" : primaryLead || candidateMedia ? "Most likely source candidate" : "Source recording"}</p>
     <h2>{summaryText(verifiedSource?.post.title || primaryLead?.title || candidateMedia?.post.title || (verifiedSource ? "Matched recording" : "No source candidate found"))}</h2>
    </div>
    {(verifiedSource || primaryLead || candidateMedia) && <span className={`${styles.sourceStatus} ${verifiedSource ? styles.verifiedSource : styles.unverifiedSource}`}>
     {verifiedSource ? "Verified speech match" : "Unverified lead"}
    </span>}
   </div>
   {verifiedSource ? <div className={styles.primaryVideo}><ReportPlayer key={verifiedSource.post.media.media_id} media={verifiedSource} label="Matched source recording"/></div>
    : primaryLead ? <div className={styles.candidatePlayer}>
     <iframe src={primaryLead.preview} title={`Unverified source candidate: ${primaryLead.title}`} loading="lazy"
      referrerPolicy="strict-origin-when-cross-origin"
      allow="accelerometer; encrypted-media; gyroscope; picture-in-picture; web-share" allowFullScreen/>
     <a href={primaryLead.url} target="_blank" rel="noopener noreferrer">Open candidate recording ↗</a>
    </div>
    : candidateMedia ? <div className={styles.primaryVideo}><ReportPlayer key={candidateMedia.post.media.media_id} media={candidateMedia} label="Unverified source candidate"/></div>
      : <div className={styles.sourceEmpty}><span aria-hidden="true">○</span><p>No candidate recording is available for this excerpt yet.</p></div>}
   <p className={styles.note}>{verifiedSource
    ? "Matching speech was found in this recording. It may be a reupload; the earliest original upload has not been established."
    : primaryLead || candidateMedia ? "This is the strongest available search lead. Its speech and surrounding context have not been verified against the excerpt."
      : "No source found does not establish whether the excerpt is accurate or unedited."}</p>
  </section>
  <section hidden={!verifiedSource || !report.context_video} className={styles.primarySource} aria-label="Spliced context video">
   <div className={styles.primaryHead}><h2>Clip in context</h2></div>
   {verifiedSource && report.context_video
    ? <div className={styles.primaryVideo}><ReportPlayer key={report.context_video.media_id}
      media={{...verifiedSource, post: {...verifiedSource.post, media: report.context_video}}}
      label="Spliced context video" generated/></div>
    : <div className={styles.sourceEmpty} role="status"><p>{verifiedSource
      ? "No generated context video is available for this report."
      : "A sufficiently confident source match is needed before generating a context video."}</p></div>}
   {verifiedSource && report.context_video && <p className={styles.note}>Generated from the matched recording, with timed labels for the excerpt and surrounding context.</p>}
  </section>
  </div>}
  {!analytics.isVideoDetected&&<SourceOptions options={leads.slice(0,4)} isVideoDetected={false} pendingUrl={pendingUrl} error={sourceError} onSelect={recheck}/>}
  <div hidden={!sourceError} data-testid="error-diagnostics">
   <DiagnosticPanel key={report.job_id} report={report} display={display} initialDiagnostics={diagnostics} onDiagnostics={setCaptured}/>
  </div>
  {analytics.isVideoDetected&&<SourceOptions options={leads.slice(0,4)} isVideoDetected pendingUrl={pendingUrl} error={sourceError} onSelect={recheck}/>}
  {/* Preserved for restoration after diagnostic mode; intentionally hidden. */}
  <div hidden className={styles.legacyPanels} data-testid="preserved-report-panels">
  <details className={styles.submittedClip} aria-label="Submitted clip">
   <summary><span>Submitted clip</span><span>View the input <span aria-hidden="true">+</span></span></summary>
   <div className={styles.submittedVideo}><ReportPlayer key={submitted.post.media.media_id} media={submitted} label="Submitted excerpt"/></div>
  </details>
  <ReportVerdict report={report}/>
  <ReportSummary report={report}/>
  {findings.length>0&&<section className={styles.section} aria-label="Cited findings"><h2>Evidence behind the result</h2>
   <div className={styles.findings}>{findings.slice(0,3).map(finding=><article className={styles.finding} key={finding.finding_id}>
    <p>{summaryText(finding.explanation)}</p>{finding.evidence.slice(0,2).map((evidence,index)=><figure key={`${evidence.media_id}-${index}`}>
     <blockquote>“{summaryText(evidence.quote)}”</blockquote><figcaption>{evidence.media_id===source?.post.media.media_id?"Matched recording":"Submitted excerpt"} · {time(evidence.start_seconds,true)}–{time(evidence.end_seconds,true)}</figcaption>
    </figure>)}
   </article>)}</div>
  </section>}
  {additionalLeadCount>0&&<details className={`${styles.disclosure} ${styles.additionalLeads}`}>
   <summary><span>Other source candidates</span><span className={styles.disclosureHint}>{additionalLeadCount} unverified {additionalLeadCount===1?"lead":"leads"}</span></summary>
   <div className={styles.sourceLeads}><SourceLeads report={report} excludeUrl={primaryLead?.url}/></div>
  </details>}
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
  </div>
 </article>;
}

export function DiagnosticPanel({ report=null, display=null, initialDiagnostics, jobId=report?.job_id??"", onDiagnostics }: {
 report?: Report|null; display?: DisplayModel|null; initialDiagnostics?: ReportDiagnostics; jobId?:string; onDiagnostics?: (value: ReportDiagnostics) => void;
}) {
 const reportAvailable=Boolean(report);
 const savedDataNote=reportAvailable?"All saved report data is shown below.":"No saved report was loaded.";
 const [diagnostics,setDiagnostics]=useState<ReportDiagnostics|null>(initialDiagnostics??null);
 const [state,setState]=useState(initialDiagnostics?.capture_status==="recorded"?"Captured output loaded.":initialDiagnostics?`Resolver output was not recorded for this older report. ${savedDataNote}`:"Loading captured output...");
 const [actionStatus,setActionStatus]=useState("");
 const [attempt,setAttempt]=useState(0);
 useEffect(()=>{
  if(initialDiagnostics)return;
  let active=true;
  getReportDiagnostics(`report_${jobId}`).then(value=>{
   if(!active)return;
   setDiagnostics(value);
   onDiagnostics?.(value);
   setState(value.capture_status==="recorded"?"Captured output loaded.":`Resolver output was not recorded for this older report. ${savedDataNote}`);
  }).catch(()=>{if(active)setState(`Captured output could not be loaded. ${savedDataNote}`)});
  return()=>{active=false};
 },[jobId,initialDiagnostics,attempt,savedDataNote,onDiagnostics]);
 const output=diagnosticOutput(report,display,diagnostics,state,jobId);
 const markers=report?reportDataMarkers(report,diagnostics):[];
 const timeline=report?reportTimelineMarkers(report):[];
 const copy=async()=>{
  try{await navigator.clipboard.writeText(output);setActionStatus("Output copied.")}
  catch{setActionStatus("Copy unavailable. Select the output or download it.")}
 };
 const download=()=>{
  const link=document.createElement("a");
  const url=URL.createObjectURL(new Blob([output],{type:"text/plain;charset=utf-8"}));
  link.href=url;link.download=diagnosticFilename(report);link.click();
  window.setTimeout(()=>URL.revokeObjectURL(url),0);
  setActionStatus("Output downloaded.");
 };
 return <section className={styles.diagnosticPanel} aria-label="Analysis diagnostic output">
  <div className={styles.diagnosticHeader}>
   <div><p className={styles.eyebrow}>Diagnostic mode</p><h2>Analysis terminal</h2></div>
   <div className={styles.diagnosticActions}>
    <button className={styles.copyButton} onClick={copy}>Copy output</button>
    <button className={styles.copyButton} onClick={download}>Download .txt</button>
   </div>
  </div>
  <p className={styles.diagnosticNote}>Per-job resolver events and saved public analysis data, without summary truncation. {reportAvailable?"The complete saved report is included.":"No saved report was loaded."} Credentials, private paths and other jobs’ server logs are excluded.</p>
  <p className={styles.diagnosticStatus} role="status">{actionStatus||state}</p>
  {!diagnostics&&state.startsWith("Captured output could")&&<button className={styles.copyButton} onClick={()=>{setState("Loading captured output...");setAttempt(value=>value+1)}}>Retry diagnostics</button>}
  {report&&<details className={styles.dataMarkers}>
   <summary>Available data markers ({markers.filter(marker=>marker.value!==null).length} available)</summary>
   <p>Values retain their recorded units. Model confidence is a label, not an accuracy percentage. Unknown data stays unavailable. {timeline.length} timed markers are available for timeline charts.</p>
   <div className={styles.tableWrap}><table><caption>Data available for bars, labels and markers</caption><thead><tr><th>Metric</th><th>Value</th><th>Unit</th><th>Origin and data path</th></tr></thead><tbody>
    {markers.map(marker=><tr key={marker.id}><td>{marker.label}</td><td>{marker.value===null?"Unavailable":String(marker.value)}</td><td>{marker.unit}</td><td>{marker.origin}<br/><code>{marker.path}</code></td></tr>)}
   </tbody></table></div>
  </details>}
  <pre className={styles.diagnosticOutput} tabIndex={0} aria-label="Complete recorded analysis output">{output}</pre>
 </section>;
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

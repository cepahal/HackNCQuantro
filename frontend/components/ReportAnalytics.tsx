"use client";
import { useState, type CSSProperties } from "react";
import { motion, useReducedMotion } from "framer-motion";
import type { ReportInterpretation, TranscriptSentence } from "../lib/api-types";
import type { AnalyticsData } from "../lib/report-analytics";
import type { SourceLead } from "../lib/source-leads";
import { percent, time } from "../lib/format";
import styles from "./ReportAnalytics.module.css";

export interface ReportAnalyticsProps {
  data: AnalyticsData;
  interpretation?: ReportInterpretation | null;
  interpretationState: "loading" | "ready" | "unavailable";
  spliceVideoReady?: boolean;
  sourceEvidence?: Array<TranscriptSentence & {media_id:string}>;
}

export function evidenceLayerPosition(index:number,active:number|null){
  const base=index*24;
  if(active===null)return base;
  return index<active?base-60:index>active?base+120:base;
}

export default function ReportAnalytics({ data, interpretation, interpretationState, spliceVideoReady = false, sourceEvidence = [] }: ReportAnalyticsProps) {
  const reducedMotion = useReducedMotion();
  const [activeLayer,setActiveLayer]=useState<number|null>(null);
  const [pinnedLayer,setPinnedLayer]=useState(false);
  if (!data.isVideoDetected) return null;
  const { metrics, wafer } = data;
  const reviewQuotes=(interpretation?.source_review?.references??[]).map(ref=>sourceEvidence.find(sentence=>sentence.media_id===ref.media_id&&sentence.sentence_id===ref.sentence_id))
    .filter((sentence):sentence is TranscriptSentence & {media_id:string}=>Boolean(sentence?.text.trim()))
    .filter((sentence,index,all)=>all.findIndex(item=>item.media_id===sentence.media_id&&item.sentence_id===sentence.sentence_id)===index).slice(0,4);
  // Only the transcript-grounded Gemini reading is shown; saved resolver prose can echo post titles or uploaders.
  const contextText = interpretation?.status === "ready" ? interpretation.context_gap : null;
  const layers = [
    { available: true, title: "Source-match confidence", value: wafer.confidence, note: "Strength of the recorded speech match." },
    { available: wafer.wordMatches !== null, title: "Word overlap", value: wafer.wordMatches ? `${wafer.wordMatches.matched}/${wafer.wordMatches.total} words` : "Unavailable",
      note: "Exact token overlap in aligned transcript passages." },
    { available: wafer.sourceGaps !== null, title: "Splice assessment", value: wafer.sourceGaps === null ? "Not assessed" : `${wafer.sourceGaps} source ${wafer.sourceGaps === 1 ? "gap" : "gaps"}`,
      note: "Gaps between aligned passages in the source recording." },
    { available: wafer.clipCoverage !== null, title: "Excerpt aligned", value: percent(wafer.clipCoverage),
      note: "Share of the submitted excerpt located in the source recording." },
  ].filter(layer => layer.available).slice(0, 3);
  const summary = interpretation?.status === "ready" ? interpretation.summary : metrics.summary;
  return <section className={styles.analytics} aria-label="Source analysis">
    <article className={`zo-card ${styles.contextCard}`}>
      <h2 className="zo-h2">The context</h2>
      {contextText ? <p className={`zo-body ${styles.context}`}>{contextText}</p>
        : <p className={`zo-meta ${styles.context}`} role="status">{interpretationState === "loading" ? "Preparing the context reading." : "A context reading is not available for this report."}</p>}
      {contextText && <p className={`zo-meta ${styles.caption}`} role="status">Gemini reading of the submitted clip and its surrounding source transcript.</p>}
      {contextText && contextText.length > 260 && <details className={styles.fullReading}><summary>Read the full context analysis</summary><p className="zo-body">{contextText}</p></details>}
      {interpretation?.status==="ready"&&interpretation.source_review&&<section className={styles.sourceReview} aria-label="Source excerpt review" data-review={interpretation.source_review.status}>
        <h3 className="zo-h3">{interpretation.source_review.status==="potential_issue"?"Questionable statements discovered":"No clearly inaccurate data discovered"}</h3>
        {interpretation.source_review.status==="potential_issue"&&<>
          <p className="zo-body">{interpretation.source_review.fact_check}</p>
          {reviewQuotes.length>0&&<details className={styles.reviewEvidence}><summary>Source evidence ({reviewQuotes.length})</summary>{reviewQuotes.map(sentence=><figure key={`${sentence.media_id}:${sentence.sentence_id}`}><blockquote>{sentence.text}</blockquote><figcaption>{time(sentence.start_seconds,true)} to {time(sentence.end_seconds,true)}</figcaption></figure>)}</details>}
        </>}
      </section>}
    </article>
    <article className={`zo-card ${styles.overview}`} aria-label="Recorded evidence">
      <h2 className="zo-h2">Analysis overview</h2>
      <div className={styles.wafer} onMouseLeave={()=>{if(!pinnedLayer)setActiveLayer(null)}} onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget)){setActiveLayer(null);setPinnedLayer(false)}}}>
        <div className={styles.stack} data-testid="evidence-wafer" data-motion={spliceVideoReady && !reducedMotion ? "animated" : "static"}>
          {layers.map((layer,index)=><motion.button type="button" key={layer.title} className={styles.disc}
            aria-label={layer.title} aria-pressed={activeLayer===index} aria-controls={`evidence-layer-${index}`}
            onMouseEnter={()=>{setActiveLayer(index);setPinnedLayer(false)}} onFocus={()=>{setActiveLayer(index);setPinnedLayer(true)}} onClick={()=>{setActiveLayer(index);setPinnedLayer(true)}}
            style={{zIndex:layers.length-index}}
            animate={{top:evidenceLayerPosition(index,activeLayer),
              y:spliceVideoReady&&!reducedMotion?[0,-4,0]:0}}
            transition={reducedMotion?{duration:0}:{top:{duration:.25},y:{duration:5,repeat:Infinity,delay:index*.22,ease:"easeInOut"}}}>
            <div className={styles.grid}/><span className={styles.discNumber}>0{index+1}</span>
            {index===0&&<span className={styles.matchMark}>✓</span>}
          </motion.button>)}
        </div>
        <div className={styles.layers} style={{top:activeLayer===null?0:200+evidenceLayerPosition(activeLayer,activeLayer)+84,"--evidence-label-top":`${activeLayer===null?0:200+evidenceLayerPosition(activeLayer,activeLayer)+84}px`} as CSSProperties}>
          {layers.map((layer,index)=><section key={layer.title} id={`evidence-layer-${index}`} className={styles.layer} hidden={index!==activeLayer}>
            <p className={styles.layerTitle}>{layer.title}</p><h3>{layer.value}</h3><p className={styles.layerNote}>{layer.note}</p>
          </section>)}
        </div>
      </div>
    </article>
    <article className={`zo-card ${styles.summaryCard}`}>
      <h2 className="zo-h2">Quick summary</h2>
      <p className={`zo-body ${styles.summary}`}>{summary}</p>
      {(metrics.generationSeconds !== null || metrics.contextWindows.length > 0) && <dl className={styles.facts}>
        {metrics.generationSeconds !== null && <div><dt>Generation time</dt><dd>{metrics.generationSeconds.toFixed(1)}s</dd><p>Recorded pipeline time</p></div>}
        {metrics.contextWindows.length > 0 && <div><dt>Source context window</dt><dd className={styles.window}>{metrics.contextWindows.map((window, index) => <span key={index}>{time(window.start, true)} to {time(window.end, true)}</span>)}</dd><p>Transcript ranges reviewed</p></div>}
      </dl>}
    </article>
  </section>;
}

export interface SourceOptionsProps {
  options: SourceLead[];
  isVideoDetected: boolean;
  pendingUrl: string | null;
  error: string | null;
  onSelect: (option: SourceLead) => void;
}

export function SourceOptions({ options, isVideoDetected, pendingUrl, error, onSelect }: SourceOptionsProps) {
  return <section className={`zo-card ${styles.options} ${isVideoDetected ? "" : styles.centered}`} aria-label="Alternative source options">
    <p className="zo-label">{isVideoDetected ? "Explore another recording" : "Continue the search"}</p>
    <h2 className="zo-h2">{isVideoDetected ? "Other possible sources" : "A source has not been confirmed yet."}</h2>
    <p className={`zo-meta ${styles.optionsNote}`}>Select a saved candidate to recheck its speech against your submitted clip. </p>
    <div className={styles.optionGrid}>
      {Array.from({ length: 4 }, (_, index) => {
        const option = options[index];
        return <button key={option?.url ?? index} type="button" className={styles.option}
          hidden={!option} disabled={!option || pendingUrl !== null} onClick={() => option && onSelect(option)}
          aria-label={option ? `Recheck ${option.title}` : `Source option ${index + 1} unavailable`}>
          <span className={styles.optionIndex}>0{index + 1}</span><span>
            <span className={styles.optionTitle}>{option?.title ?? "No additional candidate recorded"}</span>
            <span className={styles.optionReason}>{pendingUrl === option?.url ? "Checking transcript and source context..." : option ? "Recheck this recording" : "Unavailable"}</span>
          </span><span aria-hidden="true" className={styles.optionArrow}>{option ? "↗" : "·"}</span>
        </button>;
      })}
    </div>
    {pendingUrl && <p role="status" className={`zo-meta ${styles.optionsNote}`}>The selected source is being checked. The saved report will stay available.</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </section>;
}

"use client";
import { motion, useReducedMotion } from "framer-motion";
import type { ReportInterpretation } from "../lib/api-types";
import type { AnalyticsData } from "../lib/report-analytics";
import type { SourceLead } from "../lib/source-leads";
import { time } from "../lib/format";
import styles from "./ReportAnalytics.module.css";

export interface ReportAnalyticsProps {
  data: AnalyticsData;
  interpretation?: ReportInterpretation | null;
  interpretationState: "loading" | "ready" | "unavailable";
  spliceVideoReady?: boolean;
}

export default function ReportAnalytics({ data, interpretation, interpretationState, spliceVideoReady = false }: ReportAnalyticsProps) {
  const reducedMotion = useReducedMotion();
  if (!data.isVideoDetected) return null;
  const { metrics, wafer } = data;
  const contextText = interpretation?.status === "ready" ? interpretation.context_gap : data.contextText;
  const layers = [
    { available: true, title: "Source-match confidence", value: wafer.confidence, note: "Strength of the recorded speech match, not a truth score." },
    { available: wafer.wordMatches !== null, title: "Word overlap", value: wafer.wordMatches ? `${wafer.wordMatches.matched}/${wafer.wordMatches.total} words` : "Unavailable",
      note: "Exact token overlap in aligned transcript passages. Word order is not assessed." },
    { available: wafer.sourceGaps !== null, title: "Splice assessment", value: wafer.sourceGaps === null ? "Not assessed" : `${wafer.sourceGaps} source ${wafer.sourceGaps === 1 ? "gap" : "gaps"}`,
      note: "Source gaps can suggest cuts. They do not confirm a splice or manipulation." },
    { available: wafer.limitations.length > 0, title: "Detection limitations", value: wafer.limitations.length ? `${wafer.limitations.length} recorded` : "None recorded",
      note: wafer.limitations.length ? wafer.limitations[0] : "No recorded limitations does not establish accuracy or authenticity." },
  ].filter(layer => layer.available);
  return <section className={styles.analytics} aria-label="Source analysis">
    <div className="max-w-3xl"><p className={styles.eyebrow}>The context</p>
      <p className={styles.context}>{contextText}</p>
      <p className={styles.caption} role="status">{interpretationState === "loading" ? "Preparing a concise Gemini reading. Recorded evidence is shown meanwhile."
        : interpretation?.status === "ready" ? "Gemini reading of the saved transcript and source context."
          : "Gemini reading unavailable. Showing the saved analysis."}</p>
      {contextText && contextText.length > 260 && <details className={styles.fullReading}><summary>Read the full context analysis</summary><p>{contextText}</p></details>}
    </div>
    <dl className={`${styles.metrics} grid grid-cols-1 gap-6 sm:grid-cols-3`}>
      {metrics.generationSeconds !== null && <div><dt>Generation time</dt><dd>{metrics.generationSeconds.toFixed(1)}s</dd><p>Recorded pipeline time</p></div>}
      <div><dt>General summary</dt><dd className={styles.summary}>{interpretation?.status === "ready" ? interpretation.summary : metrics.summary}</dd></div>
      {metrics.contextWindows.length > 0 && <div><dt>Source context window</dt><dd className={styles.window}>{metrics.contextWindows.length
        ? metrics.contextWindows.map((window, index) => <span key={index}>{time(window.start, true)} to {time(window.end, true)}</span>) : "Unavailable"}</dd><p>Requested transcript ranges, not guaranteed complete coverage</p></div>}
    </dl>
    <div className={`${styles.wafer} ${spliceVideoReady ? "" : styles.evidenceOnly}`} aria-label="Recorded evidence">
      {spliceVideoReady && <div className={styles.stack} aria-hidden="true" data-testid="splice-animation">
        {layers.map((layer, index) => <motion.div key={layer.title} className={styles.disc}
          style={{ top: `${index * 53}px`, zIndex: 4 - index }}
          animate={reducedMotion ? { y: 0 } : { y: [0, -4, 0] }}
          transition={{ duration: 5, repeat: Infinity, delay: index * .22, ease: "easeInOut" }}>
          <div className={styles.grid}/><span className={styles.discNumber}>0{index + 1}</span>
          {index === 0 && <span className={styles.matchMark}>✓</span>}
        </motion.div>)}
      </div>}
      <ol className={styles.layers}>{layers.map((layer, index) => <li key={layer.title} className={styles.layer}>
        <span className={styles.connector} aria-hidden="true"/><p className={styles.layerTitle}>0{index + 1} / {layer.title}</p>
        <h2>{layer.value}</h2><p className={styles.layerNote}>{layer.note}</p>
      </li>)}</ol>
    </div>
    {wafer.limitations.length > 1 && <details className={styles.limitations}><summary>All detection limitations</summary>
      <ul>{wafer.limitations.map(note => <li key={note}>{note}</li>)}</ul></details>}
    <p className={styles.reliability}>{interpretation?.reliability || "Matching speech does not verify factual accuracy, speaker identity or video authenticity."}</p>
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
  return <section className={`${styles.options} ${isVideoDetected ? "" : styles.centered}`} aria-label="Alternative source options">
    <p className={styles.eyebrow}>{isVideoDetected ? "Explore another recording" : "Continue the search"}</p>
    <h2>{isVideoDetected ? "Other possible sources" : "A source has not been confirmed yet."}</h2>
    <p className={styles.optionsNote}>Select a saved candidate to recheck its speech against your submitted clip. Search leads are not verified matches.</p>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {Array.from({ length: 4 }, (_, index) => {
        const option = options[index];
        return <button key={option?.url ?? index} type="button" className={styles.option}
          hidden={!option} disabled={!option || pendingUrl !== null} onClick={() => option && onSelect(option)}
          aria-label={option ? `Recheck ${option.title}` : `Source option ${index + 1} unavailable`}>
          <span className={styles.optionIndex}>0{index + 1}</span><span>
            <span className={styles.optionTitle}>{option?.title ?? "No additional candidate recorded"}</span>
            <span className={styles.optionReason}>{pendingUrl === option?.url ? "Checking transcript and source context..." : option ? "Unverified lead · Recheck this recording" : "Unavailable"}</span>
          </span><span aria-hidden="true">{option ? "↗" : "·"}</span>
        </button>;
      })}
    </div>
    {pendingUrl && <p role="status" className={styles.optionsNote}>The selected source is being checked. The saved report will stay available.</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </section>;
}

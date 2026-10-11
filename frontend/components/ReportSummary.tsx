import type { ReportV2 } from "../lib/api-types";
import { buildReportSummary, summaryQuoteLabel, summaryTakeaways, summaryText } from "../lib/report-summary";
import styles from "./ReportSummary.module.css";

type Tone = "supported" | "difference" | "neutral";
type Assessment = { label: string; value: string; tone: Tone };

/** A positive or negative icon requires linked evidence, not just a model label. */
export function reportAssessment(report: ReportV2): { title: string; tone: Tone; items: Assessment[] } {
  const aligned = report.outcome === "matched" && report.source !== null
    && report.alignment?.status === "verified" && report.alignment.matches.length > 0
    && report.alignment.source_media_id === report.source.post.media.media_id
    && report.alignment.clip_media_id === report.submitted.post.media.media_id;
  const sourceId = report.source?.post.media.media_id;
  const context = report.context?.source_media_id === sourceId ? report.context : null;
  const sentences = [...(report.source?.transcript.sentences ?? []), ...(context?.windows.flatMap(window => window.sentences) ?? [])];
  const sourceSentences = new Map(sentences.map(sentence => [sentence.sentence_id, sentence.text]));
  const clipSentences = new Map(report.submitted.transcript.sentences.map(sentence => [sentence.sentence_id, sentence.text]));
  const citedMedia = new Set(report.comparison?.findings.flatMap(finding => finding.evidence).filter(evidence => {
    const lookup = evidence.media_id === sourceId ? sourceSentences
      : evidence.media_id === report.submitted.post.media.media_id ? clipSentences : null;
    return lookup && evidence.quote.trim() && evidence.sentence_ids.length > 0
      && evidence.sentence_ids.every(id => lookup.has(id))
      && evidence.sentence_ids.map(id => lookup.get(id)).join(" ").includes(evidence.quote.trim());
  }).map(evidence => evidence.media_id));
  const cited = aligned && report.comparison?.assessment_confidence.level !== "not_assessed"
    && Boolean(context?.windows.some(window => window.sentences.length > 0))
    && Boolean(sourceId && citedMedia.has(sourceId));
  const contextValue = cited && (report.comparison?.context_assessment !== "material_context_missing"
    || citedMedia.has(report.submitted.post.media.media_id)) ? report.comparison?.context_assessment : undefined;
  const captionValue = cited && report.submitted.post.caption?.trim() ? report.comparison?.caption_assessment : undefined;
  const contextItem: Assessment = contextValue === "material_context_missing"
    ? { label: "Source context", value: "Material context missing", tone: "difference" }
    : contextValue === "no_material_difference_found"
      ? { label: "Source context", value: "No material difference found", tone: "supported" }
      : { label: "Source context", value: contextValue === "uncertain" ? "Uncertain" : "Not assessed", tone: "neutral" };
  const captionItem: Assessment = captionValue === "contradicted_by_context"
    ? { label: "Post claim", value: "Contradicted by context", tone: "difference" }
    : captionValue === "supported_by_context"
      ? { label: "Post claim", value: "Supported by context", tone: "supported" }
      : { label: "Post claim", value: !report.submitted.post.caption?.trim() || captionValue === "not_provided"
        ? "No claim supplied" : captionValue === "uncertain" ? "Uncertain" : "Not assessed", tone: "neutral" };
  const items: Assessment[] = [
    { label: "Recording match", value: aligned ? "Matching speech found" : "Source unconfirmed", tone: aligned ? "supported" : "neutral" },
    contextItem, captionItem,
  ];
  if (items.some(item => item.tone === "difference")) return {
    title: captionItem.tone === "difference" ? "The post claim conflicts with source context." : "The excerpt is missing material context.",
    tone: "difference", items,
  };
  if (contextItem.tone === "supported") return { title: "No material difference in the available context.", tone: "supported", items };
  return { title: aligned ? "Source located. Context remains unresolved." : "A source recording has not been confirmed.", tone: "neutral", items };
}

export function AssessmentIcon({ tone }: { tone: Tone }) {
  return <span className={`${styles.icon} ${styles[tone]}`} aria-hidden="true">
    <svg viewBox="0 0 20 20" fill="none">
      {tone === "supported" ? <path d="m5 10 3.2 3.2L15 6.5" />
        : tone === "difference" ? <path d="m6 6 8 8M14 6l-8 8" />
          : <path d="M6 10h8" />}
    </svg>
  </span>;
}

export function ReportVerdict({ report }: { report: ReportV2 }) {
  const assessment = reportAssessment(report);
  return <section className={styles.verdict} aria-label="Report verdict">
    <p className={styles.eyebrow}>The result</p>
    <div className={styles.verdictTitle}><AssessmentIcon tone={assessment.tone} /><h2>{assessment.title}</h2></div>
    <p className={styles.verdictNote}>A comparison of the supplied excerpt, its post claim, and the available recording evidence.</p>
    <ul className={styles.assessments}>{assessment.items.map(item => <li key={item.label} data-assessment={item.tone}>
      <AssessmentIcon tone={item.tone} /><div><span>{item.label}</span><strong>{item.value}</strong></div>
    </li>)}</ul>
  </section>;
}

/** Recognize only the new exact summary labels. Arbitrary saved prose keeps its existing parser. */
export function conciseSummary(report: ReportV2): { takeaways: string; meaning: string; reliability: string } | null {
  const text = report.summary?.trim() || report.comparison?.summary.trim() || "";
  const match = text.match(/^Takeaways:\s*([\s\S]+?)\n\s*\nWhat this means:\s*([\s\S]+?)\n\s*\nReliability:\s*([\s\S]+)$/);
  return match ? { takeaways: match[1].trim(), meaning: match[2].trim(), reliability: match[3].trim() } : null;
}

export default function ReportSummary({ report }: { report: ReportV2 }) {
  const summary = buildReportSummary(report);
  const concise = conciseSummary(report);
  const reading = summary.sections.find(section => section.id === "meaning" || section.id === "summary");
  const evidence = summary.sections.find(section => section.id === "evidence");
  const references = summary.sections.find(section => section.id === "references");
  const caption = summary.sections.find(section => section.id === "caption");
  const finding = summary.sections.find(section => section.id === "finding");
  const limits = summary.sections.find(section => section.id === "limits");
  return <section className={styles.summary} aria-label="Analysis summary">
    <section className={`${styles.section} ${styles.takeaways}`} aria-label="Takeaways">
      <div className={styles.sectionHead}><h2>Takeaways</h2>{summary.partialTranscript && <span className={styles.badge}>Partial transcript</span>}</div>
      {(concise ? [concise.takeaways] : summaryTakeaways(reading)).map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
      {!concise && reading?.deferredReading && <details className={styles.details}>
        <summary>Read the full summary</summary>
        {reading.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
      </details>}
      {reading?.note && <p className={styles.note}>{summaryText(reading.note)}</p>}
    </section>
    <div className={styles.sections}>
      <section className={styles.section} aria-label="What this means">
        <h2>What this means</h2>
        {concise ? <p>{summaryText(concise.meaning)}</p> : caption
          ? caption.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)
          : <p>{report.outcome === "no_verified_match"
            ? "The excerpt can be read on its own, but a source comparison is unavailable."
            : "The result describes the relationship to the available source context. It does not establish whether the underlying claim is true."}</p>}
        {summary.metadataClues.length > 0 && <details className={styles.details}>
          <summary>How the post frames the clip</summary>
          {summary.metadataClues.map(item => <p key={item.label}><strong>{item.label}:</strong> {summaryText(item.text)}</p>)}
          <p className={styles.note}>The title and caption are supplied claims. They do not verify a speaker’s identity.</p>
        </details>}
        {references && <details className={styles.details}>
          <summary>Possible references, unverified</summary>
          {references.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
          {references.note && <p className={styles.note}>{summaryText(references.note)}</p>}
        </details>}
      </section>
      <section className={styles.section} aria-label="Reliability">
        <h2>Reliability</h2>
        {concise ? <p>{summaryText(concise.reliability)}</p>
          : <ul className={styles.reliability}>{summary.evidenceStatus.slice(0, 2).map(item => <li key={item.label}>{summaryText(item.text)}</li>)}</ul>}
        <p className={styles.note}>Matching words do not establish factual accuracy, intent, or recording authenticity.</p>
        {(limits || finding) && <details className={styles.details}>
          <summary>Assessment and limits</summary>
          {finding?.paragraphs.map((paragraph, index) => <p key={`finding-${index}`}>{summaryText(paragraph)}</p>)}
          {limits?.paragraphs.map((paragraph, index) => <p key={`limit-${index}`}>{summaryText(paragraph)}</p>)}
          {limits?.note && <p className={styles.note}>{summaryText(limits.note)}</p>}
        </details>}
      </section>
    </div>
    {evidence && <section className={`${styles.section} ${styles.evidenceSection}`} aria-label="Transcript evidence">
      <h2>Transcript evidence</h2>
      {evidence.quotes.slice(0, 3).map(quote => <figure className={styles.evidence} key={quote.sentence_id}>
        <blockquote>“{summaryText(quote.text)}”</blockquote>
        <figcaption>{summaryQuoteLabel(quote, report.submitted.transcript.status === "partial")}</figcaption>
      </figure>)}
      {(evidence.paragraphs.length > 0 || evidence.quotes.length > 3) && <details className={styles.details}>
        <summary>Additional quoted evidence</summary>
        {evidence.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
        {evidence.quotes.slice(3).map(quote => <figure className={styles.evidence} key={quote.sentence_id}>
          <blockquote>“{summaryText(quote.text)}”</blockquote><figcaption>{summaryQuoteLabel(quote, report.submitted.transcript.status === "partial")}</figcaption>
        </figure>)}
      </details>}
      {evidence.note && <p className={styles.note}>{summaryText(evidence.note)}</p>}
    </section>}
  </section>;
}

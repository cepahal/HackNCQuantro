import type { ReportV2 } from "../lib/api-types";
import { buildReportSummary, summaryQuoteLabel, summaryText } from "../lib/report-summary";
import styles from "./ReportSummary.module.css";

export default function ReportSummary({ report }: { report: ReportV2 }) {
  const summary = buildReportSummary(report);
  const reading = summary.sections.find(section => section.id === "meaning" || section.id === "summary");
  const evidence = summary.sections.find(section => section.id === "evidence");
  const references = summary.sections.find(section => section.id === "references");
  const caption = summary.sections.find(section => section.id === "caption");
  const finding = summary.sections.find(section => section.id === "finding");
  const limits = summary.sections.find(section => section.id === "limits");
  return <section className={styles.summary} aria-label="Analysis summary">
    {(summary.politicalAssumption || summary.partialTranscript) && <div className={styles.badges}>
      {summary.politicalAssumption && <span>Interpretation, not a fact check</span>}
      {summary.partialTranscript && <span>Partial transcript</span>}
    </div>}
    <div className={styles.sections}>
      <section className={styles.section} aria-label="Takeaways">
        <h2>Takeaways</h2>
        {summary.evidenceStatus.slice(0, 2).map(item => <p key={item.label}>{summaryText(item.text)}</p>)}
      </section>
      <section className={styles.section} aria-label="What this means">
        <h2>What this means</h2>
        {reading?.deferredReading ? <>
          <p>The model&apos;s interpretation is tentative. Its full reading is available below.</p>
          <details className={styles.details}>
            <summary>Full model reading</summary>
            {reading.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
          </details>
        </> : reading?.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
        {reading?.note && <p className={styles.note}>Assumption: {summaryText(reading.note)}</p>}
        {caption?.paragraphs.map((paragraph, index) => <p key={`caption-${index}`}>{summaryText(paragraph)}</p>)}
        {references && <details className={styles.details}>
          <summary>Model speculation (unverified)</summary>
          <ul className={styles.hypotheses}>
            {references.paragraphs.flatMap(paragraph => paragraph.split(";")).map((item, index) =>
              item.trim() && <li key={index}>{summaryText(item.trim())}</li>)}
          </ul>
          {references.note && <p className={styles.note}>Unverified: {summaryText(references.note)}</p>}
        </details>}
      </section>
      <section className={styles.section} aria-label="Reliability">
        <h2>Reliability</h2>
        <p>{summaryText(summary.evidenceStatus[2].text)}</p>
        {limits?.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
        {limits?.note && <p className={styles.note}>{summaryText(limits.note)}</p>}
        {finding && <details className={styles.details}>
          <summary>Full pipeline reading</summary>
          {finding.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
          {finding.note && <p className={styles.note}>{summaryText(finding.note)}</p>}
        </details>}
      </section>
      {evidence && <section className={`${styles.section} ${styles.wide}`} aria-label="Evidence">
        <h2>Evidence</h2>
        {evidence.paragraphs.map((paragraph, index) => <p key={index}>{summaryText(paragraph)}</p>)}
        {evidence.quotes.map(quote => <figure className={styles.evidence} key={quote.sentence_id}>
          <blockquote>{summaryText(quote.text)}</blockquote>
          <figcaption>{summaryQuoteLabel(quote, report.submitted.transcript.status === "partial")}</figcaption>
        </figure>)}
        {evidence.note && <p className={styles.note}>{summaryText(evidence.note)}</p>}
      </section>}
    </div>
  </section>;
}

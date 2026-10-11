import type { ReportV2 } from "../lib/api-types";
import { sourceLeads } from "../lib/source-leads";
import styles from "./ReportSummary.module.css";

export default function SourceLeads({ report }: { report: ReportV2 }) {
  const leads = sourceLeads(report);
  if (!leads.length) return null;
  return <section className="report-section" aria-label="Possible source recordings">
    <h2>Possible source recordings.</h2>
    <p className="step-copy">Browse the stronger and weaker search leads below. Some may be related or longer recordings; others may be irrelevant. None is a confirmed original or verified context.</p>
    <div className="claim-grid">{leads.map(lead => <article className="claim-box" key={lead.url}>
      <h3>{lead.title}</h3>
      <p className="step-copy">{lead.reason}</p>
      <a href={lead.url} target="_blank" rel="noopener noreferrer">Watch this candidate on YouTube ↗</a>
      <details className={styles.details}>
        <summary>Preview unverified candidate</summary>
        <iframe src={lead.preview} title={`Unverified candidate: ${lead.title}`} loading="lazy"
          style={{ width: "100%", aspectRatio: "16 / 9", border: 0, marginTop: 12 }}
          referrerPolicy="strict-origin-when-cross-origin"
          allow="accelerometer; encrypted-media; gyroscope; picture-in-picture; web-share" allowFullScreen />
      </details>
    </article>)}</div>
  </section>;
}

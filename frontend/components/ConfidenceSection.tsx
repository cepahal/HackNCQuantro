import type { AssessmentBlock, ClipCoverageBlock, DisplayModel } from "../lib/displayModel";

type ConfidenceSectionProps = Pick<
  DisplayModel,
  | "source_match"
  | "clip_coverage"
  | "context_assessment"
  | "confidence_note"
  | "no_source_message"
  | "context_unavailable_message"
>;

function ListOrNothing({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <h4>{title}</h4>
      <ul>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function AssessmentCard({ block, note }: { block: AssessmentBlock; note?: string | null }) {
  return (
    <article aria-label={block.label}>
      <h3>{block.label}</h3>
      <p>{block.value}</p>
      {note ? <p role="note">{note}</p> : null}
      <ListOrNothing title="Basis" items={block.basis} />
      <ListOrNothing title="Limitations" items={block.limitations} />
    </article>
  );
}

function ClipCoverageCard({ block }: { block: ClipCoverageBlock }) {
  return (
    <article aria-label={block.label}>
      <h3>{block.label}</h3>
      <p>{block.value}</p>
      <p>{block.description}</p>
    </article>
  );
}

export default function ConfidenceSection({
  source_match,
  clip_coverage,
  context_assessment,
  confidence_note,
  no_source_message,
  context_unavailable_message,
}: ConfidenceSectionProps) {
  return (
    <section aria-label="Confidence">
      {no_source_message ? <p role="status">{no_source_message}</p> : null}
      {source_match ? <AssessmentCard block={source_match} /> : null}
      {clip_coverage ? <ClipCoverageCard block={clip_coverage} /> : null}
      {context_assessment ? (
        <AssessmentCard block={context_assessment} note={context_unavailable_message} />
      ) : null}
      <p>{confidence_note}</p>
    </section>
  );
}

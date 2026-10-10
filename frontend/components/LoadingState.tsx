export default function LoadingState({ message = "Loading the report…" }: { message?: string }) {
  return <div className="notice" role="status" aria-live="polite">{message}</div>;
}

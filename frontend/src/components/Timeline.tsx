import type { StreamedEvent } from "../hooks/useTaskEventStream";

// TODO(skeleton): every event renders the same generic line, ignoring effect/matched_rules
// and every other event-specific field. Filled in next commit.
export function Timeline({ events }: { events: StreamedEvent[] }) {
  if (events.length === 0) {
    return <p>Nenhum evento ainda.</p>;
  }
  return (
    <ol>
      {events.map((event) => (
        <li key={event.seq}>{event.type}</li>
      ))}
    </ol>
  );
}

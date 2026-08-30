const formatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function formatTimestamp(value: string | null): string {
  if (value === null) return "Waiting…";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : formatter.format(date);
}

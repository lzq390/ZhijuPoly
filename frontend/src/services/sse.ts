export type SseEvent = { event: string; data: Record<string, unknown> };

export function parseSseBlock(block: string): SseEvent | null {
  const lines = block.replace(/\r\n/g, "\n").split("\n");
  let event = "message";
  const data: string[] = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  if (data.length === 0) return null;
  const parsed = JSON.parse(data.join("\n"));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("AI stream event data must be a JSON object.");
  }
  return { event, data: parsed as Record<string, unknown> };
}

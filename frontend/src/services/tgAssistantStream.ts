import { API_BASE_URL, ApiRequestError } from "./api";
import type { TgAssistantStreamRequest } from "../types";

export type TgAssistantSseEvent = {
  event: string;
  data: Record<string, unknown>;
};

export type TgAssistantImageAttachments = {
  canvasImage?: Blob;
  userImage?: File;
};

export function parseTgAssistantSseBlock(block: string): TgAssistantSseEvent | null {
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

async function streamErrorMessage(response: Response) {
  const data = await response.json().catch(() => null);
  return typeof data?.detail === "string"
    ? data.detail
    : `AI request failed with status ${response.status}`;
}

export async function streamTgAssistant(
  payload: TgAssistantStreamRequest,
  onEvent: (event: TgAssistantSseEvent) => void,
  signal: AbortSignal,
  images?: TgAssistantImageAttachments
) {
  const hasImages = Boolean(images?.canvasImage || images?.userImage);
  const form = hasImages ? new FormData() : null;
  if (form) {
    form.append("payload", JSON.stringify(payload));
    if (images?.canvasImage) form.append("canvas_image", images.canvasImage, "tg-canvas.png");
    if (images?.userImage) form.append("image", images.userImage);
  }
  const response = await fetch(
    `${API_BASE_URL}/assistant/tg/chat/${hasImages ? "image-stream" : "stream"}`,
    {
      method: "POST",
      ...(form
        ? { body: form }
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
          }),
      signal
    }
  );
  if (!response.ok) {
    throw new ApiRequestError(response.status, await streamErrorMessage(response));
  }
  if (!response.body) {
    throw new Error("AI stream response did not include a body.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseTgAssistantSseBlock(block);
        if (event) {
          onEvent(event);
          if (event.event === "done" || event.event === "error") {
            terminal = true;
            break;
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
      if (terminal) {
        await reader.cancel();
        return;
      }
      if (done) break;
    }
    const finalEvent = parseTgAssistantSseBlock(buffer.trim());
    if (finalEvent) {
      onEvent(finalEvent);
      if (finalEvent.event === "done" || finalEvent.event === "error") terminal = true;
    }
    if (!terminal && !signal.aborted) {
      throw new Error("AI stream ended before a terminal event.");
    }
  } finally {
    reader.releaseLock();
  }
}

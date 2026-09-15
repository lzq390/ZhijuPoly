import { API_BASE_URL, ApiRequestError } from "./api";
import { parseSseBlock } from "./sse";
import type { KnowledgeRecordingSummary } from "../types";

function completedSummary(value: unknown, recordingId: string): KnowledgeRecordingSummary {
  const data = value as Partial<KnowledgeRecordingSummary> | null;
  if (!data || data.recording_id !== recordingId || typeof data.summary !== "string" || !data.summary.trim() || typeof data.generated !== "boolean") {
    throw new Error("总结服务未返回完整结果，请重试总结");
  }
  return data as KnowledgeRecordingSummary;
}

export async function streamRecordingSummary(recordingId: string, onPartial: (text: string) => void): Promise<KnowledgeRecordingSummary> {
  const response = await fetch(`${API_BASE_URL}/knowledge/recordings/${encodeURIComponent(recordingId)}/summary`, {
    method: "POST", headers: { "Accept": "text/event-stream", "Content-Type": "application/json" }, body: "{}"
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new ApiRequestError(response.status, typeof error?.detail === "string" ? error.detail : "总结请求失败，请稍后重试");
  }
  // During rolling updates an older backend can still answer with the JSON contract.
  if (response.headers.get("content-type")?.includes("application/json")) {
    return completedSummary(await response.json(), recordingId);
  }
  if (!response.body) throw new Error("总结响应为空，请重试总结");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let partial = "";
  let result: KnowledgeRecordingSummary | undefined;
  function consume(block: string) {
    const event = parseSseBlock(block);
    if (event?.event === "delta") {
      if (typeof event.data.text !== "string") throw new Error("总结数据格式异常，请重试总结");
      partial += event.data.text;
      onPartial(partial);
    } else if (event?.event === "done") {
      result = completedSummary(event.data, recordingId);
    } else if (event?.event === "error") {
      throw new Error(typeof event.data.detail === "string" ? event.data.detail : "总结生成失败，请重试总结");
    }
  }
  try {
    while (!result) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        consume(block);
        if (result) return result;
      }
      if (done) {
        if (buffer.trim()) consume(buffer);
        if (result) return result;
        throw new Error("总结连接中断，已生成的内容尚未完成，请重试总结");
      }
    }
    return result;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

import { useCallback, useEffect, useRef, useState } from "react";
import { streamAssistantChat, streamAssistantImageChat } from "../services/api";
import type {
  AssistantChatContext,
  AssistantChatMessage,
  AssistantSkillCall,
  AssistantSkillErrorEvent,
  AssistantSkillResultEvent,
  AssistantSkillStartEvent
} from "../types";

const STORAGE_KEY = "zhijupoly.assistantChat.v1";
const STOPPED_MESSAGE = "已停止生成。";

type StoredAssistantMessage = AssistantChatMessage & {
  id: string;
  createdAt: string;
  requestContent?: string;
  skillCalls?: AssistantSkillCall[];
};

function createMessage(
  role: AssistantChatMessage["role"],
  content: string,
  requestContent?: string
): StoredAssistantMessage {
  return {
    id: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now() + Math.random()),
    role,
    content,
    ...(requestContent && requestContent !== content ? { requestContent } : {}),
    createdAt: new Date().toISOString()
  };
}

function loadStoredMessages(): StoredAssistantMessage[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(
      (item): item is StoredAssistantMessage =>
        typeof item?.id === "string" &&
        (item.role === "user" || item.role === "assistant") &&
        typeof item.content === "string" &&
        typeof item.createdAt === "string" &&
        (item.requestContent === undefined || typeof item.requestContent === "string")
    );
  } catch {
    return [];
  }
}

function upsertSkillCall(
  message: StoredAssistantMessage,
  skillCall: AssistantSkillCall
): StoredAssistantMessage {
  const skillCalls = message.skillCalls ?? [];
  const existingIndex = skillCalls.findIndex((item) => item.skill_call_id === skillCall.skill_call_id);
  const nextSkillCalls =
    existingIndex === -1
      ? [...skillCalls, skillCall]
      : skillCalls.map((item, index) => (index === existingIndex ? { ...item, ...skillCall } : item));
  return { ...message, skillCalls: nextSkillCalls };
}

function applySkillStart(message: StoredAssistantMessage, payload: AssistantSkillStartEvent): StoredAssistantMessage {
  return upsertSkillCall(message, {
    skill_call_id: payload.skill_call_id,
    skill_name: payload.skill_name,
    display_name: payload.display_name,
    arguments: payload.arguments,
    status: "running"
  });
}

function applySkillResult(message: StoredAssistantMessage, payload: AssistantSkillResultEvent): StoredAssistantMessage {
  return upsertSkillCall(message, {
    skill_call_id: payload.skill_call_id,
    skill_name: payload.skill_name,
    display_name: payload.display_name,
    status: "completed",
    result: payload.result
  });
}

function applySkillError(message: StoredAssistantMessage, payload: AssistantSkillErrorEvent): StoredAssistantMessage {
  return upsertSkillCall(message, {
    skill_call_id: payload.skill_call_id ?? `skill-error-${payload.skill_name}-${Date.now()}`,
    skill_name: payload.skill_name,
    status: "error",
    error: payload.detail
  });
}

function failRunningSkillCalls(message: StoredAssistantMessage, detail: string): StoredAssistantMessage {
  const skillCalls = message.skillCalls?.map((skillCall) =>
    skillCall.status === "running" ? { ...skillCall, status: "error" as const, error: detail } : skillCall
  );
  return skillCalls ? { ...message, skillCalls } : message;
}

function isAbortError(caught: unknown): boolean {
  return (
    typeof caught === "object" &&
    caught !== null &&
    "name" in caught &&
    (caught as { name?: string }).name === "AbortError"
  );
}

export function useAssistantChat() {
  const [messages, setMessages] = useState<StoredAssistantMessage[]>(loadStoredMessages);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
  }, [messages]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsStreaming(false);
  }, []);

  const clearMessages = useCallback(() => {
    stopStreaming();
    setError(null);
    setMessages([]);
  }, [stopStreaming]);

  const sendMessage = useCallback(
    async (content: string, context: AssistantChatContext, requestContent?: string) => {
      const text = content.trim();
      const requestText = (requestContent ?? content).trim();
      if (!text || !requestText || isStreaming) {
        return;
      }

      const userMessage = createMessage("user", text, requestText);
      const assistantMessage = createMessage("assistant", "");
      const nextMessages = [...messages, userMessage, assistantMessage];
      const requestMessages = [...messages, userMessage]
        .filter((message) => message.role === "user" || message.content.trim())
        .map((message) => ({
          role: message.role,
          content: message.requestContent?.trim() || message.content
        }));
      const controller = new AbortController();

      abortRef.current = controller;
      setError(null);
      setIsStreaming(true);
      setMessages(nextMessages);

      const updateAssistantMessage = (updater: (message: StoredAssistantMessage) => StoredAssistantMessage) => {
        setMessages((current) =>
          current.map((message) => (message.id === assistantMessage.id ? updater(message) : message))
        );
      };

      try {
        await streamAssistantChat(
          {
            messages: requestMessages,
            context
          },
          {
            signal: controller.signal,
            onToken: (token) => {
              updateAssistantMessage((message) => ({ ...message, content: message.content + token }));
            },
            onDone: (message) => {
              if (!message) {
                return;
              }
              updateAssistantMessage((item) => ({ ...item, content: message }));
            },
            onError: (detail) => {
              setError(detail);
            },
            onSkillStart: (payload) => {
              updateAssistantMessage((message) => applySkillStart(message, payload));
            },
            onSkillResult: (payload) => {
              updateAssistantMessage((message) => applySkillResult(message, payload));
            },
            onSkillError: (payload) => {
              updateAssistantMessage((message) => applySkillError(message, payload));
            }
          }
        );
      } catch (caught) {
        if (isAbortError(caught)) {
          setMessages((current) =>
            current.map((message) => {
              if (message.id !== assistantMessage.id) {
                return message;
              }
              const nextMessage = !message.content ? { ...message, content: STOPPED_MESSAGE } : message;
              return failRunningSkillCalls(nextMessage, STOPPED_MESSAGE);
            })
          );
          return;
        }
        const detail = caught instanceof Error ? caught.message : "Assistant chat failed.";
        setError(detail);
        setMessages((current) =>
          current.map((message) => {
            if (message.id !== assistantMessage.id) {
              return message;
            }
            const nextMessage = !message.content
              ? { ...message, content: "抱歉，助手暂时无法回复。" }
              : message;
            return failRunningSkillCalls(nextMessage, detail);
          })
        );
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
        setIsStreaming(false);
      }
    },
    [isStreaming, messages]
  );

  const sendImageMessage = useCallback(
    async (content: string, context: AssistantChatContext, image: File) => {
      const text = content.trim();
      if (!text || isStreaming) {
        return;
      }

      const userMessage = createMessage("user", text);
      const assistantMessage = createMessage("assistant", "");
      const nextMessages = [...messages, userMessage, assistantMessage];
      const requestMessages = [...messages, userMessage]
        .filter((message) => message.role === "user" || message.content.trim())
        .map((message) => ({
          role: message.role,
          content: message.requestContent?.trim() || message.content
        }));
      const controller = new AbortController();

      abortRef.current = controller;
      setError(null);
      setIsStreaming(true);
      setMessages(nextMessages);

      const updateAssistantMessage = (updater: (message: StoredAssistantMessage) => StoredAssistantMessage) => {
        setMessages((current) =>
          current.map((message) => (message.id === assistantMessage.id ? updater(message) : message))
        );
      };

      try {
        await streamAssistantImageChat(
          {
            messages: requestMessages,
            context
          },
          image,
          {
            signal: controller.signal,
            onToken: (token) => {
              updateAssistantMessage((message) => ({ ...message, content: message.content + token }));
            },
            onDone: (message) => {
              if (!message) {
                return;
              }
              updateAssistantMessage((item) => ({ ...item, content: message }));
            },
            onError: (detail) => {
              setError(detail);
            },
            onSkillStart: (payload) => {
              updateAssistantMessage((message) => applySkillStart(message, payload));
            },
            onSkillResult: (payload) => {
              updateAssistantMessage((message) => applySkillResult(message, payload));
            },
            onSkillError: (payload) => {
              updateAssistantMessage((message) => applySkillError(message, payload));
            }
          }
        );
      } catch (caught) {
        if (isAbortError(caught)) {
          setMessages((current) =>
            current.map((message) => {
              if (message.id !== assistantMessage.id) {
                return message;
              }
              const nextMessage = !message.content ? { ...message, content: STOPPED_MESSAGE } : message;
              return failRunningSkillCalls(nextMessage, STOPPED_MESSAGE);
            })
          );
          return;
        }
        const detail = caught instanceof Error ? caught.message : "Assistant image chat failed.";
        setError(detail);
        setMessages((current) =>
          current.map((message) => {
            if (message.id !== assistantMessage.id) {
              return message;
            }
            const nextMessage = !message.content
              ? { ...message, content: "抱歉，助手暂时无法分析图片。" }
              : message;
            return failRunningSkillCalls(nextMessage, detail);
          })
        );
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
        setIsStreaming(false);
      }
    },
    [isStreaming, messages]
  );

  return {
    messages,
    isStreaming,
    error,
    sendMessage,
    sendImageMessage,
    stopStreaming,
    clearMessages
  };
}

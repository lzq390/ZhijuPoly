import { useCallback, useEffect, useRef, useState } from "react";
import { streamAssistantChat } from "../services/api";
import type { AssistantChatContext, AssistantChatMessage } from "../types";

const STORAGE_KEY = "zhijupoly.assistantChat.v1";

type StoredAssistantMessage = AssistantChatMessage & {
  id: string;
  createdAt: string;
};

function createMessage(role: AssistantChatMessage["role"], content: string): StoredAssistantMessage {
  return {
    id: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now() + Math.random()),
    role,
    content,
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
        typeof item.createdAt === "string"
    );
  } catch {
    return [];
  }
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
    async (content: string, context: AssistantChatContext) => {
      const text = content.trim();
      if (!text || isStreaming) {
        return;
      }

      const userMessage = createMessage("user", text);
      const assistantMessage = createMessage("assistant", "");
      const nextMessages = [...messages, userMessage, assistantMessage];
      const requestMessages = [...messages, userMessage].map(({ role, content }) => ({ role, content }));
      const controller = new AbortController();

      abortRef.current = controller;
      setError(null);
      setIsStreaming(true);
      setMessages(nextMessages);

      try {
        await streamAssistantChat(
          {
            messages: requestMessages,
            context
          },
          {
            signal: controller.signal,
            onToken: (token) => {
              setMessages((current) =>
                current.map((message) =>
                  message.id === assistantMessage.id ? { ...message, content: message.content + token } : message
                )
              );
            },
            onDone: (message) => {
              if (!message) {
                return;
              }
              setMessages((current) =>
                current.map((item) => (item.id === assistantMessage.id ? { ...item, content: message } : item))
              );
            },
            onError: (detail) => {
              setError(detail);
            }
          }
        );
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") {
          return;
        }
        const detail = caught instanceof Error ? caught.message : "Assistant chat failed.";
        setError(detail);
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantMessage.id && !message.content
              ? { ...message, content: "抱歉，助手暂时无法回复。" }
              : message
          )
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
    stopStreaming,
    clearMessages
  };
}

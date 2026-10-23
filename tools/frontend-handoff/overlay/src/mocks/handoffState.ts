/** Cancellable streams shared by the browser handoff fixtures. */
export type MockEvent = { event: string; data: Record<string, unknown> };
export function encodeMockEvent(event: MockEvent): Uint8Array {
  return new TextEncoder().encode(`event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`);
}

/** Finite streams end with the caller's explicit done event. Task streams remain
 * open until abort/cancel, with updates published by state changes (no polling). */
export function mockEventStream(
  events: MockEvent[],
  signal?: AbortSignal | null,
  subscribe?: (push: (event: MockEvent) => void) => () => void
): Response {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let unsubscribe: (() => void) | undefined;
  let abort: () => void = () => {};
  const cleanup = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(timer);
    const detach = unsubscribe;
    unsubscribe = undefined;
    signal?.removeEventListener("abort", abort);
    detach?.();
  };
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      abort = () => {
        if (stopped) return;
        cleanup();
        controller.error(new DOMException("The operation was aborted.", "AbortError"));
      };
      signal?.addEventListener("abort", abort, { once: true });
      if (signal?.aborted) { abort(); return; }
      const push = (event: MockEvent) => { if (!stopped) controller.enqueue(encodeMockEvent(event)); };
      if (subscribe) {
        events.forEach(push);
        try {
          const detach = subscribe(push);
          if (stopped) detach();
          else unsubscribe = detach;
        } catch (error) {
          cleanup();
          controller.error(error);
        }
      } else {
        let index = 0;
        const next = () => {
          if (stopped) return;
          if (index < events.length) {
            push(events[index++]);
            timer = setTimeout(next, 35);
          } else {
            cleanup();
            controller.close();
          }
        };
        next();
      }
    },
    cancel() { cleanup(); }
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } });
}

import {
  OPENSCIENCE_BRIDGE_NAMESPACE,
  OPENSCIENCE_BRIDGE_VERSION,
  resolveAgentWorkspaceOrigin
} from "./openScienceProjectBridge";

export type OpenScienceGeneralSessionSummary = {
  id: string;
  title: string;
  updatedAt: number;
};

export type OpenScienceGeneralSessionsSnapshot = {
  namespace: typeof OPENSCIENCE_BRIDGE_NAMESPACE;
  version: typeof OPENSCIENCE_BRIDGE_VERSION;
  type: "general.sessions.snapshot";
  sessions: OpenScienceGeneralSessionSummary[];
  activeSessionID: string | null;
};

type BridgeWindow = {
  postMessage(message: unknown, targetOrigin: string): void;
};

type BridgeMessageEvent = {
  data: unknown;
  origin: string;
  source: unknown;
};

type CreateOpenScienceGeneralSessionBridgeOptions = {
  workspaceUrl: string;
  getFrameWindow: () => BridgeWindow | null;
  onSnapshot: (snapshot: OpenScienceGeneralSessionsSnapshot) => void;
};

export function parseOpenScienceGeneralSessionsSnapshot(
  value: unknown
): OpenScienceGeneralSessionsSnapshot | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  if (value.namespace !== OPENSCIENCE_BRIDGE_NAMESPACE) {
    return undefined;
  }
  if (value.version !== OPENSCIENCE_BRIDGE_VERSION || value.type !== "general.sessions.snapshot") {
    return undefined;
  }
  if (!Array.isArray(value.sessions)) {
    return undefined;
  }

  const sessions: OpenScienceGeneralSessionSummary[] = [];
  const sessionIDs = new Set<string>();
  for (const session of value.sessions) {
    const parsed = parseSessionSummary(session);
    if (!parsed || sessionIDs.has(parsed.id)) {
      return undefined;
    }
    sessionIDs.add(parsed.id);
    sessions.push(parsed);
  }

  if (
    value.activeSessionID !== null &&
    (typeof value.activeSessionID !== "string" || !sessionIDs.has(value.activeSessionID))
  ) {
    return undefined;
  }

  return {
    namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
    version: OPENSCIENCE_BRIDGE_VERSION,
    type: "general.sessions.snapshot",
    sessions,
    activeSessionID: value.activeSessionID
  };
}

export function createOpenScienceGeneralSessionBridge(
  options: CreateOpenScienceGeneralSessionBridgeOptions
) {
  const workspaceOrigin = resolveAgentWorkspaceOrigin(options.workspaceUrl);
  let visibleSessionIDs = new Set<string>();

  function post(message: unknown): boolean {
    const frameWindow = options.getFrameWindow();
    if (!workspaceOrigin || !frameWindow) {
      return false;
    }
    frameWindow.postMessage(message, workspaceOrigin);
    return true;
  }

  function postForVisibleSession(sessionID: string, message: unknown): boolean {
    const normalizedSessionID = sessionID.trim();
    if (!normalizedSessionID || !visibleSessionIDs.has(normalizedSessionID)) {
      return false;
    }
    return post(message);
  }

  return {
    handleMessage(event: BridgeMessageEvent) {
      const frameWindow = options.getFrameWindow();
      if (!workspaceOrigin || !frameWindow) {
        return;
      }
      if (event.source !== frameWindow || event.origin !== workspaceOrigin) {
        return;
      }

      const snapshot = parseOpenScienceGeneralSessionsSnapshot(event.data);
      if (!snapshot) {
        return;
      }

      visibleSessionIDs = new Set(snapshot.sessions.map((session) => session.id));
      options.onSnapshot(snapshot);
    },
    requestSessions() {
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "general.sessions.request"
      });
    },
    newSession() {
      return post({
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "general.session.new"
      });
    },
    openSession(sessionID: string) {
      const normalizedSessionID = sessionID.trim();
      return postForVisibleSession(normalizedSessionID, {
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "general.session.open",
        sessionID: normalizedSessionID
      });
    },
    renameSession(sessionID: string, title: string) {
      const normalizedSessionID = sessionID.trim();
      const normalizedTitle = title.trim();
      if (!normalizedTitle) {
        return false;
      }
      return postForVisibleSession(normalizedSessionID, {
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "general.session.rename",
        sessionID: normalizedSessionID,
        title: normalizedTitle
      });
    },
    deleteSession(sessionID: string) {
      const normalizedSessionID = sessionID.trim();
      return postForVisibleSession(normalizedSessionID, {
        namespace: OPENSCIENCE_BRIDGE_NAMESPACE,
        version: OPENSCIENCE_BRIDGE_VERSION,
        type: "general.session.delete",
        sessionID: normalizedSessionID
      });
    }
  };
}

function parseSessionSummary(value: unknown): OpenScienceGeneralSessionSummary | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  if (typeof value.id !== "string" || !value.id.trim()) {
    return undefined;
  }
  if (typeof value.title !== "string") {
    return undefined;
  }
  if (typeof value.updatedAt !== "number" || !Number.isFinite(value.updatedAt)) {
    return undefined;
  }

  return {
    id: value.id.trim(),
    title: value.title,
    updatedAt: value.updatedAt
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

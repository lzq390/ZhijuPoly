import { beforeEach } from "vitest";
import { installSession, retireSession } from "./session";
import { observeUsageEnabled } from "./usageAdmission";
// Existing private-component unit tests exercise the authenticated workspace.
// Auth boundary tests explicitly replace this fixture with a guest session.
beforeEach(() => {
  retireSession();
  installSession({ authenticated: true, user: { id: "test-user", username: "test", must_change_password: false },
    session_id: "test-session", csrf_token: "test-csrf", capabilities: {} });
  // Legacy component suites model the documented default-off configuration.
  // Admission suites explicitly install a fresh identity and test enabled probes.
  observeUsageEnabled({ enabled: false });
});

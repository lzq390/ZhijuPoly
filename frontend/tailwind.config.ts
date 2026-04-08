import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "#f3f6fb",
        foreground: "#0f172a",
        card: "#ffffff",
        cardForeground: "#0f172a",
        primary: "#2563eb",
        primaryForeground: "#eff6ff",
        secondary: "#dbeafe",
        secondaryForeground: "#1d4ed8",
        muted: "#eef2f7",
        mutedForeground: "#64748b",
        border: "rgba(148, 163, 184, 0.24)",
        input: "rgba(148, 163, 184, 0.32)",
        accent: "#f8fafc",
        accentForeground: "#334155",
        destructive: "#dc2626",
        destructiveForeground: "#fef2f2",
        ring: "#2563eb"
      },
      borderRadius: {
        lg: "1.25rem",
        md: "0.875rem",
        sm: "0.625rem"
      },
      boxShadow: {
        soft: "0 20px 45px rgba(15, 23, 42, 0.06)",
        panel: "0 10px 30px rgba(15, 23, 42, 0.04)"
      }
    }
  },
  plugins: []
} satisfies Config;

import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "#f4efe8",
        foreground: "#1f2937",
        card: "#fffdf8",
        cardForeground: "#111827",
        primary: "#0f766e",
        primaryForeground: "#f8fafc",
        secondary: "#c2410c",
        secondaryForeground: "#fff7ed",
        muted: "#efe6d9",
        mutedForeground: "#6b7280",
        border: "rgba(15, 23, 42, 0.08)",
        input: "rgba(15, 23, 42, 0.12)",
        accent: "#fff6e8",
        accentForeground: "#7c2d12",
        destructive: "#b91c1c",
        destructiveForeground: "#fef2f2",
        ring: "#0f766e"
      },
      borderRadius: {
        lg: "1rem",
        md: "0.75rem",
        sm: "0.5rem"
      },
      boxShadow: {
        soft: "0 20px 45px rgba(15, 23, 42, 0.08)"
      }
    }
  },
  plugins: []
} satisfies Config;

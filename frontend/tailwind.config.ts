import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "#edf2f7",
        foreground: "#08111f",
        card: "rgba(255,255,255,0.84)",
        cardForeground: "#08111f",
        primary: "#0f766e",
        primaryForeground: "#ecfeff",
        secondary: "#d9f3ee",
        secondaryForeground: "#115e59",
        muted: "#ecf2f4",
        mutedForeground: "#5a6b7d",
        border: "rgba(90, 107, 125, 0.16)",
        input: "rgba(90, 107, 125, 0.22)",
        accent: "#f6fbfb",
        accentForeground: "#1f3347",
        ring: "#0f766e",
        destructive: "#dc2626",
        destructiveForeground: "#fef2f2"
      },
      borderRadius: {
        lg: "1.25rem",
        md: "0.875rem",
        sm: "0.625rem"
      },
      boxShadow: {
        soft: "0 24px 60px rgba(8, 17, 31, 0.08)",
        panel: "0 16px 40px rgba(8, 17, 31, 0.06)",
        float: "0 28px 90px rgba(15, 118, 110, 0.18)"
      }
    }
  },
  plugins: []
} satisfies Config;

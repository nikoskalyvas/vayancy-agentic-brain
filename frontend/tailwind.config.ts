import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        vayancy: {
          bg:      "#0a0a0a",
          surface: "#111111",
          border:  "#1f1f1f",
          muted:   "#2a2a2a",
          text:    "#e8e4dc",
          dim:     "#888880",
          accent:  "#c8a96e",
          green:   "#4ade80",
          amber:   "#fbbf24",
          red:     "#f87171",
          blue:    "#60a5fa",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      animation: {
        "pulse-dot": "pulse-dot 2s ease-in-out infinite",
        "spin":      "spin 1s linear infinite",
      },
      keyframes: {
        "pulse-dot": {
          "0%, 100%": { opacity: "1" },
          "50%":      { opacity: "0.3" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
